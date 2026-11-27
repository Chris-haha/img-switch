#!/usr/bin/env python3
"""Build a Windows test installer on Windows with PyInstaller and Inno Setup 6."""
import argparse
import importlib.metadata
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
GUI_HIDDEN_IMPORTS = ('failovercfg', 'task_store', 'image_decode', 'jpeg_entropy', 'tk_lifecycle', 'argparse', 'glob', 'uuid', 'base64',
                      'urllib.request', 'urllib.error', 'urllib.parse', 'json',
                      'time', 'socket')
ENGINE_FILES = (
    'engine/SKILL.md',
    'engine/scripts/failovercfg.py',
    'engine/scripts/failover_imagegen_tool.py',
    'engine/scripts/task_store.py',
    'engine/scripts/image_decode.py',
    'engine/scripts/jpeg_entropy.py',
    'engine/scripts/setup.py',
    'engine/scripts/tk_lifecycle.py',
)
APP_FILES = ('app.py', 'gui_tasks.py', 'version.py')


def stage_files(root, destination, files):
    for rel in files:
        src, dst = root / rel, destination / rel
        if not src.is_file() or src.is_symlink():
            raise ValueError('missing or invalid build input: ' + rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def stage_app(root, destination):
    stage_files(root, destination, APP_FILES)


def stage_engine(root, destination):
    """Copy only explicit runtime resources, never user configuration or caches."""
    stage_files(root, destination, ENGINE_FILES)


def audit_executable(executable):
    """Reject incomplete or sensitive-content scans before creating an installer."""
    subprocess.run([sys.executable, str(ROOT / 'packaging/windows/audit_privacy.py'),
                    str(executable)], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', help='optional assertion matching version.py VERSION')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    version = runpy.run_path(str(ROOT / 'packaging/runtime/release_version.py'))['read_version'](ROOT)
    if args.version and args.version != version['VERSION']:
        parser.error('--version must match version.py VERSION')
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        parser.error('this installer must be built with 64-bit Python on Windows')
    if importlib.metadata.version('pyinstaller') != '6.16.0':
        parser.error('install pyinstaller==6.16.0 to match the bundled license')
    iscc = shutil.which('iscc') or shutil.which('ISCC.exe')
    if not iscc:
        parser.error('Inno Setup 6 compiler (ISCC.exe) must be in PATH')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    installer = output / ('IMG-Switch-' + version['VERSION'] + '-Windows-x64-unsigned-setup.exe')
    if installer.exists():
        raise FileExistsError('Refusing to overwrite existing installer: ' + str(installer))
    notices = ROOT / 'packaging/windows/licenses'
    for name in ('TCL.txt', 'TK.txt', 'PYINSTALLER.txt'):
        if not (notices / name).is_file():
            raise RuntimeError('Required third-party license is missing: ' + name)
    with tempfile.TemporaryDirectory(prefix='img-switch-build-') as tmp:
        temp = Path(tmp)
        build_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(temp / 'pyinstaller-cache'))
        stage = temp / 'stage'
        stage.mkdir()
        stage_engine(ROOT, stage)
        stage_app(ROOT, stage)
        dist, work, specs = temp / 'dist', temp / 'work', temp / 'specs'
        specs.mkdir()
        def freeze(name, options, script):
            subprocess.run([sys.executable, '-m', 'PyInstaller.utils.cliutils.makespec',
                            '--debug', 'noarchive', '--specpath', str(specs), '--name', name] + options + [str(script)],
                           check=True, cwd=stage, env=build_env)
            spec = specs / (name + '.spec')
            subprocess.run([sys.executable, str(ROOT / 'packaging/runtime/sanitize.py'),
                            str(spec), str(temp / ('runtime-' + name))], check=True, cwd=stage, env=build_env)
            subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                            '--distpath', str(dist), '--workpath', str(work), str(spec)],
                           check=True, cwd=stage, env=build_env)
        freeze('img-switch-engine', ['--onefile', '--console'], stage / 'engine/scripts/failover_imagegen_tool.py')
        bin_dir = stage / 'engine/bin'
        bin_dir.mkdir()
        shutil.copyfile(dist / 'img-switch-engine.exe', bin_dir / 'img-switch-engine.exe')
        hidden = [part for module in GUI_HIDDEN_IMPORTS for part in ('--hidden-import', module)]
        freeze('IMG-Switch', ['--onefile', '--windowed',
               '--icon', str(ROOT / 'assets/img-switch.ico'),
               '--add-data', str(stage / 'engine') + ';engine'] + hidden +
               ['--paths', str(stage / 'engine/scripts')], stage / 'app.py')
        subprocess.run([sys.executable, str(ROOT / 'packaging/runtime/nozip.py'), str(temp)], check=True)
        audit_executable(dist / 'IMG-Switch.exe')
        # Python's Windows distribution includes the license terms for Python and
        # its bundled third-party components. Fail closed if they are unavailable.
        python_license = Path(sys.base_prefix) / 'LICENSE.txt'
        if not python_license.is_file():
            raise RuntimeError('Python distribution LICENSE.txt is missing; cannot assemble license notices')
        shutil.copyfile(ROOT / 'LICENSE', stage / 'PROJECT-LICENSE.txt')
        shutil.copyfile(python_license, stage / 'PYTHON-LICENSE.txt')
        subprocess.run([iscc, '/DAppVersion=' + version['VERSION'],
                        '/DNumericVersion=' + version['NUMERIC_VERSION'], '/DBuildNumber=' + version['BUILD_NUMBER'],
                        '/DAppExe=' + str(dist / 'IMG-Switch.exe'),
                        '/DProjectLicense=' + str(stage / 'PROJECT-LICENSE.txt'),
                        '/DPythonLicense=' + str(stage / 'PYTHON-LICENSE.txt'),
                        '/DThirdPartyLicenses=' + str(notices),
                        '/DRuntimeNotice=' + str(ROOT / 'packaging/runtime/NOTICE.txt'),
                        '/DSetupIcon=' + str(ROOT / 'assets/img-switch.ico'),
                        '/O' + str(output), str(ROOT / 'packaging/windows/setup.iss')], check=True)
        subprocess.run([sys.executable, str(ROOT / 'packaging/runtime/nozip.py'), str(installer)], check=True)
        print('Unsigned Windows test installer:', installer)

if __name__ == '__main__':
    main()
