#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
python="${PYTHON:-python3}"
output="${1:-$root/dist}"
# Verify the actual Tk runtime and native host before creating any build files.
# In CI, IMG_SWITCH_EXPECTED_ARCH also checks the runner label's architecture.
python="$("$python" -B "$root/packaging/select_python.py" --candidate "$python" --expected-arch "${IMG_SWITCH_EXPECTED_ARCH:-$(uname -m)}")"
arch="$("$python" -c 'import platform; print(platform.machine())')"
case "$arch" in arm64|x86_64) ;; *) echo "Unsupported architecture: $arch" >&2; exit 1;; esac
"$python" -c 'import tkinter, PyInstaller, pycdlib, certifi, importlib.metadata as m; assert m.version("pycdlib") == "1.22.0", "Install pycdlib==1.22.0"'
mkdir -p "$output"
output="$(cd "$output" && pwd)"
version="$("$python" "$root/packaging/runtime/release_version.py" --field VERSION)"
dmg="$output/IMG-Switch-$version-macOS-$arch-unsigned.dmg"
if [ -e "$dmg" ]; then echo "Refusing to overwrite: $dmg" >&2; exit 1; fi
work="$(mktemp -d /tmp/img-switch-build.XXXXXXXX)"
export PYINSTALLER_CONFIG_DIR="$work/pyinstaller-cache"
trap 'rm -rf "$work"' EXIT
"$python" "$root/packaging/macos/stage.py" "$root" "$work/source"
mkdir -p "$work/cli-dist" "$work/gui-dist" "$work/licenses" "$work/source/engine/bin"
cp "$work/source/LICENSE" "$work/licenses/IMG-SWITCH.txt"
cp "$root/packaging/macos/licenses/TCL.txt" "$work/licenses/"
cp "$root/packaging/macos/licenses/TK.txt" "$work/licenses/"
cp "$root/packaging/runtime/NOTICE.txt" "$work/licenses/RUNTIME-MODIFICATION.txt"
stdlib="$("$python" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
if [ ! -f "$stdlib/LICENSE.txt" ]; then echo 'Python LICENSE.txt missing; cannot prepare package.' >&2; exit 1; fi
cp "$stdlib/LICENSE.txt" "$work/licenses/PYTHON.txt"
pyinstaller_license="$("$python" -c 'import importlib.metadata as m; d=m.distribution("pyinstaller"); files=[f for f in d.files if str(f).endswith("/COPYING.txt")]; assert len(files)==1, "PyInstaller license missing"; print(d.locate_file(files[0]))')"
cp "$pyinstaller_license" "$work/licenses/PYINSTALLER.txt"
certifi_bundle="$("$python" -c 'import certifi; print(certifi.where())')"
certifi_license="$("$python" -c 'import importlib.metadata as m; d=m.distribution("certifi"); files=[f for f in d.files if str(f).endswith("/LICENSE")]; assert len(files)==1, "Certifi license missing"; print(d.locate_file(files[0]))')"
cp "$certifi_license" "$work/licenses/CERTIFI.txt"
cd "$work/source"
"$python" -m PyInstaller.utils.cliutils.makespec --debug noarchive --onefile --console --name img-switch-engine --paths engine/scripts --add-data "$certifi_bundle:certifi" --runtime-hook "$root/packaging/macos/ssl_runtime_hook.py" --specpath "$work" engine/scripts/failover_imagegen_tool.py
"$python" "$root/packaging/runtime/sanitize.py" "$work/img-switch-engine.spec" "$work/runtime-cli"
"$python" -m PyInstaller --noconfirm --clean --distpath "$work/cli-dist" --workpath "$work/cli-build" "$work/img-switch-engine.spec"
cp "$work/cli-dist/img-switch-engine" engine/bin/img-switch-engine
"$python" -m PyInstaller.utils.cliutils.makespec --debug noarchive --onedir --windowed --name 'IMG Switch' --icon "$root/assets/img-switch.icns" --paths engine/scripts --hidden-import failovercfg --hidden-import task_store --hidden-import image_decode --hidden-import jpeg_entropy --hidden-import tk_lifecycle --hidden-import urllib.request --hidden-import urllib.error --hidden-import urllib.parse --hidden-import glob --hidden-import uuid --hidden-import base64 --hidden-import argparse --hidden-import json --hidden-import time --hidden-import socket --add-data "$certifi_bundle:certifi" --runtime-hook "$root/packaging/macos/ssl_runtime_hook.py" --add-data "$work/source/engine:engine" --add-data "$work/licenses:licenses" --specpath "$work" app.py
"$python" "$root/packaging/runtime/release_version.py" --patch-spec "$work/IMG Switch.spec"
"$python" "$root/packaging/runtime/sanitize.py" "$work/IMG Switch.spec" "$work/runtime-gui"
"$python" -m PyInstaller --noconfirm --clean --distpath "$work/gui-dist" --workpath "$work/gui-build" "$work/IMG Switch.spec"
"$python" "$root/packaging/runtime/nozip.py" "$work"
"$python" "$root/packaging/macos/audit_privacy.py" "$work/gui-dist/IMG Switch.app"
mkdir -p "$work/dmg"
cp -R "$work/gui-dist/IMG Switch.app" "$work/dmg/"
ln -s /Applications "$work/dmg/Applications"
"$python" "$root/packaging/macos/make_dmg.py" "$work/dmg" "$work/image.iso"
hdiutil convert -quiet "$work/image.iso" -format UDZO -o "$work/final.dmg"
hdiutil convert -quiet "$work/final.dmg" -format UDTO -o "$work/roundtrip"
if ! cmp -s "$work/image.iso" "$work/roundtrip.cdr"; then
  echo 'DMG round-trip differs from the audited raw image.' >&2
  exit 1
fi
"$python" "$root/packaging/runtime/nozip.py" "$work"
cp "$work/final.dmg" "$dmg"
shasum -a 256 "$dmg"
echo "Created unsigned (ad-hoc signed only) test DMG: $dmg"
