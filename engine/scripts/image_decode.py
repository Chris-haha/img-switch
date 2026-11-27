"""Decode JPEG pixels before accepting an upstream image as a successful result.

macOS uses ImageIO through sips; Windows uses the built-in GDI+ decoder. Other
platforms can use an already-installed Pillow. No dependency is auto-installed.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import jpeg_entropy


class InvalidImage(ValueError):
    pass


class DecoderUnavailable(RuntimeError):
    pass


WINDOWS_SCRIPT = r'''
param([string]$ImageInputPath, [string]$ImageOutputPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$stream = $null; $image = $null; $bitmap = $null; $graphics = $null
try {
    $stream = [System.IO.File]::OpenRead($ImageInputPath)
    $image = [System.Drawing.Image]::FromStream($stream, $false, $true)
    $bitmap = New-Object System.Drawing.Bitmap($image.Width, $image.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.DrawImage($image, 0, 0, $image.Width, $image.Height)
    $bitmap.Save($ImageOutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
} catch { exit 2 }
finally {
    if ($graphics) { $graphics.Dispose() }
    if ($bitmap) { $bitmap.Dispose() }
    if ($image) { $image.Dispose() }
    if ($stream) { $stream.Dispose() }
}
'''


def validate_jpeg(data, expected_size=None):
    """Return decoded dimensions or raise a stable, credential-free error."""
    try:
        expected_size = jpeg_entropy.validate_entropy(data, expected_size)
    except jpeg_entropy.MalformedJpeg as error:
        raise InvalidImage(str(error)) from None
    except jpeg_entropy.UnsupportedJpeg as error:
        raise DecoderUnavailable(str(error)) from None
    if sys.platform == "darwin":
        decoder = "/usr/bin/sips"
        if not os.path.isfile(decoder):
            raise DecoderUnavailable("系统 JPEG 解码器不可用，请修复 macOS 图像工具后重试")
    elif os.name == "nt":
        decoder = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not decoder:
            raise DecoderUnavailable("Windows JPEG 解码需要系统 PowerShell；当前环境不可用")
    else:
        try:
            from PIL import Image, ImageFile
            import io
        except ImportError:
            raise DecoderUnavailable("此系统的 JPEG 完整解码需要 Pillow；请安装后再试，或使用返回 PNG 的源") from None
        try:
            # Pillow defaults to rejecting truncated streams. Do not alter the
            # process-wide setting, which could affect concurrent operations.
            if ImageFile.LOAD_TRUNCATED_IMAGES:
                raise DecoderUnavailable("当前图像库允许截断图片，无法执行严格检查")
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format != "JPEG" or picture.size != expected_size:
                    raise InvalidImage("JPEG 解码尺寸与文件头不一致")
                picture.load()
                return picture.size
        except DecoderUnavailable:
            raise
        except Exception:
            raise InvalidImage("JPEG 无法完整解码") from None

    with tempfile.TemporaryDirectory(prefix="img-switch-decode-") as directory:
        root = Path(directory)
        source, target = root / "input.jpg", root / "decoded.png"
        source.write_bytes(data)
        if sys.platform == "darwin":
            command = [decoder, "-s", "format", "png", str(source), "--out", str(target)]
        else:
            script = root / "decode.ps1"
            script.write_text(WINDOWS_SCRIPT, encoding="utf-8")
            command = [decoder, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", str(script), "-ImageInputPath", str(source), "-ImageOutputPath", str(target)]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=20, **options)
        except subprocess.TimeoutExpired:
            raise DecoderUnavailable("JPEG 解码超时，无法确认图片完整性；不会因此自动重新生成") from None
        except OSError:
            raise DecoderUnavailable("无法启动系统 JPEG 解码器") from None
        if process.returncode != 0 or not target.is_file():
            raise InvalidImage("JPEG 无法完整解码")
        with target.open("rb") as stream:
            header = stream.read(24)
        if not header.startswith(b"\x89PNG\r\n\x1a\n") or len(header) != 24:
            raise InvalidImage("JPEG 解码未产生有效图像")
        decoded_size = (int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big"))
        if decoded_size != expected_size:
            raise InvalidImage("JPEG 解码尺寸与文件头不一致")
        return decoded_size
