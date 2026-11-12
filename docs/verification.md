# Verification — updated 2026-11-06

## 0.1.2 runtime compatibility

Newer Codex logs use `session_task.run:run_turn{conversation.id=... turn.id=...}`
instead of the unadorned `run_turn` span. The previous SQL filter and parser both
required the old boundary, so a running watcher silently skipped these errors.
The reader now accepts both formats while checking the runtime target, complete
capacity sentence and consistent thread/turn fields, including the new span.

A read-only check of local Codex 0.166-series logs found seven genuine runtime
errors, including two capacity failures, that the old parser rejected. All seven
parse with this change. Only routing metadata was reported; no historical error
was submitted for recovery and no real log body or session ID is in the fixtures.

The queue backend now excludes archived and subagent sessions before opening
their histories, recognizes `turn_aborted`, and requires the complete recovery
message prefix when confirming delivery. Diagnostics identify the Codex home and
guard state directory so different environments can be distinguished.

Synthetic regression tests reproduce the newer spans, deleted child histories,
interrupts, quoted recovery tokens and the full log-to-queue recovery chain.
The chain checks the initial delay, a second capacity failure with increased
delay, original thread/model, delivery confirmation and no duplicate submission.
All 97 offline tests passed on macOS with Python 3.14.

Both opt-in smoke scripts also passed against a real Codex 0.166.0 app-server
with a temporary home and loopback-only model stub. The queue smoke used the
PATH Codex 0.164.0 client: a synthetic new-format capacity log plus a real failed
rollout passed through `LogSource`, `RecoveryEngine` and `CodexBackend(queue)`
to one completed continuation on the same thread/model, with two provider
requests total. Its output explicitly marks `runtime_log_synthetic=true`;
the native stub failure was not reported as a genuine upstream capacity event.
The direct transport smoke also completed on the same loaded thread/model.

```sh
python3 tests/smoke_transport.py /path/to/server-codex
python3 tests/smoke_queue.py /path/to/server-codex /path/to/queue-codex
```

These checks do not establish live recovery against an upstream model service.

## 0.1.1 compatibility release

The working local plugin cache contained a log-parser patch that was absent
from the source repository. This release brings that fix into the distributed
package: dotted and underscored thread/turn fields are both accepted, and one
balanced pair of double quotes is allowed around UUID and model values.
Conflicting aliases, mismatched thread IDs and malformed UUID values are rejected.
Quoted user/tool text still cannot trigger recovery.

On macOS with Python 3.14 and Codex CLI 0.161.0 installed:

```sh
python3 -m unittest discover -s tests -q
git diff --check
```

All 80 offline tests passed. New log fixtures cover both field spellings and
independently quoted values. The synthetic SQLite-to-socket integration covers
the underscored format, original thread/model and inherited settings. Running
the updated log/integration tests against the previous parser in a temporary
directory failed, including the new-format recovery case.

The suite still emits existing Python 3.14 SQLite `ResourceWarning` messages;
these were also present before the compatibility change. Version consistency
was checked across the Python package, `pyproject.toml` and plugin manifest.
The installer was run with the PATH Codex binary; `codex plugin list
--marketplace personal --json` reported installed version `0.1.1`, and the new
cache's runtime files matched the source. The already-running, locally patched
watcher was left running to preserve pending recoveries; this cache check is not
evidence that that process reloaded the new release.
No live capacity failure was induced and no recovery message was sent for this
release check. These tests do not establish end-to-end live delivery on 0.161.0
or compatibility with every later Codex version.

## Observed CLI recovery

On Codex CLI 0.154.0 on macOS, the guard recorded a capacity failure, submitted
one continuation through `codex queue`, observed that continuation fail with
capacity again, and submitted a second continuation. The original thread and
`gpt-6-astra` model were preserved. Distinct recovery tokens corresponded to
distinct failed turns, not duplicate submissions for one turn.

This confirms live CLI delivery and repeated retries. It does not imply that the
model's capacity shortage ended or that every supported environment was tested.
The local guard audit and Codex turn lifecycle were checked; private session IDs
and transcript contents are intentionally omitted here.

Runtime corrections include selecting the queue backend for normal CLI sessions
and routing it through its `inspect` / `resume` interface. Watcher startup alone
did not catch that integration error in the first release. Treat process checks,
backend reads and actual continuation delivery as separate evidence.

## Initial test environment

The first release was developed and tested on macOS with Python 3.14 and Codex
CLI 0.153.4. Runtime code targets Python 3.11+; GitHub CI tests 3.11–3.13 on Linux
and macOS. Windows is not supported (Unix sockets and POSIX process locking).

## Automated evidence

```sh
python3 -m unittest discover -s tests -v
python3 tests/smoke_transport.py /path/to/codex
```

The optional smoke creates a temporary configuration, workspace, app-server Unix
listener and loopback-only Responses service. Two synthetic requests demonstrate:

```json
{"failed_turn":"failed","retry_turn":"completed","same_loaded_thread":true,"same_model":true,"local_provider_requests":2}
```

All synthetic processes are cleaned up. No real conversation, credential or model
service is involved. App-server Unix sockets require HTTP WebSocket Upgrade and
masked client frames; raw newline JSON through `app-server proxy` is insufficient.
The observer deliberately ignores server-initiated approval requests so the
original client retains their callbacks.

## Desktop evidence and limits

The desktop adapter was derived from installed desktop protocol definitions:
length-prefixed JSON (32-bit little endian), owner discovery v1, snapshot v11 and
follower start-turn v2. Fake socket tests cover these messages, exact owner/turn
routing, model preservation, pending approvals, unknown versions and delivery loss.
Actual automatic continuation in the desktop UI remains a manual release check.
The private protocol may change and offers no atomic expected-turn check or draft
visibility. Metadata-only diagnostics must not be described as successful delivery.

On an isolated test conversation, enable the plugin before causing a capacity
failure. Confirm that it waits, continues on the same model/thread, and stops
scheduling the old failure when you manually continue or cancel. Do not test
against a task with pending irreversible side effects.

## Upstream references

- [Codex Hooks](https://learn.chatgpt.com/docs/hooks): background hook completion
  does not start a new idle turn; hooks can coordinate the independent watcher.
- [App Server](https://learn.chatgpt.com/docs/app-server): Unix WebSocket transport,
  thread/turn APIs and errors. Desktop owner IPC is a separate private protocol.
