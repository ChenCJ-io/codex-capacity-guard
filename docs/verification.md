# Verification — updated 2026-10-09

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
