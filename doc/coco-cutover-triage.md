# CoCo Cutover Triage

Updated: 2026-03-16 UTC

## Current cutover facts

- Repo path: `/home/pcopu/env/coco`
- Branch: `main`
- Commit: `c375023`
- New public repo: `https://github.com/pcopu/coco`
- Original repo remote remains: `https://github.com/six-ddc/ccbot`

## State migration completed

Old runtime state was copied into the CoCo state root.

- Source: `/home/pcopu/.ccbot`
- Target: `/home/pcopu/.coco`
- Backup root: `/home/pcopu/.coco-migration-backups/20260316T174355Z`

The copy was done with `rsync -a`, so the target now contains:

- `.env`
- `state.json`
- `monitor_state.json`
- `allowed_users_meta.json`
- `group_members.json`
- `codex-home/`
- `images/`
- legacy log/pid files preserved as-is

`nodes.json` in `/home/pcopu/.coco` was preserved.

## Important warning

The installed runtime has not been fully cut over yet.

At the time this document was written, these external surfaces still pointed at the old path/name:

- `/home/pcopu/.config/systemd/user/coco.service`
- `/etc/systemd/system/codex.service`
- running launcher: `/home/pcopu/env/coco/.venv/bin/python -m coco.main`

That means a restart can fail if path/name migration is only partially applied.

## Fast triage checklist

1. Confirm repo and commit:

```bash
cd /home/pcopu/env/coco
git rev-parse --short HEAD
git status --short
```

2. Confirm state files exist:

```bash
ls -l /home/pcopu/.coco/state.json /home/pcopu/.coco/monitor_state.json /home/pcopu/.coco/.env
```

3. Check user service:

```bash
systemctl --user status coco.service --no-pager
journalctl --user -u coco.service -n 200 --no-pager
```

4. Check system service if relevant:

```bash
sudo systemctl status codex.service --no-pager
sudo journalctl -u codex.service -n 200 --no-pager
```

5. Check current process:

```bash
pgrep -af "ccbot|coco|codex"
```

## If the bot restart fails

Run the bot manually first. Do not debug through systemd until the manual launch works.

```bash
cd /home/pcopu/env/coco
COCO_DIR=/home/pcopu/.coco .venv/bin/coco
```

If `coco` is not available but the environment is otherwise intact:

```bash
cd /home/pcopu/env/coco
COCO_DIR=/home/pcopu/.coco .venv/bin/python -m coco.main
```

## If CoCo state looks wrong

Inspect the current copied state:

```bash
ls -lah /home/pcopu/.coco
python -m json.tool /home/pcopu/.coco/state.json >/dev/null
python -m json.tool /home/pcopu/.coco/nodes.json >/dev/null
```

Restore the pre-cutover backup if needed:

```bash
rsync -a /home/pcopu/.coco-migration-backups/20260316T174355Z/coco.before/ /home/pcopu/.coco/
```

Re-copy the old state into CoCo if needed:

```bash
rsync -a /home/pcopu/.ccbot/ /home/pcopu/.coco/
```

## If Telegram is down but Codex should keep going

Pick up manually with Codex directly.

1. Find the bound workspace from `/home/pcopu/.coco/state.json`.
2. Change into that workspace.
3. Resume from the workspace:

```bash
cd /path/to/project
codex resume
```

If multiple sessions exist, use the latest session for that folder.

Useful transcript root:

```bash
ls -lah ~/.codex/sessions
```

## If you need to push the repo manually

The new public remote is named `coco`.

```bash
cd /home/pcopu/env/coco
git remote -v
git push -u coco main
```

## Current cutover status

1. Repo folder is now `/home/pcopu/env/coco`.
2. The active user unit is `coco.service` and points at the new path.
3. The remaining old-name system unit in `/etc/systemd/system/codex.service` is a separate inactive deployment and was intentionally not changed during this cutover.
4. Final verification is: start `coco.service`, confirm state writes under `~/.coco`, and test Telegram send/resume.
