# Codex Capacity Guard

[中文说明](README.zh-CN.md)

Resume a local Codex conversation after the runtime reports:

> Selected model is at capacity. Please try a different model.

Capacity Guard waits and retries in the **same conversation with the same model**. It never selects a fallback model. Repeated capacity errors have no retry-count limit.

This is an independent community plugin, not an OpenAI product. It is an early release: run `doctor --thread YOUR_THREAD_ID` against your installation before enabling it. Installing the plugin does **not** enable background recovery.

## How it works

1. An explicitly enabled background process reads new turn failures from Codex's local `logs_2.sqlite` database, opened read-only.
2. Only the runtime's capacity error schedules recovery. Quoted error text in chat or tool output does not trigger it.
3. After a delay, the guard checks the original conversation and submits a continuation when the failed turn is still eligible.
4. A repeated capacity error waits again. A newer user turn, cancellation, a different error, or disabling the guard stops that recovery.

The default delay grows from 30 to 60 to 120 seconds, with ±15% jitter, then stays around 120 seconds. Waiting has no maximum duration. It retries a continuation; it does not reserve capacity or predict when the model will be available.

First startup starts at the current log watermark. **Historical failures are not replayed.** If a conversation already failed before enabling the guard, continue it once yourself; subsequent capacity failures can then be recovered.

## Requirements and compatibility

- Python 3.11 or newer. The runtime uses the Python standard library only.
- A local Codex installation with the compatible SQLite log schema and a supported connection to its existing conversation owner.
- A running Codex application or App Server. This plugin cannot wake a sleeping computer or restart a closed application.

There are two connection backends:

| Backend | Connection | Compatibility boundary |
| --- | --- | --- |
| `queue` | The installed Codex CLI's `queue --thread … --message …` command. | Default for ordinary CLI sessions; the local app-server must support `thread/queue/add`. The guard omits `--model`, so the persisted model is retained. |
| `app-server` | WebSocket over an explicitly configured existing Unix socket; recovery uses `turn/start`. | The existing server must expose a compatible control socket and own the conversation. |
| `desktop` | Connects to the running desktop application's local IPC. | This is a **private, version-sensitive interface**, not a supported public plugin API. Desktop updates may require an adapter update. |

`auto` uses `queue` for ordinary CLI sessions; an explicit socket path selects `app-server`. The desktop adapter is opt-in with `backend=desktop`. It does not fall back between owners after a request. The guard does not launch a replacement App Server to take over an existing conversation. An unavailable backend leaves recovery waiting and surfaces a diagnostic; it is not a reason to change models.

The log schema and runtime error format are also internal interfaces. This release does not claim compatibility with every Codex version, remote/cloud tasks, or every operating system. Offline tests cover the core logic; `doctor` checks your local prerequisites.

## Install and enable

Clone or download this repository, then install the Codex plugin from its root:

```sh
python3 scripts/install_plugin.py
```

Follow the installer's result to make the plugin available in Codex. You can then ask Codex to use the Capacity Guard skill to check or enable recovery. The bundled skill invokes the same local commands as the CLI.

For direct terminal use, install the CLI in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codex-capacity-guard doctor
.venv/bin/codex-capacity-guard enable
.venv/bin/codex-capacity-guard status
```

Enable recovery only for selected conversation IDs if preferred:

```sh
.venv/bin/codex-capacity-guard enable --thread YOUR_THREAD_ID
```

Stop one pending recovery or disable background recovery:

```sh
.venv/bin/codex-capacity-guard cancel --thread YOUR_THREAD_ID
.venv/bin/codex-capacity-guard disable
```

`enable` without `--thread` watches eligible new failures across local main conversations. It does not replay past failures. Ordinary use does not require a model API key: recovery goes through your existing Codex application and its account.

## Configuration and local data

The state directory defaults to `$CODEX_HOME/capacity-guard`, or `~/.codex/capacity-guard` when `CODEX_HOME` is unset. Set `CODEX_CAPACITY_GUARD_HOME` to use another directory.

`config.json` in that directory accepts these settings:

```json
{
  "initial_delay": 30,
  "max_delay": 120,
  "poll_interval": 2,
  "jitter": 0.15,
  "codex_bin": "",
  "socket_path": null,
  "backend": "auto"
}
```

An empty `codex_bin` uses the `codex` executable on PATH (then the desktop-bundled CLI). You can also set `CODEX_CAPACITY_GUARD_CODEX`. A non-null `socket_path` selects the App Server backend. Disable and enable the guard after editing configuration.

The guard stores scheduling state, conversation/turn IDs, model names, and a bounded audit trail locally. It does not copy full chat transcripts, log bodies, or credentials into its state database. It reads conversation metadata from the local application to avoid resuming a stale failure. The original Codex turn may use network access and tools as authorized by that conversation.

`disable` stops recovery but keeps its local configuration and history. Do not commit state databases, Codex logs, real conversation exports, or credentials when contributing diagnostics.

## Troubleshooting

Start with:

```sh
codex-capacity-guard doctor
codex-capacity-guard status
```

- **Missing log database:** open Codex and run a local conversation. Check that the guard and application use the same `CODEX_HOME`.
- **No compatible connection:** keep Codex running; check the selected executable and backend. Desktop IPC may have changed after an application update.
- **Nothing happens after enabling:** old errors are intentionally skipped. Continue the already failed conversation once to produce a new event if capacity is still unavailable.
- **The conversation moved on:** a pending retry is cancelled when it is no longer the original failed turn. A continuation must not revive a completed or manually resumed task.
- **Capacity remains unavailable:** the guard keeps waiting for the same model while enabled. `status` shows scheduled recoveries; `disable` stops them.

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The default test suite is offline and uses synthetic fixtures. The optional App Server transport smoke test uses a temporary `CODEX_HOME` and a local model stub; see [`tests/smoke_transport.py`](tests/smoke_transport.py) before running it. It does not establish compatibility with desktop IPC.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and reporting guidance. Licensed under [MIT](LICENSE).

## Verification and current limits

- Offline unit/integration tests cover log parsing, unlimited backoff, cancellation,
  exact-thread recovery, hook behavior, socket framing, and ambiguous delivery.
- Codex CLI 0.153.4 on macOS was tested with an isolated `CODEX_HOME` and a local
  Responses stub: failed turn -> continuation on the same loaded thread -> completed;
  the model remained unchanged and no real model service was called.
- The desktop adapter targets snapshot protocol 11 and start-turn protocol 2.
  Its socket protocol is tested with a fake desktop owner. Actual desktop
  continuation has **not** been exercised by this release's automated checks.
- Desktop drafts are not exposed by IPC. The adapter rechecks state before sending,
  but the private protocol has no atomic expected-turn condition: a narrow race
  with a simultaneous user action remains. Existing pending approvals are deferred.
- `doctor` without `--thread` checks prerequisites only and returns
  `socket_present_unverified` when it finds a socket. `doctor --thread UUID` checks
  owner/state access without submitting a turn. Neither is a delivery test.
- Closing the desktop app, sleeping the computer, a missing owner, or an incompatible
  IPC version can delay recovery. If delivery times out ambiguously, the watcher
  keeps checking the thread instead of blindly sending another continuation.

See [verification notes](docs/verification.md) for the exact tested boundary.
