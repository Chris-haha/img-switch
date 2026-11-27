---
name: failover-imagegen-tool
description: 通过 OpenAI 兼容中转站生成或编辑图片，多中转站×多模型两级级联故障切换（明确失败时切换，结果未知时停止重发）。当用户要求生图、画图、AI 绘图、图像生成、图片编辑、出图、改图、批量生图，或提到中转站生图、查生图模型、源体检时使用。支持尺寸别名（2K/4K 横竖图方图）、批量生图、模型发现（--models）、源健康体检（--check），输出图片与可能包含敏感信息的 meta.json 到 generated/ 目录，API Key 保存在本机配置中。
---

# failover-imagegen-tool：中转站生图（多源×多模型级联）

通过 OpenAI 兼容中转站（`/v1/images/generations`、`/v1/images/edits`）生成或编辑图片。
故障切换为两级级联：**中转站优先级（外层）× 站内模型顺序（内层）**。

| 失败类型 | 动作 |
|---|---|
| HTTP 404、结构化明确模型/尺寸不支持 | 按兼容性尝试下个模型或来源，不偷偷改变用户尺寸 |
| 发送前连接拒绝、401/403认证拒绝、429 | 换源；内容安全拒绝始终停止 |
| HTTP 408/5xx、发送后超时/断连、已生成后的下载失败 | 记为 unknown，保留请求 ID；不得自动重发 |
| 其他 4xx、本地解码环境不可用 | 停止并解释原因 |

脚本位于本 skill 目录 `scripts/`（下文 `<skill_dir>` 即 SKILL.md 所在目录）。

## 使用前：确认已配置中转源

```bash
python <skill_dir>/scripts/failover_imagegen_tool.py --list-sources
```

提示 `No config found` 或无已启用源时先配置。每个源需要：名称、Base URL（通常以 /v1 结尾）、API Key、模型（可多个，逗号分隔或多次 `--model`，组成站内级联）。

```bash
python <skill_dir>/scripts/setup.py add --name example-source --base-url https://relay.example.com/v1 --api-key sk-xxx --model gpt-image-2.5-flare --model dall-e-3
python <skill_dir>/scripts/setup.py gui      # 图形界面（模型下拉+手输、启停、优先级、新增/编辑分离）
```

**不知道站上有什么模型？** 一条命令发现：

```bash
python <skill_dir>/scripts/failover_imagegen_tool.py --models        # 查 /v1/models 并过滤生图模型
python <skill_dir>/scripts/failover_imagegen_tool.py --check         # 源健康体检 + 历史成功/耗时统计
```

- `config.json` 含 API Key，**不要读取内容到对话、不要展示、不要提交**；列源信息一律用 `--list-sources`（已脱敏）。
- 优先使用图形界面填写密钥；命令行参数可能出现在终端历史或进程列表中。
- 站顺序用 `setup.py up/down` 调整；`--check` 的历史统计可指导排优先级。

## 生成图片

```bash
python <skill_dir>/scripts/failover_imagegen_tool.py generate \
  --prompt-file <提示词文件> --name test --size 4k --force
```

- 提示词优先 `--prompt-file`（防进终端历史）；`--input-image 参考图.png` 走编辑模式（`/v1/images/edits`）。
- `--size`：`2k`(2560x1440 默认)/`4k`(3840x2160)/`2k-portrait`/`4k-portrait`/`square`，或 `3840x2160`。
- `--source <名>` 锁定某站；`--model <id>` 临时覆盖模型（忽略站内级联）。
- 输出到当前项目 `generated/`：`test-YYYYMMDD-HHMMSS-4k.png` + 同名 `.meta.json`（含提示词、文件绝对路径、服务地址和逐次尝试记录，可能包含敏感信息）。

## 批量生图（工作流提效）

```bash
python <skill_dir>/scripts/failover_imagegen_tool.py batch --prompts-file jobs.txt --name blog --size 2k
```

`jobs.txt` 一行一条提示词（`#` 开头为注释）。逐张走完整级联，单张失败不停继续下一张，结束输出"成功 X / 失败 Y"汇总；退出码 0=全部成功、1=有失败。

## 任务查询、取消和恢复

```bash
python <skill_dir>/scripts/failover_imagegen_tool.py history --json
python <skill_dir>/scripts/failover_imagegen_tool.py cancel --task-id <任务ID>
python <skill_dir>/scripts/failover_imagegen_tool.py retry --task-id <任务ID>
```

生成时保存任务 ID；`--deadline <秒>` 指定整项任务总时限（默认 600）。任务默认保存在配置旁的 `jobs/`，使用 `--jobs-dir` 时后续命令须指定同一目录。

- 成功：按 `.meta.json` 汇报实际尺寸与图片路径，尺寸不一致要说明。
- 失败：从任务详情解释原因，修正后仅重试未完成项；已成功项不会重复执行。
- `unknown`：服务端可能已处理或计费。先向用户说明并让其核对请求 ID；未经明确同意不要使用 `--confirm-unknown`、新建同样任务或换源再次生成。用户确认后才运行 `retry --task-id <ID> --confirm-unknown`。
- 取消：停止本地等待不代表撤销上游请求；已发出的请求保留未知状态。
- 诊断中不要展示配置或密钥；即使已脱敏，提示词、图片和本机路径仍需按分享范围筛选。

## 安全红线

- 永不把 config.json 内容、密钥、完整请求头输出到对话或文件；图片、提示词和 meta.json 都可能含敏感信息，分享前须检查，无法确认时不要分享。
- 不要把密钥写进提示词。
