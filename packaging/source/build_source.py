#!/usr/bin/env python3
"""Deliver allowlisted sources as a directory; ZIP requires explicit --format zip."""
import argparse
from pathlib import Path
import sys
import zipfile
import shutil


FILES = (
    "assets/img-switch-icon.png", "assets/img-switch.icns", "assets/img-switch.ico",
    ".gitignore", "LICENSE", "README.md", "app.py", "app_tests.py",
    "gui_tasks.py", "version.py", "test_reliability.py", "test_engine_jobs.py", "test_build_tools.py",
    "test_gui_workflows.py", "test_secret_redaction.py", "test_packaging_nozip.py", "test_image_decode.py",
    "test-fixtures/valid-64.jpg", "test-fixtures/README.md", "test-fixtures/generate_jpeg.c",
    "test-fixtures/imageio-color-baseline.jpg", "test-fixtures/imageio-color-progressive.jpg",
    "test-fixtures/imageio-gray-baseline.jpg", "test-fixtures/imageio-gray-progressive.jpg",
    "test_packaged_runtime.py", "test_probe.py", "安装.bat", "卸载.bat", "打包.bat",
    "engine/.gitignore", "engine/README.md", "engine/SKILL.md",
    "engine/config.example.json", "engine/prompts/example.txt",
    "engine/scripts/failovercfg.py", "engine/scripts/failover_imagegen_tool.py",
    "engine/scripts/task_store.py", "engine/scripts/image_decode.py", "engine/scripts/jpeg_entropy.py", "engine/scripts/tk_lifecycle.py",
    "engine/scripts/setup.py", "engine/tests/mock_relay.py", "engine/tests/run_tests.py",
    "engine/tests/test_url_privacy.py", "engine/tests/test_user_agent.py", "engine/tests/test_legacy_gui_lifecycle.py",
    "packaging/macos/audit_privacy.py", "packaging/macos/build.sh", "packaging/macos/smoke.py",
    "packaging/macos/stage.py", "packaging/macos/make_dmg.py", "packaging/macos/test_audit_privacy.py",
    "packaging/macos/test_stage.py", "packaging/macos/test_make_dmg.py",
    "packaging/macos/ssl_runtime_hook.py", "packaging/macos/test_certificates.py",
    "packaging/macos/licenses/TCL.txt", "packaging/macos/licenses/TK.txt",
    "packaging/windows/audit_privacy.py", "packaging/windows/build_windows.py",
    "packaging/windows/mock_server.py", "packaging/windows/setup.iss", "packaging/windows/smoke.ps1",
    "packaging/windows/test_audit_privacy.py", "packaging/windows/test_build_windows.py",
    "packaging/windows/licenses/PYINSTALLER.txt", "packaging/windows/licenses/SOURCES.md",
    "packaging/windows/licenses/TCL.txt", "packaging/windows/licenses/TK.txt",
    "packaging/runtime/sanitize.py", "packaging/runtime/test_sanitize.py", "packaging/runtime/NOTICE.txt",
    "packaging/runtime/nozip.py", "packaging/runtime/release_version.py", "packaging/RELEASE.md", "packaging/prepare_tools.py",
    "packaging/select_python.py", "packaging/test_select_python.py", "packaging/macos/test_build_preflight.py",
    "packaging/ci_checks.py", "packaging/ci_release.py", "packaging/test_ci_release.py",
    "packaging/windows/native_smoke.py", "packaging/windows/native_processes.py",
    "packaging/windows/test_native_processes.py", "packaging/windows/test_mock_server.py",
    "docs/releases/v0.1.0-beta.6.md",
    "packaging/source/build_source.py", "packaging/source/test_build_source.py",
    ".github/workflows/test-installers.yml",
    "docs/superpowers/plans/2026-10-29-open-source-release.md",
    "docs/superpowers/specs/2026-10-29-open-source-release-design.md",
)


def collect(root):
    root = root.resolve(strict=True)
    entries = []
    for name in sorted(FILES):
        parts = Path(name).parts
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("符号链接不能进入源码包: " + name)
        if not current.is_file():
            raise ValueError("源码白名单文件缺失: " + name)
        entries.append((name, current.read_bytes()))
    return entries


def create_directory(root, output):
    entries = collect(root)
    output.mkdir()  # An existing directory, file or symlink is never overwritten.
    try:
        for name, data in entries:
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
            target.chmod(0o755 if name.endswith('.sh') else 0o644)
    except Exception:
        shutil.rmtree(output)
        raise


def create_archive(root, output):
    """Explicit opt-in only; callers must have the user's ZIP authorization."""
    entries = collect(root)
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, data in entries:
                    info = zipfile.ZipInfo("img-switch/" + name, (1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    mode = 0o100755 if name.endswith(".sh") else 0o100644
                    info.external_attr = mode << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--format", choices=("directory", "zip"), default="directory",
                        help="directory (default); zip only with explicit user authorization")
    args = parser.parse_args()
    try:
        if args.format == 'zip':
            create_archive(args.root, args.output)
        else:
            create_directory(args.root, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print("源码打包失败: %s" % exc, file=sys.stderr)
        return 1
    print("源码%s已创建: %s" % ('ZIP' if args.format == 'zip' else '目录', args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
