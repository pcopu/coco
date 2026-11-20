# Contributing

CoCo is a pragmatic Telegram overlay for real Codex sessions. Keep changes
small, test the behavior you touch, and prefer clear operational docs over
hand-wavy abstractions.

## Local Setup

```bash
uv sync --extra dev
```

## Common Checks

```bash
.venv/bin/python scripts/run_tests.py legacy -q
.venv/bin/python scripts/run_tests.py e2e -q
.venv/bin/ruff check .
```

## Guidelines

The runner reconstructs the environment and uses disposable state. The legacy
command runs every existing test in `tests/coco` and `tests/integration` against
a guarded temporary source snapshot. The E2E command drives real application
subprocesses through protocol peers; it does not import or monkeypatch CoCo
internals. Default pytest discovery remains `tests`, including all legacy tests.
Use the runner when sharing a machine with a live CoCo service.

Both runners require Linux Landlock ABI >=4 and GCC. The E2E speech scenarios
also use the normal project's soundfile/numpy dependencies. Artifacts remain in
the printed directory; choose a location with `--artifacts /tmp/my-coco-tests`.
Pytest selection arguments are forwarded, for example:

```bash
.venv/bin/python scripts/run_tests.py e2e -q -k remote
```

Negative controls deliberately modify **temporary source copies** and should
exit nonzero with the named behavioral assertion failures:

```bash
.venv/bin/python scripts/run_tests.py e2e --fault allow-every-user -q -k unauthorized_user
.venv/bin/python scripts/run_tests.py e2e --fault allow-remote-ingress -q -k remote_attachments
```

`--source-ref HEAD` tests a committed source revision without changing the
checkout. This confirmed that the legacy
`test_authorized_admin_general_voice_uses_canonical_control_owner` failure also
occurred on the original source with an empty speech-model cache: first-download
notices conflicted with its `replies == []` expectation. Its existing routing
fixture now declares that bootstrap is already complete, matching the other
routing tests. Dedicated bootstrap-notification tests remain intact. The runner
does not read production model caches or seed a fake model cache.

See [the coverage matrix](doc/e2e-coverage.md) before claiming equivalence to a
legacy regression. Add observable E2E behavior and preserve existing tests.

## Change guidelines

- Keep user-facing behavior documented in [README.md](README.md) when it changes.
- Preserve the repo's Codex-first scope; avoid reintroducing generic assistant layers.
- Prefer targeted fixes over sweeping refactors unless the migration is intentional.
- Do not commit secrets, live tokens, or local state from `~/.coco`.
