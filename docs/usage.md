# Advanced usage / 进阶使用说明

For the copy-and-paste installation and conversation prompts, start with the
[English README](../README.md) or [中文说明](../README.zh-CN.md).

## Terminal commands

以下是在终端直接操作的方式。已经通过对话完成安装的用户可以跳过安装命令。
Run these in your terminal if you prefer to manage the plugin directly.

### First installation / 首次安装

Use an existing checkout if you already have one. Otherwise:

```sh
git clone https://github.com/ChenCJ-io/codex-capacity-guard.git ~/plugins/codex-capacity-guard
cd ~/plugins/codex-capacity-guard
python3 scripts/install_plugin.py --codex-bin "$(command -v codex)"
python3 scripts/capacity_guard.py configure --backend queue --codex-bin "$(command -v codex)"
python3 scripts/capacity_guard.py enable
python3 scripts/capacity_guard.py status --json
```

The installer registers the plugin in the personal marketplace and calls
`codex plugin add`. It preserves unrelated marketplace entries. No `pip install`
is needed when running the bundled scripts. Review hooks through `/hooks` when
Codex requires trust. An installed plugin and an enabled watcher are separate states.

安装脚本会登记个人插件市场并调用 `codex plugin add`。直接运行这些脚本不需要
安装 Python 依赖。如果 Codex 提示 hooks 待信任，在 `/hooks` 中审阅。

### Manage recovery / 管理恢复

Run from the checkout directory:

```sh
# Enable / 开启
python3 scripts/capacity_guard.py enable

# Status / 查看状态
python3 scripts/capacity_guard.py status --json

# Disable / 关闭
python3 scripts/capacity_guard.py disable
```

To restrict monitoring, replace `THREAD_UUID` with the actual conversation ID:

```sh
python3 scripts/capacity_guard.py enable --thread THREAD_UUID
python3 scripts/capacity_guard.py doctor --thread THREAD_UUID
python3 scripts/capacity_guard.py cancel --thread THREAD_UUID
```

`enable --thread` changes the monitoring scope. `cancel --thread` cancels that
conversation's pending recovery; it does not turn off recovery for future failures.
`disable` stops all pending recoveries, leaving already-running Codex work alone.

`enable --thread` 会收窄监控范围；`cancel --thread` 取消该会话当前的待恢复任务，
不等于永久关闭这个会话的恢复功能。关闭整个 watcher 使用 `disable`。

## Status / 状态含义

| Field | Meaning / 含义 |
| --- | --- |
| `enabled` | Recovery is configured on / 已配置为开启 |
| `running` | The watcher process holds its lock / 后台进程正在运行 |
| `attempts` | Continuation attempts for the current recovery chain / 当前恢复链的尝试次数，不是成功次数 |
| `waiting` | Waiting for the next attempt or a usable backend / 等待重试或等待后端可用 |
| `submitted` | A continuation was submitted; completion is still being checked / 已提交续跑，仍在检查结果 |
| `uncertain` | Delivery could not be confirmed; do not blindly send again / 发送结果不确定，核对会话后再判断 |
| `recovered` | The tracked continuation completed / 跟踪的续跑回合已完成 |
| `cancelled` | This recovery is no longer scheduled / 该次恢复已取消 |
| `reason` | Why recovery is waiting or cancelled / 等待或取消的原因 |

`doctor --thread` reads the conversation state; it does not send a recovery
message. A successful result is not an end-to-end delivery test. Use the affected
conversation's audit/status and actual new turn to verify delivery.

## Updates / 更新插件

Copy this into a working Codex CLI conversation:

```text
请更新 https://github.com/ChenCJ-io/codex-capacity-guard 的本地安装到最新 main（至少 0.1.1）。先定位已有源码目录，记录 enabled 和 threads；工作区干净时执行 git pull --ff-only，不覆盖本地修改。若 watcher 已开启或正在运行，先 disable，再等待 status --json 确认 running=false。使用 PATH 中的 codex 运行更新后的 scripts/install_plugin.py --codex-bin，确认源码、安装缓存和 scripts/capacity_guard.py --version 版本一致。原来已开启的才从更新后的脚本重新 enable，并恢复原 threads 范围；原来关闭的保持关闭。保留模型、Provider、账号、推理强度、权限和等待间隔。最后报告源码提交、插件版本和 watcher 状态；如果 skill 或 hooks 尚未刷新，告诉我如何重启 CLI 或恢复对话，并按需在 /hooks 审阅。
```

For a terminal update, use the **existing checkout**, which may be elsewhere:
在终端更新时，进入**已有源码目录**；如果不是下面的路径，请替换它。

```sh
cd ~/plugins/codex-capacity-guard
python3 scripts/capacity_guard.py status --json
git status --short
```

Record `enabled` and `threads`. If Git reports local changes, resolve them before
continuing; do not reset or overwrite them. With a clean checkout:
记录 `enabled` 和 `threads`。Git 有本地改动时先处理，不要覆盖；工作区干净后执行：

```sh
git pull --ff-only origin main
python3 scripts/capacity_guard.py disable
python3 scripts/capacity_guard.py status --json
```

Wait until `running=false`, then install the new cache:
等 `running=false` 后再安装新版缓存：

```sh
python3 scripts/install_plugin.py --codex-bin "$(command -v codex)"
python3 scripts/capacity_guard.py --version
```

The compatibility release is `0.1.1`. A failed installer must be resolved before
restarting. If recovery was enabled with `threads: []`, run:
本次兼容版本为 `0.1.1`。安装失败时先处理错误。若原来已开启且 `threads: []`，执行：

```sh
python3 scripts/capacity_guard.py enable
python3 scripts/capacity_guard.py status --json
```

For a restricted scope, use `enable --thread UUID` with each original UUID
(repeat `--thread` for multiple threads). If previously disabled, skip `enable`.
Use `doctor --thread UUID` for a read-only check of an available conversation.
Restart the CLI, resume the conversation and review `/hooks` if Codex requests it.
仅监控指定会话时，对原来的每个 UUID 传入 `--thread`；原来关闭的不要执行 `enable`。
可用 `doctor --thread UUID` 只读诊断现有会话。重启 CLI、恢复会话，并按提示审阅 `/hooks`。

The personal marketplace points to a local checkout. A GitHub push or a
marketplace refresh alone does not pull that checkout or replace the installed
cache; run both `git pull --ff-only` and the installer. Each release changes
`.codex-plugin/plugin.json`'s version so Codex installs into a new cache directory.
个人市场引用的是本地源码；GitHub 推送或市场刷新不会代替拉取源码、重新安装缓存。
每次发布都会更新插件版本，避免继续使用旧版缓存。

Updating files does not reload a running Python watcher. Disable it, wait for
`running=false`, then enable from the updated installation, preserving the previous
thread scope. This restart cancels pending retries and starts at the current log
position. A conversation already stopped by a capacity error may need one manual
“continue” afterward.

更新文件不会替换已经运行的 watcher。需要先确认旧进程退出，再启动新版本。
重启会取消待恢复记录并从当前日志位置开始；重启前已经失败的任务可能需要手动
发一次“继续”。不要把文件已更新或安装成功当作后台进程已更新。

## Waiting intervals / 等待间隔

```sh
python3 scripts/capacity_guard.py configure --initial-delay 30 --max-delay 120
```

Defaults: `initial_delay=30`, `max_delay=120`, `poll_interval=2`, `jitter=0.15`.
Intervals grow exponentially to the cap, with jitter. There is no maximum attempt
count. Configuration changes apply after restarting the watcher as described above.

## Optional adapters / 可选后端

Normal CLI users should use `queue` (the default selected by `auto` without a socket).
The other adapters are optional and are not required to install or use the CLI plugin.

| Backend | Connection / 连接方式 |
| --- | --- |
| `queue` | Installed `codex queue --thread … --message …`; omits model overrides / 调用原生 CLI 队列，不传模型覆盖参数 |
| `app-server` | Existing App Server Unix socket with WebSocket framing; requires `socket_path` / 需显式配置已有控制 socket |
| `desktop` | Private local desktop IPC; version-dependent and experimental / 桌面私有 IPC，依赖版本，仍为实验适配 |

`auto` with an explicit `socket_path` selects `app-server`. Desktop drafts are not
visible over IPC, and there is no atomic expected-turn precondition. CLI rollout
formats are also internal and may change. See [verification notes](verification.md).

## Local data / 本地数据

The guard uses `$CODEX_HOME/capacity-guard` by default (`~/.codex/capacity-guard`
when `CODEX_HOME` is unset). `CODEX_CAPACITY_GUARD_HOME` overrides that location.

`config.json` contains waiting intervals and backend settings. `state.sqlite3`
contains scheduling metadata and a bounded audit log. The reader inspects Codex's
local log and session history; it does not copy full conversations, raw errors or
credentials into the guard database. Disabling leaves local configuration/history
in place. Do not attach real databases or transcripts to public issues.

配置和调度记录只保存在本机。向 GitHub 提交问题时，只提供必要的脱敏状态，
不要上传真实会话数据库、完整日志或凭据。
