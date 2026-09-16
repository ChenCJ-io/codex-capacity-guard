# Verification — updated 2026-09-16

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
