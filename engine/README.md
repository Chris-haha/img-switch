# failover-imagegen-tool

通过 OpenAI 兼容中转站生成/编辑图片，支持多源、多模型以及可恢复的本机任务记录。只在明确的失败条件下切换；无法判断服务端是否已生成时停止并保存「结果未知」。

引擎使用 Python 标准库；JPEG 另需系统像素解码器（macOS sips、Windows PowerShell/GDI+；其他系统可使用已安装 Pillow）。不自动安装依赖。PNG 校验块 CRC、解压数据和行过滤；8 位 Huffman JPEG 严格检查扫描数据并实际解码。无法支持的编码或本地解码环境不可用会明确停止；完整性检查不评价画面质量。

- 首选源：顶层 `"prefer": "源名"`；`"failover": false` 只用首选源首个模型。
- 每次任务独立保存请求 ID、耗时、逐次尝试、成功图片和失败原因；日志、任务与 meta 按已配置密钥精确脱敏。
- 配置保存采用写锁、原子替换及上一版备份；损坏配置不覆盖，修改冲突会拒绝保存。

## 目录结构

```
failover-imagegen-tool/
├── SKILL.md               # skill 定义（ZCode/Codex 等按此触发）
├── README.md
├── config.example.json    # 配置示例（含多模型级联写法，占位符非真实密钥）
├── prompts/example.txt    # 提示词文件示例
├── scripts/
│   ├── failover_imagegen_tool.py  # 生图主脚本：级联切换、批量、模型发现、体检
│   ├── setup.py           # 中转源配置管理（CLI + tkinter GUI）
│   └── failovercfg.py     # 共享配置模块
└── tests/
    ├── mock_relay.py      # 本地模拟中转站（用于验证，不访问外网）
    └── run_tests.py       # 本地端到端验证
```

## 安装为 ZCode skill

把本目录复制（或 junction）到以下任一位置即可被发现：

```
~/.agents/skills/failover-imagegen-tool/     # 用户级（推荐）
<project>/.agents/skills/failover-imagegen-tool/
```

安装后在 ZCode 里说"用 failover-imagegen-tool 生成一张…"即可触发；也可直接命令行调用脚本。

## 配置中转站

每个中转源 4 项信息：名称、Base URL（服务商提供的 OpenAI 兼容根地址，通常以 `/v1` 结尾）、API Key、模型 ID（可多个组成站内级联）。

```bash
python scripts/setup.py add --name example-source --base-url https://relay.example.com/v1 --api-key sk-xxx \
  --model gpt-image-2.5-flare --model dall-e-3      # 多次 --model = 站内级联，按顺序尝试
python scripts/setup.py list                 # 查看源与优先级（密钥脱敏显示）
python scripts/setup.py up --name example-source      # 上移提升优先级；down 下移
python scripts/setup.py disable --name example-source # 临时禁用；enable 启用；remove 删除
python scripts/setup.py gui                  # 图形界面（需 tkinter；模型可下拉选/手输，逗号分隔多个）
```

不知道站上有什么模型？先发现再配置：

```bash
python scripts/failover_imagegen_tool.py --models    # 列出各站生图相关模型
python scripts/failover_imagegen_tool.py --check     # 体检：可达性/密钥/延迟 + 历史统计
```

配置可保存在 skill 目录 `config.json` 或本机统一配置目录，也可用环境变量 `FAILOVER_IMAGEGEN_TOOL_CONFIG` 指定路径；**配置包含 API Key，禁止提交或分享**。优先使用图形界面填写密钥：命令行参数可能留在终端历史或进程列表中。

## 生成图片

```bash
python scripts/failover_imagegen_tool.py generate \
  --prompt-file prompts/example.txt \
  --name test \
  --source example-source \      # 可选：锁定某源；缺省按优先级自动级联切换
  --size 4k \
  --force
```

- 提示词：优先 `--prompt-file`（避免长提示词进入终端历史）；`--prompt` 也可。
- 尺寸别名：`2k`=2560x1440（默认）、`4k`=3840x2160、`2k-portrait`=1440x2560、`4k-portrait`=2160x3840、`square`=2048x2048；也可直接写 `3840x2160`。
- 编辑模式：`--input-image ref.png` 走 `/v1/images/edits`（multipart 上传参考图）。
- 输出：当前项目 `generated/test-YYYYMMDD-HHMMSS-4k.png` + `test-...-4k.meta.json`（含提示词、文件绝对路径、源/模型、服务主机和每次尝试结果；可能含敏感信息）。
- `--verbose` 打印请求细节，日志可能含敏感信息，分享前应检查。

## 批量生图

```bash
python scripts/failover_imagegen_tool.py batch --prompts-file jobs.txt --name blog --size 2k
```

`jobs.txt` 一行一条提示词（`#` 开头为注释，空行跳过）；产物命名 `blog-01-时间戳-2k.png`、`blog-02-…`。单张失败记录原因后继续下一张，结束打印"成功 X / 失败 Y / 共 N"与逐条清单；退出码 0=全部成功、1=有失败。

## 任务记录、取消与恢复

```bash
python scripts/failover_imagegen_tool.py history --json
python scripts/failover_imagegen_tool.py cancel --task-id <任务ID>
python scripts/failover_imagegen_tool.py retry --task-id <任务ID>
```

记录默认放在配置旁的 `jobs/`；可用 `--jobs-dir` 指定。`generate`、`batch` 的 `--deadline` 为整项任务总时限，默认 600 秒，包含排队后的尝试、网络等待和图片处理。任务 ID 在开始时输出。批量恢复跳过已成功项，并保留以往尝试。

取消会停止本地请求和后续任务；已发送给上游的请求无法保证撤回，记为「结果未知」。进程意外退出的在途任务也会恢复为未知，不自动重发。先按详情中的请求 ID 向中转站核实结果/计费；明确决定重新生成后才运行 `retry --task-id <ID> --confirm-unknown`。该选项只是用户确认重发，不是服务端幂等保证。服务商未提供通用查单接口，因此软件不会猜测扣费金额或声称已经退款。

## 故障切换规则

| 失败类型 | 行为 |
|---|---|
| HTTP 404；结构化明确拒绝模型 | 尝试下个模型/来源 |
| 结构化明确拒绝尺寸 | 尝试下个来源；不偷偷更改尺寸 |
| 发送前 DNS、连接拒绝、证书校验失败；认证拒绝或 429 | 尝试下个来源；同批跳过已确认不可用源 |
| HTTP 408、5xx、发送后超时/断连、生成后下载失败 | 结果未知，停止该项自动重发；保留请求 ID |
| 确认是坏图、错误 Base64、响应无图 | 按明确异常尝试后备；会记录每次请求，可能产生额外费用 |
| 内容安全拒绝、其他未知 4xx、本地解码器不可用 | 停止；检查原因后再决定操作 |

可在来源配置中填写 `supported_models: ["模型ID"]` 和 `supported_sizes: ["1024x1024", "2k"]`。请求前校验已配置能力；未填写时按服务实际响应处理。GUI 编辑会保留这些可选字段。尺寸别名仅表示请求值，结果以实际解码尺寸为准。

## 常见问题

- **No config found**：先 `setup.py add` 保存至少一个已启用源，再 `--list-sources` 检查。
- **不知道填什么模型**：跑 `--models` 看该站支持的生图模型，选一个或多个填入。
- **模型或尺寸不支持（400/422）**：查服务商文档确认模型 ID 和允许尺寸，只调对应参数；不要为绕过 4xx 反复重试。
- **超时/5xx**：查看任务详情并向中转站核对请求 ID；未知状态不会自动切换，确认要重新生成后再重试。
- **实际尺寸与请求不一致**：以输出图片实际宽高为准（见 `.meta.json` 的 `size_mismatch`）；上游可能不支持请求尺寸，本地放大不等于原生 4K 细节。
- **防泄露**：用 `--prompt-file`，不要把密钥写进提示词；不分享配置、终端历史或完整请求日志。图片、提示词和 meta.json 都可能包含敏感内容，分享前逐项检查。

## 验证

```bash
python tests/run_tests.py
python tests/test_url_privacy.py
```

在本地起模拟中转站（127.0.0.1），26 项端到端验证：站级/模型级故障切换、URL/b64 响应、致命 400 不切换、尺寸、编辑模式、批量失败续跑、模型发现过滤、体检统计、常见密钥格式打码、GUI 冒烟与自测。不访问外网、不需要真实密钥。
