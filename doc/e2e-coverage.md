# CoCo deterministic boundary E2E coverage

All **1,556 legacy tests remain in place and in collection**. The E2E suite adds
observable workflows alongside them. It does not establish complete behavioral
equivalence to the legacy suite, and no tests are retired.

## Run and boundaries

```bash
.venv/bin/python scripts/run_tests.py e2e -q
.venv/bin/python scripts/run_tests.py legacy -q
```

There are 57 E2E cases (including parameterized media/update cases). Real
`coco.main` controller/agent processes run normal PTB polling, handlers, queues,
CodexAppServerClient, persistence, attachment codecs and Git commands. External
services are deterministic peers: Telegram HTTP, an executable Codex JSON-RPC
stdio process, and a speech HTTP endpoint. Tests assert actual protocol records,
visible messages, downloaded/uploaded bytes, files and Git HEADs. No E2E test
imports or monkeypatches application internals.

These are **boundary E2E tests**, not live Telegram/OpenAI/speech-model smoke
tests. Successful actual speech recognition, actual model output and Telegram's
server-side media validation remain outside this suite.

The runner needs GCC and Linux Landlock ABI >=4. Child processes receive fresh
HOME, COCO_DIR, CODEX_HOME, SESSIONS_PATH, fake credentials and explicit loopback
endpoints. Landlock restricts filesystem access and TCP ports; an inherited
dynamic-library guard restricts addresses, DNS, Unix sockets and signals.
Unsupported isolation fails closed. The safety probe checks denied external
network/file access and a harmless signal-0 check against the harness parent.
The policy is a guard against accidental access by the tested application; it
is not a hostile-native-code containment boundary.

The runner snapshots application source so concurrent working-copy edits cannot
change the code midway through a run. Application cleanup checks the dedicated process group and fails if descendants
leak. The outer runner also checks its pytest group. Protocol and process logs
remain under the printed artifact directory. Ordinary Git probes can attempt DNS
lookups; those attempts are blocked and recorded. No updater can write the real
checkout. Legacy multiprocessing tests additionally receive disposable local
listener registration and shared-memory semaphore access.

## Behavior matrix

| Family | Observable E2E assertions | Remaining behavior not proven here |
|---|---|---|
| Isolation | [sandbox](../tests/e2e/test_sandbox.py): external IP/DNS, other loopback ports, outside files and unowned signal denied; permitted peer works | Host pytest itself is trusted; abrupt host termination can bypass fixture cleanup |
| Authorization and ownership | [controller](../tests/e2e/test_controller.py), [approvals/security](../tests/e2e/test_approvals_security.py): unauthorized user/group, nonadmin update/allowlist actions, forged folder topic, approval admin-only and duplicate resolution | Anonymous-admin fallback, all callback families, ACL migration and every revoke-during-await race |
| General and topic isolation | [lifecycle](../tests/e2e/test_lifecycle.py): reserved General commands, canonical owner/admin sharing, distinct users/topics, per-topic fast mode and replies | Historical General migration collisions, all host-follow aliases and cross-chat collisions |
| Sessions and persistence | [lifecycle](../tests/e2e/test_lifecycle.py): directory bind, unbind, fork, rollback, goal/settings persistence, manual controller stop/start and real `/restart` re-exec preserve thread | Full resume history pagination, topic deletion, worktree create/fold and corrupted-state repair |
| Queues and transport | [queue/transport](../tests/e2e/test_queue_transport.py): FIFO dispatch once, steer active turn, interrupt, discard queued input/late output on unbind, process death and lost ACK without replay, flood with another responsive topic, actual 30-second watchdog keepalive without replay | Every in-flight rebind race, safe-read retries, late-response permutations and other watchdog timing/retry branches |
| Approvals and user input | [approvals/security](../tests/e2e/test_approvals_security.py): command approval owned by admin, nonadmin file-change decline, structured input default response | Full approval modes, expiry/overflow and every unknown server-request shape; current input behavior auto-selects defaults |
| Media ingress/output | [attachments](../tests/e2e/test_attachments.py): photo, PDF, image/audio documents and video input; photo/GIF/video/document output bytes and topic; fallback/permanent failure; split reply content/order; Telegram 429 without repeated Codex turn | Office formats, successful archive extraction, download failure races and actual Telegram transcoding |
| Media confinement | [attachments](../tests/e2e/test_attachments.py): parent/absolute/symlink output escape and ZIP traversal reject; [remote](../tests/e2e/test_remote.py): controller-local media rejected before download for remote topics | URL attachment policy, archive bombs/hardlinks and every parser edge case |
| Voice | [voice](../tests/e2e/test_voice.py): real WAV decode/resample/Opus encode and upload; HTTP outage text fallback; empty-model transcription failure without dispatch | Successful ASR/model bootstrap, silence trimming edge cases, managed speech-server install/start/idle stop, General voice routing |
| Local/remote updates | [updates](../tests/e2e/test_updates.py): both real updaters advance untracked-only clones, preserve artifacts, reject tracked modifications and incoming untracked collisions via real Git | Staged-only parameter, npm/Codex upgrades, package installation and every update discovery error |
| Remote cluster | [remote](../tests/e2e/test_remote.py): heartbeat/picker/bind/turn/goal/artifact delivery; secret, malformed JSON, unknown method, stale epoch reject; offline/recovery gates and no replay | Saturation/oversized frame limits, all forwarded ACK/approval/notification epoch races and topology migration |
| Models/status/monitoring | [runtime features](../tests/e2e/test_runtime_features.py): cached model/reasoning choices reach next turn; status reports node and wire quota; General doctor probes workspace/thread health; [controller](../tests/e2e/test_controller.py): real resource sample produces weekly RAM/disk summary and persisted monitor state | Live model refresh edge cases, quota reset-credit HTTP/auth, resource threshold histories and complete runtime capability reports |
| Apps/skills/plugins | [runtime features](../tests/e2e/test_runtime_features.py): app/native skill injection, persistence/clear/topic isolation; plugin inventory admin toggle; guided looper start/stop and completion keyword; autoresearch outcome capture/persistence, real heuristic Run Now digest, schedule/stop; real personality scheduler digest and deduplication across restart from historical topic memory | Scheduled looper nudges, autoresearch daily timer/research-provider execution, marketplace installation, all cross-chat state migration and delivery-failure retries |
| Memory/Chief of Staff | [controller](../tests/e2e/test_controller.py): visible incoming/outgoing messages persisted without JSON-RPC; [runtime features](../tests/e2e/test_runtime_features.py): authenticated ingress, invalid request, busy/no side effect, explicit retry and General-only delivery | Webhook service's durable deduplication/retry, voice ingress, edit/truncation/rotation edge cases |
| Parser/concurrency regressions | Malformed RPC, archive traversal, output confinement, flood, queue ordering and stale topic output have boundary scenarios above | Legacy parser, serialization, resource limits, lock fairness and adversarial race tests remain essential |

## Legacy node mapping

[e2e-legacy-manifest.json](e2e-legacy-manifest.json) inventories every collected
node and its source definition/hash/assertions: 1,556 nodes, 1,349 definitions.
Parameterized nodes resolve to their base definition. Every unmapped definition
explicitly lacks reviewed E2E equivalence; a family table entry does not imply it
is covered.

Four legacy parameter IDs embed collection-time monotonic timestamps. Their
literal IDs vary between runs; preservation was verified by matching every
definition and its parameter count as well as the unchanged assertions.

[e2e-reviewed-mappings.json](e2e-reviewed-mappings.json) holds 25 reviewed mappings
with concrete scenario IDs, evidence and gaps. Four map the relevant observable
behavior fully; twenty-one are partial. All other definitions remain unmapped.
This is intentionally conservative: testing a happy-path family does not prove
its legacy edge cases. The matrix also links new scenarios that have no direct
one-to-one legacy counterpart.

Regenerate after collecting the legacy suite through the safe runner:

```bash
.venv/bin/python scripts/run_tests.py legacy --collect-only -q
.venv/bin/python scripts/audit_e2e_coverage.py /printed/artifact/path/pytest.log | apply_patch
```

The audit validates legacy and E2E IDs and preserves original assertion text;
it does not infer coverage from names or automatically mark cases equivalent.

## Verification and negative controls

Final local verification on 2026-10-11:

| Run | Result | Time |
|---|---|---|
| E2E, frozen source, first final run | 57 passed | 221.00s |
| E2E, frozen source, repeated final run | 57 passed | 218.18s |
| Preserved legacy suite | 1,556 passed, 15 existing warnings | 23.64s |
| Combined collection | 1,613 collected | 4.46s |

Both E2E source snapshots match the final production files. Cleanup found zero
remaining worker runtime/peer/pytest processes. All recorded denied attempts
were attributable to the deliberate safety probe or temporary Git DNS probes.
Detailed commands, timings, earlier red/green evidence and final results are
recorded in `/tmp/coco-e2e-20261011/final.md`.

The harness detected two deliberate faults in temporary source copies:

```bash
.venv/bin/python scripts/run_tests.py e2e --fault allow-every-user -q -k unauthorized_user
.venv/bin/python scripts/run_tests.py e2e --fault allow-remote-ingress -q -k remote_attachments
```

The first fails authorization; the second fails document/photo/video ingress.
The latter also found a real production bug before the fix: only General checked
remote attachment ingress. Named remote topics now reject controller-local file
paths before download, using the existing error and telemetry behavior.

Another E2E check found cached plugin inventory reading the plugin directory as
the marketplace name. Cache paths are now decoded as
`<market>/<plugin>/<revision>/.codex-plugin/plugin.json`, so enabling a cached
plugin writes the correct `<plugin>@<market>` configuration key.

The endpoint configuration change adds opt-in `COCO_TELEGRAM_BASE_URL` and
`COCO_TELEGRAM_BASE_FILE_URL` to controller PTB construction. Defaults remain
`https://api.telegram.org/bot` and `https://api.telegram.org/file/bot`. The initial
isolated boot test failed before this wiring and passed afterward. Separate CLI
Telegram calls do not yet consume these endpoint settings.

Two existing fixture assumptions were corrected while preserving every original
assertion. The General voice-routing test now declares bootstrap complete,
matching adjacent routing tests; its original empty-cache failure was also
reproduced against committed source. Dedicated bootstrap-notification tests are
unchanged. The photo rebind race now identifies its starting machine as local,
so the new remote-ingress guard allows it to reach the download race. No test is
removed, skipped, excluded or stripped of assertions, and no production model
cache is accessed or fake model cache supplied by the runner.

The added GitHub workflow runs both preserved legacy and deterministic E2E suites
on Ubuntu 24.04 and retains failure traces. It has not been executed on GitHub
from this workspace.
