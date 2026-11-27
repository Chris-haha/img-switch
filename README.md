# IMG Switch（img-switch）

> 管理本机 Agent 的中转站生图技能、源配置与故障转移。

IMG Switch 把一个 OpenAI 兼容的**中转站生图技能**按需安装进本机各 AI 编程 Agent（ZCode / Codex / Claude Code / Trae / CodeBuddy / MateClaw / Cursor / 跨工具 `.agents` 标准），并提供一个 ccswitch 风格的图形控制台管理一切。适合没有 GPT 套餐、但想在自己的工作流里稳定出图的用户。

**关注作者** · [B站：不吃辣的Chris](https://space.bilibili.com/387976167) · [抖音：不吃辣的Chris](https://v.douyin.com/AbSCTFj0QLM/) · [小红书：AI硬核智识](https://xhslink.cn/o/2mNaOYWXMZK) · [个人网站](https://heychris.cn/)

## 下载与安装

本版为 **v0.1.0-beta.6 测试版**。安装包包含运行程序，无需预装 Python。发布流程要求三平台原生验收全部通过；安装包、验收 JSON 和 SHA-256 在同一 Release 提供。更新内容见 [版本说明](docs/releases/v0.1.0-beta.6.md)。

| 系统 | 下载 |
| --- | --- |
| Windows x64 | [下载安装程序（.exe）](https://github.com/Chris-haha/img-switch/releases/download/v0.1.0-beta.6/IMG-Switch-0.1.0-beta.6-Windows-x64-unsigned-setup.exe) |
| macOS Apple Silicon（M 系列） | [下载 Apple Silicon 安装包（.dmg）](https://github.com/Chris-haha/img-switch/releases/download/v0.1.0-beta.6/IMG-Switch-0.1.0-beta.6-macOS-arm64-unsigned.dmg) |
| macOS Intel | [下载 Intel 安装包（.dmg）](https://github.com/Chris-haha/img-switch/releases/download/v0.1.0-beta.6/IMG-Switch-0.1.0-beta.6-macOS-x86_64-unsigned.dmg) |

[全部版本与源码](https://github.com/Chris-haha/img-switch/releases) · [SHA-256 校验文件](https://github.com/Chris-haha/img-switch/releases/download/v0.1.0-beta.6/SHA256SUMS.txt)

> **测试版提示：** 安装包尚未正式签名，macOS 版也未公证，系统可能显示安全提示或阻止打开。请只从本项目 Release 下载，并在确认来源和完整性后自行决定是否安装。

### Windows 安装

1. 下载上表中的 Windows 安装程序，双击运行并按提示完成安装。
2. 从开始菜单打开 **IMG Switch**。安装默认只针对当前用户，并会创建开始菜单快捷方式。
3. 按下方「首次使用」配置中转源并接入 Agent。

卸载前，请先在应用的「Agent 接入」页关闭需要断开的开关，再通过 Windows 的应用设置卸载。卸载程序不会自动移除已接入的技能，配置也会保留。

### macOS 安装

1. 在苹果菜单  →「关于本机」查看芯片或处理器：Apple M 系列选择 **Apple Silicon**，Intel 处理器选择 **Intel**。不确定时可参考 [Apple 的说明](https://support.apple.com/zh-cn/116943)。
2. 打开下载的 `.dmg`，将 **IMG Switch** 拖入 **Applications（应用程序）** 文件夹，然后从「应用程序」启动。
3. 若 macOS 阻止打开，在确认文件可信且未被篡改后，可查看 [Apple 的安全打开说明](https://support.apple.com/zh-cn/102445)。
4. 按下方「首次使用」配置中转源并接入 Agent。

卸载前，请先在「Agent 接入」页关闭需要断开的开关，再将「应用程序」中的 **IMG Switch** 移到废纸篓。配置默认保留。

## 首次使用

1. **准备服务：** 先获取支持图片生成、兼容 OpenAI 接口的服务地址、API Key 和可用生图模型。IMG Switch 本身不提供密钥或生图服务。
2. **添加中转源：** 打开「中转源」→「＋ 添加中转源」，填写自定义名称、Base URL（通常以 `/v1` 结尾）、API Key 和至少一个生图模型，然后点击「保存」。可点「⟳ 获取」拉取模型；如果服务不支持获取列表，可手动填写模型。
3. **检查连接：** 点击中转源卡片上的「探测」，验证模型列表接口返回有效数据。该结果不代表图片生成权限、余额或实际出图能力。点击卡片「测试出图」，确认可能计费后生成一张；成功后可查看实际尺寸、打开图片及任务详情。
4. **接入 Agent：** 打开「Agent 接入」，开启要使用的 Agent 右侧开关。页面只显示本机检测到的 Agent；也可点击「全部接入」。
5. **开始生图：** 在目标项目中打开已接入的 Agent，提出图片生成请求，例如「生成一张山间日出的插画」。默认图片和对应记录保存在该项目的 `generated/` 目录。

需要停止接入时，在「Agent 接入」关闭对应开关；配置仍保留。页面显示技能能否被发现，实际是否调用由 Agent 决定；已有会话请重启后确认。API Key 保存在本机配置中，并会在请求时发送给你配置的中转源；不要提交或分享配置文件。

Codex 开关会维护 `~/.codex/config.toml` 中的原生技能启停规则，关闭时仍保留其他 Agent 使用的共享技能。Cursor 还会发现 `.agents`、`.claude`、`.codex` 中的同名技能，因此存在共享副本时，软件会明确阻止无法落实的单独关闭；先关闭相应共享接入，或在 Cursor 中自行管理。明确排除 Cursor 后，软件也会阻止让它重新发现技能的共享安装。用户自行安装的同名技能不会被本软件删除。

## 功能特性

- **Agent 按需接入**：显示本机检测到的 Agent 与已知共享发现路径；Codex 使用原生规则启停，Cursor 共享冲突会明确提示
- **两级故障转移**：站优先级（外层）× 站内多模型（内层），明确的模型不支持、认证拒绝或发送前连接失败可切换；超时、HTTP 5xx 等结果未知时停止自动重发，避免重复生成；可一键关闭
- **★ 首选源**：显式指定生图第一个尝试的源，与排序无关；▲▼ 调整后备顺序
- **模型实时获取**：配置中转源时点「⟳ 获取」直接拉取该站 `/v1/models` 生图模型，不再手填猜测
- **探测**：校验模型列表接口及响应结构，显示延迟；HTML 登录页不会被判为连接正常
- **批量生图 / 模型发现 / 源体检**：引擎 CLI 一并支持（`--models`、`--check`、`batch`）
- **测试出图与任务历史**：界面主动试图，显示耗时和实际尺寸；任务独立落盘，支持搜索、状态筛选、详情、取消和仅重试未完成项，未知结果需先核对
- **本机配置**：API Key 保存在本机配置文件中；生成记录和日志可能包含敏感信息，分享前须检查

## 从源码运行与构建

### Windows 源码运行

```bash
# 需要 Python 3.11+（含 tkinter）
python app.py gui      # 图形界面
python app.py on       # 命令行：全部接入（遵守排除名单）
python app.py off      # 移除本软件管理的技能，配置保留；已有会话需重启
python app.py status   # 查看状态
```

双击 `安装.bat` 可创建桌面/开始菜单快捷方式（走本机 pythonw）。

在「Agent 接入」页开启需要接入的 Agent，然后在「中转源」页添加 Base URL、API Key 和生图模型。之后在项目目录中使用已接入的 Agent 生成图片，图片会保存在该项目的 `generated/`。

### 构建 Windows 测试安装包

在 Windows x64 上使用已有 Python/Tk 和 Inno Setup 6。macOS 则在对应架构的机器上使用已有 Python/Tk。依赖准备、无 ZIP 构建、安装包验收与发布边界统一见 [packaging/RELEASE.md](packaging/RELEASE.md)。

源码运行：`python3 app.py gui`，需要 Python 3.11+ 与 tkinter。GUI 依赖本机窗口环境；CLI 可用于自动化。

构建默认不发布版本。CI 仅手动触发，本地成品必须实际安装并运行后才能记录对应平台通过。测试安装包未正式签名、公证。

### 导出源码目录

使用白名单导出到新目录，不包含配置、图片、运行日志与构建缓存；拒绝覆盖已有目录：

```bash
python3 -B packaging/source/build_source.py --output dist/source-review
```

## 目录结构

```
img-switch/
├── app.py               # IMG Switch 主程序（GUI 控制台 + on/off/status CLI）
├── app_tests.py         # App 集成测试（11 项，临时 HOME 隔离）
├── 安装.bat / 卸载.bat    # Windows 快捷方式脚本
├── 打包.bat              # 构建未签名 Windows 测试安装包
├── packaging/           # 两平台构建脚本和许可证材料
├── engine/              # 生图技能本体（可独立使用，见 engine/README.md）
│   ├── SKILL.md         # skill 定义（Agent 按此触发）
│   ├── scripts/         # failover_imagegen_tool.py / failovercfg.py / setup.py
│   └── tests/           # 引擎端到端测试（26 项，本地 mock 中转站，不访问外网）
└── dist/                # PyInstaller 产物（自行构建生成，不入库）
```

## 只想要生图技能（不要控制台）

`engine/` 目录即完整的 failover-imagegen-tool 技能，可独立分发：复制到任何 Agent 的 skills 目录（如 `~/.agents/skills/failover-imagegen-tool/`）即可被识别，用法见 `engine/README.md`。

## 配置与密钥安全

- 统一配置：Windows `%APPDATA%\failover-imagegen-tool\config.json`，macOS/Linux `~/.config/failover-imagegen-tool/config.json`
- 排除名单（Agent 按需接入）：同目录 `app-settings.json`
- 保存前检查配置是否损坏或已被其他窗口修改，通过进程锁和原子替换避免互相覆盖。上一次有效配置保存在 `config.json.backup`，也包含密钥；POSIX 系统新写文件权限为 `0600`。锁文件会保留，不表示进程一直占用。
- 遇到配置错误时停止写入，修复原文件或确认备份内容后恢复，再重新打开页面。编辑源期间发生改名、删除或外部修改时，请重新打开该源。
- Codex 配置首次修改前备份到 `config.toml.img-switch-backup`；只修改带 `IMG SWITCH SKILL POLICY` 标记的规则段，自动升级技能不会改动用户的原生启停策略。
- 安装新版后首次启动应用时，会自动更新已接入且由 IMG Switch 管理的 Agent 技能；未接入和非本软件管理的同名技能不会被自动接入或覆盖。
- **config.json 含 API Key，切勿提交或分享**（仓库 .gitignore 已排除常见配置文件）。忽略规则不能替代发布前检查。
- 升级或关闭旧版已接入技能时，技能目录中的旧配置和临时配置会备份到统一配置目录的 `migration-backups/`；备份同样可能含密钥，请勿提交或分享。
- `.meta.json` 可能包含提示词、本机绝对路径、服务地址和错误信息；图片本身也可能包含敏感内容。分享前请逐项检查，无法确认时不要分享。
- 命令行的 `--api-key` 参数可能出现在终端历史或进程列表中；优先使用图形界面填写密钥。
- 配置示例见 `engine/config.example.json`

## 开源发布前检查

- 使用当前清理后的源码，勿直接发布原始压缩包或未经检查的构建产物。打包时排除本机扩展属性、绝对路径、缓存、配置、日志和生成记录。
- 检查实际待发布文件及 Git 历史；首次提交前确认 Git 提交者名称和邮箱符合匿名要求，后续每次提交也要检查。`.gitignore` 不会移除已跟踪文件或已有历史。
- 如需发布可执行文件，检查其嵌入的路径、元数据和第三方许可信息；不能为匿名而删除必须保留的版权和许可证声明。
- 确认项目源码及依赖的许可和发布权限，避免将个人或组织数据加入示例与测试。

## 运行测试

```bash
python engine/tests/run_tests.py   # 引擎 26 项（mock 中转站，不联网）
python engine/tests/test_url_privacy.py  # URL 凭据显示回归测试
python test_packaged_runtime.py    # 独立引擎与技能安装回归测试
python -m unittest test_secret_redaction test_image_decode test_engine_jobs test_gui_workflows
python test_reliability.py         # 配置冲突、实际 Tk 窗口、Agent 策略与异常图片响应回归
python packaging/windows/test_build_windows.py
python packaging/macos/test_stage.py
python packaging/macos/test_certificates.py
python packaging/macos/test_make_dmg.py
python packaging/source/test_build_source.py
python app_tests.py                # App 11 项（安装/卸载/配置迁移/GUI）
```

## License

[MIT](LICENSE)
