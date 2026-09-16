# Codex Capacity Guard

[English](README.md)

这是一个 **Codex CLI 插件**。当终端里的 Codex 出现：

> Selected model is at capacity. Please try a different model.

插件会等待后自动继续**原会话、原模型**，容量不足时持续重试。

**你照常启动 `codex` 即可，不需要追加启动参数，也不需要自己输入 `codex queue`。** 安装和日常开启都可以直接在 Codex 对话中完成。

## 第一次使用：复制这一整段给 Codex

在一个**能正常响应的 Codex CLI 对话**里，粘贴下面这段话：

```text
请帮我安装并开启 Codex Capacity Guard CLI 插件，仓库是 https://github.com/ChenCJ-io/codex-capacity-guard 。先阅读仓库 README 和安装脚本，确认本机是 macOS 或 Linux、Python 3.11 及以上，且 PATH 中的 Codex CLI 支持 queue 和 plugin add。把仓库克隆到 ~/plugins/codex-capacity-guard；如果已经安装，复用已有源码目录，不覆盖我的本地修改。使用 PATH 中的 codex 执行安装，运行 scripts/install_plugin.py 时用 --codex-bin 指定这个可执行文件。将 guard 的 backend 配置为 queue，codex_bin 配置为同一个可执行文件；保持我原来的模型、Provider、账号、推理强度和权限设置。如果旧版 watcher 还在运行，先 disable，等 status 确认 running=false 后，再从更新后的脚本启动。执行 scripts/capacity_guard.py enable 和 status --json，确认 enabled=true、running=true；若能取得当前会话 ID，再执行 doctor --thread 检查并单独报告结果，不把进程运行当作自动恢复成功。此次开启覆盖共用当前 CODEX_HOME 的本地会话。如果 Codex 要求信任 hooks，请明确告诉我在 /hooks 中需要确认什么，不绕过信任检查。最后告诉我安装版本、源码位置、运行状态、尚需处理的步骤，并给出以后在对话中开启、查看状态和关闭的方法。
```

这段话会让 Codex 完成**安装、配置和开启**。单独运行安装脚本只会安装插件，不会自动开启恢复。插件沿用现有 Codex 配置，不需要另外申请模型 API Key。

如果安装后找不到 `$capacity-guard`，重启 CLI，或在新的 CLI 进程中恢复原对话，让它加载新插件。若提示 hooks 待信任，进入 `/hooks` 审阅。**整个流程不需要桌面应用。**

## 日常使用：在对话里唤醒

以后在 Codex CLI 对话里发送下面任意一条即可，每次只发送你需要的那一条：

| 你想做什么 | 复制到对话里的文字 |
| --- | --- |
| 开启自动恢复 | `$capacity-guard 开启自动恢复` |
| 查看是否运行、重试了几次 | `$capacity-guard 查看恢复状态` |
| 关闭自动恢复 | `$capacity-guard 关闭自动恢复` |
| 只监控当前对话 | `$capacity-guard 只为当前会话开启自动恢复，使用当前会话 ID` |
| 排查没有恢复的原因 | `$capacity-guard 排查当前会话为什么没有自动恢复，检查后端、watcher 和最近失败回合` |

最常用的开启方式就是：

```text
$capacity-guard 开启自动恢复
```

开启后继续正常工作。**watcher 已启用且正在运行时，不用每轮都唤醒它。** 默认监控共用同一个 `CODEX_HOME` 的本地会话，不只限于你输入开启指令的那个对话；“只为当前会话开启”会把监控范围收窄到该会话。

## 报错之后会看到什么？

容量错误发生后，插件约 **30 秒**后发起第一次续跑；如果新回合仍报容量不足，下一次约等 **60 秒**，之后约等 **120 秒**。每次有 ±15% 随机浮动，**没有最大重试次数**。

对话中可能出现这样的消息：

```text
[codex-capacity-guard recovery=...]
Continue the task interrupted by temporary model capacity limits, using the same model.
```

这是插件发送的恢复提示。若新的恢复回合也失败，会再次出现。插件负责替你继续尝试，不能让上游模型提前恢复容量。

- 保持 CLI 会话打开、电脑不休眠。
- 只处理**开启以后新检测到的容量错误**。如果安装前就已经失败，先手动发一次“继续”，之后再出现的容量错误会被监控。
- 会话进入新回合、明确取消、出现其他错误或关闭插件时，可以终止旧的待恢复任务；关闭插件不会打断已经运行的 Codex 回合。
- `enabled=true`、`running=true` 只表示后台进程已开启。要确认有没有实际续跑，还要看对应会话的 `attempts`、`status` 和 `reason`。

## 环境与排查

需要 macOS 或 Linux、Python 3.11 及以上，以及支持 `codex queue` 和 `codex plugin add` 的 Codex CLI。已经在 **Codex 0.154.0** 上观察到真实 CLI 自动续跑。当前版本不覆盖 Windows、远程或云端会话。

没有自动恢复时，可以复制这一段：

```text
$capacity-guard 请排查当前 CLI 会话的自动恢复。检查 backend 是否为 queue、watcher 是否运行着当前安装版本、最近的容量错误是否发生在开启之后，并显示对应的 attempts、status、reason。不要只凭 enabled=true 或 doctor 成功就判断续跑已送达。
```

如果当前模型已经不可用，它就无法执行安装或配置 skill。可以先在能响应的 Codex 对话中配置，或使用[终端命令](docs/usage.md#terminal-commands)。开启后的 watcher 独立运行，不依赖当前模型响应。

更新插件、终端操作、重试间隔、数据位置和可选后端见[进阶使用说明](docs/usage.md)。测试范围和限制见[验证记录](docs/verification.md)。

## 开发与许可证

```sh
python3 -m unittest discover -s tests -v
```

默认测试使用本地合成数据；可选的 [App Server 冒烟测试](tests/smoke_transport.py) 使用隔离环境和本地模型模拟服务。贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

这是独立社区项目，采用 [MIT 许可证](LICENSE)。
