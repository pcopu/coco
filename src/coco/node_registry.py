"""Persistent registry of controller/agent nodes for multi-machine CoCo."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import config
from .runtime_capabilities import get_local_runtime_capabilities, get_local_runtime_summary
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

NODE_STATUS_ONLINE = "online"
NODE_STATUS_OFFLINE = "offline"
NODE_STATUS_DEGRADED = "degraded"


def _coerce_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_port(value: object) -> int:
    port = _coerce_int(value)
    return port if 1 <= port <= 65535 else 0


def _coerce_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_str(value: object, *, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _coerce_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


@dataclass
class NodeStatusChange:
    machine_id: str
    display_name: str
    old_status: str
    new_status: str
    last_seen_ts: float


@dataclass
class NodeRecord:
    machine_id: str
    display_name: str
    tailnet_name: str = ""
    status: str = NODE_STATUS_ONLINE
    last_seen_ts: float = 0.0
    browse_roots: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)
    agent_version: str = ""
    transport: str = "local"
    rpc_host: str = ""
    rpc_port: int = 0
    is_local: bool = False
    controller_capable: bool = False
    controller_active: bool = False
    preferred_controller: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "machine_id": self.machine_id,
            "display_name": self.display_name,
            "status": self.status,
            "last_seen_ts": self.last_seen_ts,
            "transport": self.transport,
            "rpc_host": self.rpc_host,
            "rpc_port": self.rpc_port,
            "is_local": self.is_local,
            "controller_capable": self.controller_capable,
            "controller_active": self.controller_active,
            "preferred_controller": self.preferred_controller,
        }
        if self.tailnet_name:
            payload["tailnet_name"] = self.tailnet_name
        if self.browse_roots:
            payload["browse_roots"] = list(self.browse_roots)
        if self.capabilities:
            payload["capabilities"] = list(self.capabilities)
        if self.runtime:
            payload["runtime"] = dict(self.runtime)
        if self.agent_version:
            payload["agent_version"] = self.agent_version
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeRecord":
        machine_id = _coerce_str(data.get("machine_id"))
        display_name = _coerce_str(data.get("display_name")) or machine_id
        status = _coerce_str(data.get("status"), default=NODE_STATUS_OFFLINE)
        if status not in {NODE_STATUS_ONLINE, NODE_STATUS_OFFLINE, NODE_STATUS_DEGRADED}:
            status = NODE_STATUS_OFFLINE
        browse_roots = _coerce_str_list(data.get("browse_roots"))
        capabilities = _coerce_str_list(data.get("capabilities"))
        raw_runtime = data.get("runtime", {})
        runtime = dict(raw_runtime) if isinstance(raw_runtime, dict) else {}
        try:
            last_seen_ts = float(data.get("last_seen_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            last_seen_ts = 0.0
        if not math.isfinite(last_seen_ts):
            last_seen_ts = 0.0
        return cls(
            machine_id=machine_id,
            display_name=display_name,
            tailnet_name=_coerce_str(data.get("tailnet_name")),
            status=status,
            last_seen_ts=last_seen_ts,
            browse_roots=browse_roots,
            capabilities=capabilities,
            runtime=runtime,
            agent_version=_coerce_str(data.get("agent_version")),
            transport=_coerce_str(data.get("transport"), default="local") or "local",
            rpc_host=_coerce_str(data.get("rpc_host")),
            rpc_port=_coerce_port(data.get("rpc_port", 0)),
            is_local=_coerce_bool(data.get("is_local", False)),
            controller_capable=_coerce_bool(data.get("controller_capable", False)),
            controller_active=_coerce_bool(data.get("controller_active", False)),
            preferred_controller=_coerce_bool(data.get("preferred_controller", False)),
        )


class NodeRegistry:
    """Persistent node registry with online/offline transition tracking."""

    def __init__(
        self,
        *,
        state_file: Path | None = None,
        offline_timeout_seconds: float | None = None,
    ) -> None:
        self.state_file = state_file or config.node_registry_file
        self.offline_timeout_seconds = (
            float(offline_timeout_seconds)
            if offline_timeout_seconds is not None
            else float(config.node_offline_timeout)
        )
        self._nodes: dict[str, NodeRecord] = {}
        self._pending_status_changes: list[NodeStatusChange] = []
        self._load()

    @property
    def local_machine_id(self) -> str:
        return config.machine_id.strip()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load node registry %s: %s", self.state_file, exc)
            return
        if not isinstance(payload, dict):
            logger.warning("Failed to load node registry %s: root is not an object", self.state_file)
            return
        raw_nodes = payload.get("nodes", {})
        if not isinstance(raw_nodes, dict):
            return
        parsed: dict[str, NodeRecord] = {}
        for machine_id, raw_record in raw_nodes.items():
            if not isinstance(raw_record, dict):
                continue
            record = NodeRecord.from_dict(raw_record)
            normalized_id = record.machine_id or str(machine_id).strip()
            if not normalized_id:
                continue
            record.machine_id = normalized_id
            parsed[normalized_id] = record
        self._nodes = parsed

    def _save(self) -> None:
        payload = {
            "nodes": {
                machine_id: record.to_dict()
                for machine_id, record in sorted(self._nodes.items())
            }
        }
        atomic_write_json(self.state_file, payload)

    def get_node(self, machine_id: str) -> NodeRecord | None:
        normalized = machine_id.strip()
        if not normalized:
            return None
        return self._nodes.get(normalized)

    def iter_nodes(self) -> list[NodeRecord]:
        return list(self._nodes.values())

    def ensure_local_node(
        self,
        *,
        machine_id: str | None = None,
        display_name: str | None = None,
        transport: str = "local",
        is_local: bool = True,
        now: float | None = None,
    ) -> NodeRecord:
        return self.note_heartbeat(
            machine_id=machine_id or config.machine_id,
            display_name=display_name or config.machine_name,
            tailnet_name=config.tailnet_name,
            transport=transport,
            rpc_host=config.rpc_advertise_host or config.tailnet_name,
            rpc_port=config.rpc_port,
            is_local=is_local,
            browse_roots=[str(config.browse_root)],
            capabilities=get_local_runtime_capabilities(
                controller_capable=config.controller_capable,
            ),
            runtime=get_local_runtime_summary(
                controller_capable=config.controller_capable,
            ),
            controller_capable=config.controller_capable,
            controller_active=config.controller_active,
            preferred_controller=config.preferred_controller,
            now=now,
        )

    def note_heartbeat(
        self,
        *,
        machine_id: str,
        display_name: str,
        tailnet_name: str = "",
        transport: str,
        rpc_host: str = "",
        rpc_port: int = 0,
        is_local: bool,
        browse_roots: list[str] | None = None,
        capabilities: list[str] | None = None,
        runtime: dict[str, Any] | None = None,
        agent_version: str = "",
        controller_capable: bool = False,
        controller_active: bool = False,
        preferred_controller: bool = False,
        now: float | None = None,
    ) -> NodeRecord:
        normalized_id = machine_id.strip()
        if not normalized_id:
            raise ValueError("machine_id is required")
        normalized_display = display_name.strip() or normalized_id
        timestamp = time.time() if now is None else float(now)
        existing = self._nodes.get(normalized_id)
        old_status = existing.status if existing is not None else NODE_STATUS_ONLINE
        record = NodeRecord(
            machine_id=normalized_id,
            display_name=normalized_display,
            tailnet_name=tailnet_name.strip(),
            status=NODE_STATUS_ONLINE,
            last_seen_ts=timestamp,
            browse_roots=list(browse_roots or []),
            capabilities=list(capabilities or []),
            runtime=dict(runtime or {}),
            agent_version=agent_version.strip(),
            transport=transport.strip() or "local",
            rpc_host=rpc_host.strip(),
            rpc_port=_coerce_port(rpc_port),
            is_local=is_local,
            controller_capable=controller_capable,
            controller_active=controller_active,
            preferred_controller=preferred_controller,
        )
        self._nodes[normalized_id] = record
        self._save()
        if existing is not None and old_status != NODE_STATUS_ONLINE:
            self._pending_status_changes.append(
                NodeStatusChange(
                    machine_id=normalized_id,
                    display_name=record.display_name,
                    old_status=old_status,
                    new_status=NODE_STATUS_ONLINE,
                    last_seen_ts=record.last_seen_ts,
                )
            )
        return record

    def mark_stale_nodes_offline(self, *, now: float | None = None) -> list[NodeStatusChange]:
        timestamp = time.time() if now is None else float(now)
        changes: list[NodeStatusChange] = []
        for record in self._nodes.values():
            if record.is_local:
                continue
            if record.status != NODE_STATUS_ONLINE:
                continue
            if record.last_seen_ts <= 0:
                continue
            if timestamp - record.last_seen_ts < self.offline_timeout_seconds:
                continue
            old_status = record.status
            record.status = NODE_STATUS_OFFLINE
            change = NodeStatusChange(
                machine_id=record.machine_id,
                display_name=record.display_name,
                old_status=old_status,
                new_status=NODE_STATUS_OFFLINE,
                last_seen_ts=record.last_seen_ts,
            )
            changes.append(change)
            self._pending_status_changes.append(change)
        if changes:
            self._save()
        return changes

    def drain_status_changes(self) -> list[NodeStatusChange]:
        changes = list(self._pending_status_changes)
        self._pending_status_changes.clear()
        return changes


node_registry = NodeRegistry()
