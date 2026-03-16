#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -n "${COCO_DIR:-}" ]]; then
    RUNTIME_DIR="$COCO_DIR"
else
    RUNTIME_DIR="$HOME/.coco"
fi
PID_FILE="$RUNTIME_DIR/coco.pid"
LOG_FILE="$RUNTIME_DIR/coco.log"
MAX_WAIT=10

mkdir -p "$RUNTIME_DIR"

is_pid_running() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

resolve_start_command() {
    if command -v uv >/dev/null 2>&1; then
        START_CMD=(uv run coco)
    elif [[ -x "$PROJECT_DIR/.venv/bin/uv" ]]; then
        START_CMD=("$PROJECT_DIR/.venv/bin/uv" run coco)
    elif [[ -x "$PROJECT_DIR/.venv/bin/coco" ]]; then
        START_CMD=("$PROJECT_DIR/.venv/bin/coco")
    elif command -v coco >/dev/null 2>&1; then
        START_CMD=(coco)
    else
        echo "Unable to find a CoCo launcher. Install uv or create .venv with a coco entrypoint." >&2
        exit 1
    fi
}

stop_existing() {
    local pid=""
    if [[ -f "$PID_FILE" ]]; then
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    fi

    if is_pid_running "$pid"; then
        echo "Stopping existing CoCo process ($pid)..."
        kill "$pid" 2>/dev/null || true

        local waited=0
        while is_pid_running "$pid" && [[ "$waited" -lt "$MAX_WAIT" ]]; do
            sleep 1
            waited=$((waited + 1))
            echo "  Waiting for process to exit... (${waited}s/${MAX_WAIT}s)"
        done

        if is_pid_running "$pid"; then
            echo "Process still running, sending SIGKILL..."
            kill -9 "$pid" 2>/dev/null || true
            sleep 1
        fi
    fi

    # Best-effort cleanup for stale pid file.
    rm -f "$PID_FILE"
}

start_new() {
    echo "Starting CoCo from $PROJECT_DIR"
    resolve_start_command
    echo "Launch command: ${START_CMD[*]}"
    (
        cd "$PROJECT_DIR"
        nohup "${START_CMD[@]}" >>"$LOG_FILE" 2>&1 &
        echo $! >"$PID_FILE"
    )

    sleep 2
    local new_pid
    new_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if ! is_pid_running "$new_pid"; then
        echo "CoCo failed to start. Recent logs:"
        echo "----------------------------------------"
        tail -30 "$LOG_FILE" 2>/dev/null || true
        echo "----------------------------------------"
        exit 1
    fi

    echo "CoCo restarted successfully (pid $new_pid). Recent logs:"
    echo "----------------------------------------"
    tail -20 "$LOG_FILE" 2>/dev/null || true
    echo "----------------------------------------"
}

stop_existing
start_new
