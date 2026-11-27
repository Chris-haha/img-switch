# 无 ZIP 构建与发布准备

`version.py` 是版本来源。`VERSION` 用于显示和成品文件名；`NUMERIC_VERSION` 与 `BUILD_NUMBER` 用于平台的数字版本信息。不要单独修改 workflow 中的文件名或版本。

默认源码交付为新目录，拒绝覆盖已有目标：

```sh
python3 -B packaging/source/build_source.py --output dist/source-review
python3 -B packaging/runtime/nozip.py dist/source-review
```

仅在用户明确要求或同意 ZIP 时使用 `--format zip`。现有压缩包不覆盖、不删除；默认测试也不创建 ZIP。

## 本机构建

构建要求已安装的 Python、Tk 和平台编译工具。依赖准备使用 `packaging/prepare_tools.py`：已发布 wheel 只在内存读取并展开普通文件，不在磁盘创建 wheel/ZIP，不用 `pip install`。找不到可用 Python/Tk 时停止，不自动安装解释器。不要复用已有构建临时目录或改写已发布成品。

macOS：

```sh
python3 -B packaging/prepare_tools.py --target build/tools
PYTHONPATH="$PWD/build/tools" PYTHONDONTWRITEBYTECODE=1 PYTHON=python3 bash packaging/macos/build.sh dist/round2
```

`PYTHONPATH` 指向 `--target` 指定的目录。构建包含两个 `--debug noarchive` 冻结步骤，Python 模块以独立 `.pyc` 收集。保留的 PYZ/CArchive 是 PyInstaller 自有格式，不是 ZIP。临时目录和 app 执行 ZIP 检查、字节码及二进制路径扫描后才生成版本化 DMG；DMG 镜像仍需挂载执行 `packaging/macos/smoke.py`。

Windows x64：使用已有 64 位 Python/Tk 和 Inno Setup 6（ISCC.exe 在 PATH），准备依赖后运行：

```powershell
python -B packaging/prepare_tools.py --target build/tools
$env:PYTHONPATH = Join-Path $PWD 'build/tools'
python -B packaging/windows/build_windows.py --output dist/windows-review
$version = python -B packaging/runtime/release_version.py --field VERSION
./packaging/windows/smoke.ps1 -Installer "dist/windows-review/IMG-Switch-$version-Windows-x64-unsigned-setup.exe"
```

`--version` 仅为可选断言，必须和 `version.py` 的 VERSION 一致。安装器包含数字版本和显示版本。

## CI 与发布

PR 在 Windows Server 2022 x64、macOS Apple Silicon 和 `macos-15-intel` 原生 runner 上验收。`select_python.py` 只选择已有、架构匹配且可以实际初始化 Tk 的解释器；不下载解释器，也不接受 Rosetta 充当 Intel 运行结果。

全部验收通过后先合并 main，再从该提交创建 `codex/release-v<version.py VERSION>` 分支。发布流程重新验收最终提交，在私有草稿中逐文件上传原始 EXE/DMG 与验收 JSON。只有三个平台的单测、真实安装/GUI/引擎烟测、隐私检查全部成功，且重新下载后的二进制 SHA-256 与各平台记录一致，才生成校验清单并公开该版本。

- 不使用 Actions artifact 上传/下载；这些动作会生成 ZIP。源码默认交付为普通目录。
- `ci_release.py` 仅在明确版本的 release 分支运行，校验 main 提交一致，并拒绝改写已有标签、公开版本或同名资产。
- 测试只使用本地模拟服务与隔离配置，不向真实上游扣费。
- Windows 升级测试使用受管理技能的过期引擎夹具，验证程序启动后替换及配置保留；不把它描述为安装了历史 beta.5 安装程序。
- 原始日志和个人真实服务证据不提交；公开验收 JSON 只包含平台、源码提交、检查结果和安装包校验。
- 安装包仍是未正式签名的测试版，macOS 尚未公证。

本次修复已将 ZIP 创建测试改为目录/独立字节码测试。旧 ZIP 输入兼容读代码可以保留，但新构建数据遇到 ZIP 会失败，不会生成清理副本。
