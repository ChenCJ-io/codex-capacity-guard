# Codex Capacity Guard

[English](README.md)

当本地 Codex 的运行时出现以下错误时，自动等待并继续原会话：

> Selected model is at capacity. Please try a different model.

**始终使用原模型、原会话，不切换备用模型。** 容量错误反复出现时，持续等待，没有最大重试次数。

这是独立的社区插件，不是 OpenAI 官方产品。目前为早期版本，启用前请运行 `doctor` 检查本机兼容性。**安装插件不会自动开启后台恢复。**

## 工作方式

1. 用户显式启用后，后台进程以只读方式扫描 Codex 本地 `logs_2.sqlite` 中的新回合错误。
2. 只有运行时的容量错误会安排恢复。聊天或工具输出引用这句话不会触发。
3. 等待一段时间后，检查原会话；失败回合仍符合恢复条件时，发送继续指令。
4. 再次容量不足就继续等待；用户发起新回合、取消、出现其他错误或关闭插件时，停止该次恢复。

默认间隔从 30 秒增加到 60 秒、120 秒，之后保持约 120 秒；每次加入 ±15% 随机浮动。插件不能预留模型容量，也无法预测何时恢复。

首次启动从当前日志末尾开始，**不会重放历史错误**。如果会话在开启插件前就已经失败，请先手动继续一次；之后新出现的容量错误才会进入自动恢复流程。

## 环境与兼容范围

- macOS 或 Linux、Python 3.11 及以上，运行时仅使用 Python 标准库；暂不支持 Windows。
- 本地 Codex 安装，其日志 SQLite 结构以及现有会话连接方式需要兼容。
- Codex 应用或 App Server 保持运行。插件不能唤醒休眠电脑，也不会重启已关闭的应用。

支持两种连接后端：

| 后端 | 连接方式 | 兼容边界 |
| --- | --- | --- |
| `app-server` | 通过显式配置的 Unix socket，以 WebSocket 接入现有 App Server，再调用 `turn/start`。 | 现有服务必须提供兼容的控制 socket，并拥有目标会话。 |
| `desktop` | 连接正在运行的桌面应用的本地 IPC。 | 这是**私有、依赖版本的接口**，不是官方稳定的插件 API；应用更新后可能需要更新适配。 |

`auto` 默认使用桌面后端；显式配置 socket 路径时使用 App Server。它不会在发送请求后切换连接到另一个 owner。插件不会另起一个 App Server 接管桌面会话。后端暂时不可用时，恢复任务保留等待，并记录诊断信息，不会因此切换模型。

日志结构和运行时错误格式也属于内部接口。此版本不承诺兼容所有 Codex 版本、所有操作系统或远程/云端任务。离线测试验证核心行为，`doctor` 检查本机运行前提。

## 安装和开启

克隆或下载本项目，在项目根目录运行：

```sh
python3 scripts/install_plugin.py
```

按安装脚本的结果让插件在 Codex 中可用。之后可以让 Codex 使用 Capacity Guard skill 检查环境、开启或关闭恢复。Skill 与命令行调用的是同一套本地功能。

如果直接从终端管理，在虚拟环境安装 CLI：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codex-capacity-guard doctor
.venv/bin/codex-capacity-guard enable
.venv/bin/codex-capacity-guard status
```

只监控指定会话：

```sh
.venv/bin/codex-capacity-guard enable --thread YOUR_THREAD_ID
```

取消某个会话当前的待恢复任务，或者关闭整个后台恢复：

```sh
.venv/bin/codex-capacity-guard cancel --thread YOUR_THREAD_ID
.venv/bin/codex-capacity-guard disable
```

不传 `--thread` 的 `enable` 会监听本地主会话中符合条件的新错误，不会补跑历史错误。正常使用不需要新增模型 API 密钥，恢复请求通过现有 Codex 应用和账户执行。

## 配置与本地数据

状态目录默认是 `$CODEX_HOME/capacity-guard`；未设置 `CODEX_HOME` 时为 `~/.codex/capacity-guard`。可以通过 `CODEX_CAPACITY_GUARD_HOME` 指定其他目录。

该目录下的 `config.json` 支持：

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

`codex_bin` 留空时自动查找可执行文件，也可以设置 `CODEX_CAPACITY_GUARD_CODEX`。`socket_path` 用于指定现有 App Server 的控制 socket；配合 `backend=desktop` 时也可指定桌面 IPC socket。修改配置后先 `disable`，再 `enable`。

插件在本地保存调度状态、会话和回合 ID、模型名及有限条审计记录，不会将完整聊天记录、错误正文或凭据复制进状态数据库。为了避免恢复过期错误，会向本地应用读取会话元数据。恢复后的 Codex 回合仍按照原会话的授权使用网络和工具。

`disable` 关闭恢复，但保留本地配置和记录。提交诊断材料时，不要上传状态数据库、Codex 日志、真实会话导出或密钥。

## 排查

先运行：

```sh
codex-capacity-guard doctor
codex-capacity-guard status
```

- **找不到日志数据库：** 打开 Codex 并运行一个本地会话，确认插件与应用使用同一个 `CODEX_HOME`。
- **找不到兼容连接：** 保持 Codex 运行，检查可执行文件和后端。桌面应用更新可能改变私有 IPC。
- **开启后没有动作：** 首次启动跳过旧错误；已失败的会话需要先手动继续一次，后续新容量错误才会进入监控。
- **会话已经继续：** 原失败回合不再是待恢复目标时，撤销重试，避免重新唤起已完成或手动继续的任务。
- **模型一直不可用：** 开启期间持续等待原模型。用 `status` 查看计划，用 `disable` 停止。

## 开发

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

默认测试完全离线，只使用合成数据。可选的 App Server 传输冒烟测试使用临时 `CODEX_HOME` 和本地模型替身，运行前请阅读 [`tests/smoke_transport.py`](tests/smoke_transport.py)。该测试不能证明桌面 IPC 兼容。

贡献和问题报告说明见 [CONTRIBUTING.md](CONTRIBUTING.md)，采用 [MIT 许可证](LICENSE)。

## 验证结果与当前限制

- 离线单元及集成测试覆盖错误识别、无限退避、取消、原线程恢复、hook、socket 协议及不确定的发送结果。
- 已用 macOS 上的 Codex CLI 0.153.4、隔离的 `CODEX_HOME` 和本地 Responses 模拟服务实测：失败回合 → 同一已加载线程续跑 → 完成，模型保持一致，未调用真实模型服务。
- 桌面适配使用 snapshot 协议 11、start-turn 协议 2，已通过模拟桌面 owner 的 socket 测试；**本版本尚未实测真实桌面里的自动续跑效果**。
- IPC 不暴露输入框草稿，也没有原子的 expected-turn 条件。发送前会重新检查会话状态，但与用户同时操作仍有很小的竞态窗口；发现待审批请求时会等待。
- 不带 `--thread` 的 `doctor` 只检查环境，找到 socket 会返回 `socket_present_unverified`。`doctor --thread UUID` 只读验证 owner 和会话状态，两者都不是实际发送测试。
- 电脑休眠、应用关闭、找不到 owner 或 IPC 不兼容时继续等待。发送结果不确定时，只核对原线程，不直接再次发送，以免重复执行。

具体证据范围见[验证记录](docs/verification.md)。
