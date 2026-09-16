# Codex Capacity Guard

[中文说明：复制一段话完成安装](README.zh-CN.md)

A **Codex CLI plugin** that waits and continues the same conversation when Codex reports:

> Selected model is at capacity. Please try a different model.

Keep starting Codex the way you already do. The plugin runs a local background watcher and sends a continuation after a capacity failure, without selecting another model. You do not add `codex queue` to your startup command or type it yourself.

## Install and enable: paste this into Codex

Copy the entire paragraph below into a **working Codex CLI conversation**:

```text
Please install and enable Codex Capacity Guard for my Codex CLI from https://github.com/ChenCJ-io/codex-capacity-guard. Read its README and installation script first. Check that I have macOS or Linux, Python 3.11+, and a Codex CLI with `queue` and `plugin add` support. Clone the repository to ~/plugins/codex-capacity-guard, or reuse its existing local checkout without overwriting my changes. Use the Codex CLI on my PATH to run scripts/install_plugin.py with --codex-bin set to that executable. Configure the guard's backend as queue and its codex_bin as that same executable; retain my current model, provider, account, reasoning and permission settings. If an older guard watcher is running, disable it and wait until status reports running=false before starting the updated script. Run scripts/capacity_guard.py enable and status --json to verify that the watcher is enabled and running. If the current thread ID is available, also run doctor --thread with that ID and report its result separately; a running watcher alone does not prove successful recovery. Enable recovery across local sessions sharing this CODEX_HOME. If Codex requires hook trust, tell me exactly what to review in /hooks without bypassing it. Finally report the installed version, checkout path, watcher status and any remaining setup step, and show me how to enable, inspect and disable the plugin in conversation.
```

This paragraph asks Codex to install **and enable** recovery. Running the installer alone only installs the plugin. The plugin uses your existing Codex setup; you do not need a new model API key.

If the skill is not visible after installation, restart the CLI or resume your conversation in a new CLI process so it picks up the plugin. Review new hooks through `/hooks` if Codex asks. You do not need the desktop app.

## Use it in conversation

Send **one** of these messages in your Codex CLI conversation:

| What you want | Message to paste |
| --- | --- |
| Enable recovery | `$capacity-guard Enable automatic recovery` |
| Inspect the watcher and recent retries | `$capacity-guard Show recovery status` |
| Disable recovery | `$capacity-guard Disable automatic recovery` |
| Restrict recovery to this conversation | `$capacity-guard Enable automatic recovery only for this conversation; use its current thread ID` |
| Diagnose a problem | `$capacity-guard Diagnose why automatic recovery is not working in this conversation; check the backend, watcher and latest failed turn` |

For example, enable it with:

```text
$capacity-guard Enable automatic recovery
```

Then continue your task normally. While the watcher is enabled and running, you do not need to invoke the skill for every turn. Default enablement covers sessions sharing the same `CODEX_HOME`; it is not limited to the conversation where you typed the command. Restricting it to one conversation changes that monitoring scope.

## What happens after a capacity error?

The watcher waits about **30 seconds**, then submits a continuation to the **same conversation**. If that new turn also fails with the capacity error, the next delay is about **60 seconds**, then **120 seconds** for later attempts. Delays include ±15% jitter and there is no retry-count limit.

You may see a message starting with:

```text
[codex-capacity-guard recovery=...]
Continue the task interrupted by temporary model capacity limits, using the same model.
```

That is the plugin's continuation message. It can appear more than once if separate recovery turns also fail. The plugin does not make the model available sooner; it removes the need to keep typing “continue.”

- Keep the CLI session open and your computer awake.
- Only capacity failures detected **after enabling** are scheduled. If the conversation already failed before setup, send “continue” once; subsequent capacity failures are monitored.
- A newer turn, explicit cancellation, a different error, or disabling the guard can end a pending recovery. Disabling does not interrupt a Codex turn that is already running.
- `enabled=true` and `running=true` mean the watcher is on. They do **not** prove a continuation was sent. Inspect `attempts`, `status` and `reason` for the affected conversation.

## Requirements and troubleshooting

Use macOS or Linux, Python 3.11+, and a Codex CLI that supports `codex queue` and `codex plugin add`. CLI recovery has been observed on **Codex 0.154.0**. Windows and remote/cloud sessions are not covered by this release.

If recovery does not happen, paste:

```text
$capacity-guard Diagnose automatic recovery for this CLI conversation. Check that the backend is queue, the watcher is running the current installed code, and the latest capacity failure was detected after enablement. Show its attempts, status and reason. Do not treat enabled=true or a successful doctor check as proof that a continuation was delivered.
```

If the current model is already unavailable, it cannot run the skill to install or configure the plugin. Use the [terminal commands](docs/usage.md#terminal-commands), or set it up in a working Codex conversation first. Once enabled, the watcher runs independently of model calls.

For updates, terminal commands, waiting intervals, data locations and optional adapters, see [Advanced usage](docs/usage.md). See [Verification](docs/verification.md) for tested behavior and limits.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic local fixtures. The optional [App Server smoke test](tests/smoke_transport.py) uses an isolated Codex installation and a loopback model stub. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.

Independent community project. Licensed under [MIT](LICENSE).
