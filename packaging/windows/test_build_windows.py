import importlib.util
import tempfile
import unittest
import subprocess
import json
import os
import runpy
import shutil
import sys
import time
import urllib.request
from pathlib import Path

PATH = Path(__file__).resolve().with_name('build_windows.py')

class BuildTest(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'requires native Windows PowerShell 5.1')
    def test_smoke_runner_preserves_process_exit_codes_and_redirected_output(self):
        with tempfile.TemporaryDirectory(prefix='img-switch-process-') as directory:
            work = Path(directory)
            child = work / 'child with spaces.py'
            child.write_text("import sys, time\nprint('child stdout')\nprint('child stderr', file=sys.stderr)\ntime.sleep(0.1)\nsys.exit(int(sys.argv[1]))\n", encoding='utf-8')
            script = work / 'check.ps1'
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            # Parse only the production helper; never run the installer smoke body.
            script.write_text("""$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'This regression requires Windows PowerShell 5.1' }
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(SMOKE_PATH, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$helper = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-CheckedProcess' }, $true)
if ($null -eq $helper) { throw 'Smoke process helper missing' }
Invoke-Expression $helper.Extent.Text
Invoke-CheckedProcess PYTHON_PATH @('-B', CHILD_ARGUMENT, '0') SUCCESS_LOG
$failure = ''
try { Invoke-CheckedProcess PYTHON_PATH @('-B', CHILD_ARGUMENT, '7') FAILURE_LOG }
catch { $failure = $_.Exception.Message }
if ($failure -notlike 'Process failed with exit code 7:*') { throw ('Incorrect failure exit: ' + $failure) }
Write-Output 'Verified actual process exit codes 0 and 7'
""".replace('SMOKE_PATH', quote(PATH.with_name('smoke.ps1')))
                .replace('PYTHON_PATH', quote(sys.executable))
                .replace('CHILD_ARGUMENT', quote('"' + str(child) + '"'))
                .replace('SUCCESS_LOG', quote(work / 'success'))
                .replace('FAILURE_LOG', quote(work / 'failure')), encoding='utf-8')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                    capture_output=True, text=True, errors='replace', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Verified actual process exit codes 0 and 7', result.stdout)
            for label in ('success', 'failure'):
                for stream in ('stdout', 'stderr'):
                    self.assertEqual((work / (label + '-' + stream + '.txt')).read_text(encoding='utf-8-sig').strip(), 'child ' + stream)

    def test_inno_inputs_remain_absolute_when_ci_uses_relative_script_entry(self):
        root = Path(__file__).resolve().parents[2]
        script = "import runpy; print(runpy.run_path('packaging/windows/test_build_windows.py')['PATH'])"
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=root,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(Path(result.stdout.strip()).is_absolute(),
                        'Inno resolves relative resource paths against setup.iss, not the CI working directory')

    def test_prerelease_name_is_separate_from_numeric_windows_versions(self):
        version = runpy.run_path(str(PATH.parents[2] / 'version.py'))
        setup = PATH.with_name('setup.iss').read_text(encoding='utf-8')
        for macro, key in (('AppVersion', 'VERSION'), ('NumericVersion', 'NUMERIC_VERSION'), ('BuildNumber', 'BUILD_NUMBER')):
            setup = setup.replace('{#' + macro + '}', version[key])
        values = dict(line.split('=', 1) for line in setup.splitlines() if '=' in line)
        expected = version['NUMERIC_VERSION'] + '.' + version['BUILD_NUMBER']
        for field in ('VersionInfoVersion', 'VersionInfoProductVersion'):
            self.assertRegex(values[field], r'^[0-9]+(?:\.[0-9]+){3}$', field + ' must be a numeric Windows version')
            self.assertEqual(values[field], expected)
        for field in ('AppVersion', 'VersionInfoTextVersion', 'VersionInfoProductTextVersion'):
            self.assertEqual(values[field], version['VERSION'])

    @unittest.skipUnless(os.name == 'nt', 'requires actual Windows Inno Setup compiler')
    def test_inno_compiles_prerelease_with_numeric_and_text_version_resources(self):
        root = PATH.parents[2]
        version = runpy.run_path(str(root / 'version.py'))
        iscc = shutil.which('iscc') or shutil.which('ISCC.exe')
        self.assertIsNotNone(iscc, 'native packaging checks require preinstalled Inno Setup')
        with tempfile.TemporaryDirectory(prefix='img-switch-inno-') as directory:
            work = Path(directory)
            # A valid small EXE is enough to compile all installer directives.
            # This fixture installer is never installed or executed.
            executable = work / 'IMG-Switch.exe'
            shutil.copyfile(sys.executable, executable)
            command = [iscc, '/DAppVersion=' + version['VERSION'],
                '/DNumericVersion=' + version['NUMERIC_VERSION'], '/DBuildNumber=' + version['BUILD_NUMBER'],
                '/DAppExe=' + str(executable), '/DProjectLicense=' + str(root / 'LICENSE'),
                '/DPythonLicense=' + str(Path(sys.base_prefix) / 'LICENSE.txt'),
                '/DThirdPartyLicenses=' + str(PATH.parent / 'licenses'),
                '/DRuntimeNotice=' + str(root / 'packaging/runtime/NOTICE.txt'),
                '/DSetupIcon=' + str(root / 'assets/img-switch.ico'), '/O' + str(work), str(PATH.with_name('setup.iss'))]
            result = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            installer = work / ('IMG-Switch-' + version['VERSION'] + '-Windows-x64-unsigned-setup.exe')
            self.assertTrue(installer.is_file())
            quoted = str(installer).replace("'", "''")
            script = "(Get-Item -LiteralPath '%s').VersionInfo | Select-Object ProductVersion,FileVersion,FileMajorPart,FileMinorPart,FileBuildPart,FilePrivatePart,ProductMajorPart,ProductMinorPart,ProductBuildPart,ProductPrivatePart | ConvertTo-Json -Compress" % quoted
            inspected = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                capture_output=True, text=True, errors='replace', timeout=15)
            self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
            metadata = json.loads(inspected.stdout)
            expected = [int(part) for part in (version['NUMERIC_VERSION'] + '.' + version['BUILD_NUMBER']).split('.')]
            for prefix in ('File', 'Product'):
                self.assertEqual([metadata[prefix + part + 'Part'] for part in ('Major', 'Minor', 'Build', 'Private')], expected)
                # Inno overwrites fixed-width, space-filled version resources.
                self.assertEqual(metadata[prefix + 'Version'].rstrip(' '), version['VERSION'])

    def test_local_mock_server_returns_image(self):
        script = PATH.with_name('mock_server.py')
        self.assertTrue(script.is_file(), 'local mock server missing')
        with tempfile.TemporaryDirectory() as tmp:
            port_file = Path(tmp) / 'port'
            process = subprocess.Popen([sys.executable, '-B', str(script), str(port_file)])
            try:
                deadline = time.monotonic() + 10
                while not port_file.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(port_file.exists(), 'mock server failed to start; exit=%r' % process.poll())
                port = port_file.read_text(encoding='ascii').strip()
                self.assertRegex(port, r'^[0-9]+$', 'mock ready port must not be empty or malformed')
                self.assertTrue(1 <= int(port) <= 65535, 'mock port is outside TCP range')
                request = urllib.request.Request('http://127.0.0.1:' + port + '/v1/images/generations',
                    data=json.dumps({'prompt': 'test', 'model': 'mock', 'size': '64x64'}).encode(),
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer dummy'}, method='POST')
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertTrue(json.load(response)['data'][0]['b64_json'])
                except Exception as error:
                    self.fail('Mock HTTP failed at %s; server exit=%r; %s: %s' %
                              (request.full_url, process.poll(), type(error).__name__, error))
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_audit_rejects_invalid_executable(self):
        spec = importlib.util.spec_from_file_location('windows_build', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'audit_executable'), 'build privacy audit is missing')
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / 'invalid.exe'
            invalid.write_bytes(b'invalid executable')
            with self.assertRaises(subprocess.CalledProcessError):
                module.audit_executable(invalid)

    def test_gui_includes_dynamic_engine_imports(self):
        spec = importlib.util.spec_from_file_location('windows_build', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue({'failovercfg', 'argparse', 'glob', 'uuid', 'base64',
                         'urllib.request', 'urllib.error', 'urllib.parse'} <= set(getattr(module, 'GUI_HIDDEN_IMPORTS', ())))

    def test_stage_includes_only_explicit_runtime_files(self):
        self.assertTrue(PATH.is_file(), 'Windows build script is missing')
        spec = importlib.util.spec_from_file_location('windows_build', PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root, stage = Path(tmp) / 'source', Path(tmp) / 'stage'
            for rel in module.ENGINE_FILES:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(rel, encoding='utf-8')
            (root / 'engine/config.json').write_text('PRIVATE', encoding='utf-8')
            (root / 'engine/config.json.tmp').write_text('PRIVATE', encoding='utf-8')
            module.stage_engine(root, stage)
            self.assertEqual({str(p.relative_to(stage)).replace('\\', '/') for p in stage.rglob('*') if p.is_file()}, set(module.ENGINE_FILES))

if __name__ == '__main__':
    unittest.main()
