"""Run native checks and emit a small public evidence record, without archives."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
COMMON = ('app_tests.py', 'test_probe.py', 'test_reliability.py', 'test_engine_jobs.py',
          'test_image_decode.py', 'test_gui_workflows.py', 'test_secret_redaction.py',
          'test_packaging_nozip.py', 'test_build_tools.py', 'test_packaged_runtime.py',
          'engine/tests/run_tests.py', 'engine/tests/test_user_agent.py',
          'engine/tests/test_url_privacy.py', 'engine/tests/test_legacy_gui_lifecycle.py', 'packaging/runtime/test_sanitize.py',
          'packaging/test_select_python.py', 'packaging/test_ci_release.py')


def check_platform(target):
    expected_os = 'Windows' if target == 'windows-x64' else 'Darwin'
    expected_arch = 'arm64' if target == 'macos-arm64' else 'x86_64'
    arch = {'AMD64': 'x86_64', 'aarch64': 'arm64'}.get(platform.machine(), platform.machine())
    if platform.system() != expected_os or arch != expected_arch:
        raise RuntimeError('This is not the requested native platform: ' + target)
    if expected_os == 'Darwin':
        native = subprocess.run(['/usr/sbin/sysctl', '-in', 'sysctl.proc_translated'],
                                capture_output=True, text=True)
        if native.stdout.strip() == '1':
            raise RuntimeError('Rosetta is not a native Intel acceptance environment')


def execute(command, label, timeout=180):
    print('\nCHECK ' + label, flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=ROOT, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Check timed out after %s seconds: %s' % (timeout, label)) from None
    entry = {'name': label, 'passed': result.returncode == 0,
             'elapsed_seconds': round(time.monotonic() - started, 2)}
    if result.returncode:
        raise RuntimeError('Check failed: ' + label)
    return entry


def run_tests(target, record):
    check_platform(target)
    names = list(COMMON)
    folder = 'windows' if target == 'windows-x64' else 'macos'
    names += [p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'packaging' / folder).glob('test_*.py'))]
    # Print the exact test in progress and thread stacks before a stuck script
    # reaches its deadline. This also covers native Tk calls that never return.
    bootstrap = ('import faulthandler, os, runpy, sys; '
                 'faulthandler.enable(); faulthandler.dump_traceback_later(90, repeat=True); '
                 'sys.argv = sys.argv[1:]; '
                 'sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0]))); '
                 'runpy.run_path(sys.argv[0], run_name="__main__")')
    record['checks'] = []
    failures = []
    for name in names:
        try:
            entry = execute([sys.executable, '-B', '-u', '-c', bootstrap, name, '-v'], name)
        except RuntimeError as error:
            # Suites use independent fixtures. Finish collecting failures so a
            # platform-specific assertion cannot hide later native checks.
            entry = {'name': name, 'passed': False, 'error': str(error)}
            failures.append(name)
            print(str(error), flush=True)
        record['checks'].append(entry)
    if failures:
        raise RuntimeError('Native regression checks failed: ' + ', '.join(failures))
    record['tests_passed'] = True


def smoke(target, package, record):
    check_platform(target)
    if not record.get('tests_passed') or record.get('commit') != os.environ.get('IMG_SWITCH_SOURCE_SHA', os.environ['GITHUB_SHA']):
        raise RuntimeError('Native tests for this commit must pass before installed smoke')
    if target == 'windows-x64':
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(ROOT / 'packaging/windows/smoke.ps1'), '-Installer', str(package)]
    else:
        command = [sys.executable, '-B', str(ROOT / 'packaging/macos/smoke.py'), str(package)]
    record['checks'].append(execute(command, 'installed_smoke', timeout=600))
    record['installed_smoke_passed'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('tests', 'smoke'))
    parser.add_argument('--platform', required=True, choices=('windows-x64', 'macos-arm64', 'macos-x86_64'))
    parser.add_argument('--record', required=True, type=Path)
    parser.add_argument('--package', type=Path)
    args = parser.parse_args()
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    record = ({'platform': args.platform, 'commit': os.environ.get('IMG_SWITCH_SOURCE_SHA', os.environ['GITHUB_SHA']),
               'python': platform.python_version(), 'system': platform.system(),
               'machine': platform.machine(), 'tests_passed': False, 'installed_smoke_passed': False}
              if args.action == 'tests' else json.loads(args.record.read_text()))
    try:
        if args.action == 'tests':
            run_tests(args.platform, record)
        else:
            if not args.package:
                raise RuntimeError('--package is required')
            smoke(args.platform, args.package, record)
    finally:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
