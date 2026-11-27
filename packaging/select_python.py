#!/usr/bin/env python3
"""Select an existing native CPython with working Tk and pip; never install one.

stdout contains only the executable path for shell/PowerShell command capture.
Probe results go to stderr. GitHub's default Homebrew Python need not have Tk,
so the runner's existing tool cache is checked before the default interpreter.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


MIN_VERSION = (3, 12)
MAX_VERSION = (3, 14)

PROBE = r'''
import json, os, platform, struct, subprocess, sys, sysconfig
machine = platform.machine()
host_machine = machine
translated = False
if sys.platform == 'darwin':
    def sysctl(name):
        result = subprocess.run(['/usr/sbin/sysctl', '-in', name],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ''
    translated = sysctl('sysctl.proc_translated') == '1'
    if sysctl('hw.optional.arm64') == '1':
        host_machine = 'arm64'
elif sys.platform == 'win32':
    host_machine = os.environ.get('PROCESSOR_ARCHITEW6432',
                                 os.environ.get('PROCESSOR_ARCHITECTURE', machine))
import tkinter, pip
window = tkinter.Tk()
try:
    window.withdraw()
    window.update_idletasks()
    tk_version = window.tk.call('package', 'present', 'Tk')
finally:
    window.destroy()
print(json.dumps({'executable': sys.executable, 'version': list(sys.version_info[:3]),
                  'machine': machine, 'host_machine': host_machine, 'translated': translated,
                  'implementation': platform.python_implementation(),
                  'free_threaded': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
                  'bits': struct.calcsize('P') * 8, 'tk': tk_version, 'pip': pip.__version__}))
'''


def normalize_arch(value):
    return {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'arm64'}.get(value.lower(), value.lower())


def version_key(directory):
    numbers = tuple(int(part) for part in re.findall(r'\d+', directory.name))
    # Prefer the oldest supported minor, with its newest available patch.
    return numbers[:2], tuple(-part for part in numbers[2:])


def discover_candidates(environ=None, platform_name=None, expected_arch=None):
    environ = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    expected_arch = normalize_arch(expected_arch or platform.machine())
    cache_arch = {'x86_64': 'x64', 'arm64': 'arm64'}.get(expected_arch, expected_arch)
    candidates = []
    for key in ('RUNNER_TOOL_CACHE', 'AGENT_TOOLSDIRECTORY'):
        if not environ.get(key):
            continue
        root = Path(environ[key]) / 'Python'
        for version in sorted(root.glob('3.*'), key=version_key):
            base = version / cache_arch
            candidates.extend([base / 'python.exe'] if platform_name == 'win32'
                              else [base / 'bin/python3', base / 'python'])
    if platform_name == 'darwin':
        framework = Path('/Library/Frameworks/Python.framework/Versions')
        for version in sorted(framework.glob('3.*'), key=version_key):
            candidates.append(version / 'bin/python3')
        for prefix in ('/opt/homebrew', '/usr/local'):
            for minor in range(MIN_VERSION[1], MAX_VERSION[1] + 1):
                candidates.append(Path(prefix) / ('opt/python@3.%s/bin/python3.%s' % (minor, minor)))
    if sys.executable:
        candidates.append(Path(sys.executable))
    for command in ('python3', 'python'):
        found = shutil.which(command, path=environ.get('PATH'))
        if found:
            candidates.append(Path(found))
    found, seen = [], set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)
    return found


def probe_python(executable):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    # Do not let another Python's configured stdlib invalidate an otherwise
    # healthy preinstalled interpreter. No package or user files are modified.
    environment.pop('PYTHONHOME', None)
    try:
        result = subprocess.run([str(executable), '-B', '-c', PROBE],
                                env=environment, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(str(error)) from error
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'probe exited %s' % result.returncode).strip())
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Interpreter probe did not return valid JSON') from error


def validate_report(details, expected_arch, minimum=MIN_VERSION, maximum=MAX_VERSION):
    if details['implementation'] != 'CPython' or details['free_threaded']:
        raise ValueError('Builds require standard CPython with the GIL enabled')
    version = tuple(details['version'][:2])
    if not minimum <= version <= maximum:
        raise ValueError('Python version is outside supported range %s.%s–%s.%s' % (*minimum, *maximum))
    machine = normalize_arch(details['machine'])
    host = normalize_arch(details['host_machine'])
    expected = normalize_arch(expected_arch)
    if details['translated'] or machine != host:
        raise ValueError('A native host/interpreter is required; Rosetta or emulation cannot verify this platform')
    if machine != expected or details.get('bits', 64) != 64:
        raise ValueError('Expected native 64-bit %s; found %s on %s' % (expected, machine, host))


def select_python(candidates, expected_arch, minimum=MIN_VERSION, maximum=MAX_VERSION):
    for candidate in candidates:
        try:
            details = probe_python(candidate)
            validate_report(details, expected_arch, minimum, maximum)
        except (RuntimeError, ValueError, KeyError) as error:
            print('Skipped %s: %s' % (candidate, error), file=sys.stderr)
            continue
        print('Selected preinstalled Python: ' + json.dumps(details, sort_keys=True), file=sys.stderr)
        return details
    raise RuntimeError('No compatible preinstalled Python with working Tk and pip; no interpreter was downloaded or installed')


def parse_version(value):
    if not re.fullmatch(r'3\.\d+', value):
        raise argparse.ArgumentTypeError('Use a major.minor version such as 3.12')
    return tuple(int(part) for part in value.split('.'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', action='append', help='Only probe this existing executable (repeatable)')
    parser.add_argument('--expected-arch', default=platform.machine())
    parser.add_argument('--min-version', type=parse_version, default=MIN_VERSION)
    parser.add_argument('--max-version', type=parse_version, default=MAX_VERSION)
    options = parser.parse_args()
    candidates = ([Path(shutil.which(value) or value) for value in options.candidate]
                  if options.candidate else discover_candidates(expected_arch=options.expected_arch))
    try:
        selected = select_python(candidates, options.expected_arch, options.min_version, options.max_version)
    except RuntimeError as error:
        parser.exit(1, str(error) + '\n')
    print(selected['executable'])


if __name__ == '__main__':
    main()
