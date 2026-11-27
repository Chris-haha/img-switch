"""Interpreter selection checks using ordinary files, never downloaded archives."""
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name('select_python.py')
spec = importlib.util.spec_from_file_location('select_python', SOURCE)
selector = importlib.util.module_from_spec(spec) if SOURCE.exists() else None
if selector is not None:
    spec.loader.exec_module(selector)


def report(**overrides):
    result = {'executable': '/existing/python3', 'version': [3, 12, 10], 'bits': 64,
              'machine': 'x86_64', 'host_machine': 'x86_64', 'translated': False,
              'implementation': 'CPython', 'free_threaded': False,
              'tk': '8.6.16', 'pip': '25.0'}
    result.update(overrides)
    return result


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(selector, 'preinstalled Python selector is missing')

    def test_cached_interpreters_are_discovered_in_supported_version_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in ('3.14.0', '3.12.9', '3.12.10', '3.13.1'):
                candidate = root / 'Python' / version / 'x64/bin/python3'
                candidate.parent.mkdir(parents=True)
                candidate.write_text('existing interpreter')
            with mock.patch.object(selector.sys, 'executable', ''), \
                 mock.patch.object(selector.shutil, 'which', return_value=None):
                found = selector.discover_candidates(
                    {'RUNNER_TOOL_CACHE': str(root)}, platform_name='linux', expected_arch='x86_64')
            self.assertEqual([path.parts[-4] for path in found],
                             ['3.12.10', '3.12.9', '3.13.1', '3.14.0'])

    def test_windows_cache_uses_python_exe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'Python/3.12.10/x64/python.exe'
            python.parent.mkdir(parents=True)
            python.write_text('existing interpreter')
            found = selector.discover_candidates(
                {'RUNNER_TOOL_CACHE': str(root)}, platform_name='win32', expected_arch='x86_64')
            self.assertIn(python.resolve(), found)

    def test_rejects_rosetta_even_when_machine_reports_x86_64(self):
        with self.assertRaisesRegex(ValueError, 'native|Rosetta'):
            selector.validate_report(report(host_machine='arm64', translated=True), 'x86_64')

    def test_rejects_wrong_architecture_and_unsupported_python(self):
        for details in (report(machine='arm64', host_machine='arm64'),
                        report(version=[3, 11, 9]), report(version=[3, 15, 0]),
                        report(free_threaded=True), report(implementation='PyPy'), report(bits=32)):
            with self.subTest(details=details), self.assertRaises(ValueError):
                selector.validate_report(details, 'x86_64')

    def test_accepts_windows_architecture_name(self):
        selector.validate_report(report(machine='AMD64', host_machine='AMD64'), 'x86_64')

    def test_selection_falls_back_after_missing_tk(self):
        with mock.patch.object(selector, 'probe_python', side_effect=[
                RuntimeError('No module named tkinter'), report()]), \
             mock.patch.object(selector.sys, 'stderr', new_callable=io.StringIO) as errors:
            selected = selector.select_python([Path('/no-tk/python'), Path('/existing/python3')], 'x86_64')
        self.assertEqual(selected['executable'], '/existing/python3')
        self.assertIn('No module named tkinter', errors.getvalue())

    def test_total_failure_is_explicit_and_never_installs(self):
        with mock.patch.object(selector, 'probe_python', side_effect=RuntimeError('no Tk')), \
             mock.patch.object(selector.sys, 'stderr', new_callable=io.StringIO):
            with self.assertRaisesRegex(RuntimeError, 'No compatible preinstalled Python'):
                selector.select_python([Path('/no-tk/python')], 'x86_64')

    def test_probe_disables_bytecode_and_rejects_child_failure(self):
        failed = types.SimpleNamespace(returncode=1, stdout='', stderr='Tk initialization failed')
        with mock.patch.object(selector.subprocess, 'run', return_value=failed) as run:
            with self.assertRaisesRegex(RuntimeError, 'Tk initialization failed'):
                selector.probe_python(Path('/existing/python3'))
        args, options = run.call_args
        self.assertEqual(args[0][:2], ['/existing/python3', '-B'])
        self.assertEqual(options['env']['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertNotIn('shell', options)

    def test_native_probe_creates_tk_and_reports_process_translation(self):
        # Execute the production probe with local fake Tk/pip modules so this
        # regression also runs on CI hosts without a display or installed Tk.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tkinter.py').write_text(
                'class Tk:\n'
                ' def __init__(self): self.tk = self\n'
                ' def withdraw(self): pass\n'
                ' def update_idletasks(self): pass\n'
                ' def call(self, *args): return "8.6.test"\n'
                ' def destroy(self): pass\n')
            (root / 'pip.py').write_text('__version__ = "test"\n')
            with mock.patch.dict(selector.os.environ, {'PYTHONPATH': str(root)}):
                result = selector.probe_python(Path(sys.executable))
        self.assertEqual(result['tk'], '8.6.test')
        self.assertEqual(result['pip'], 'test')
        self.assertIsInstance(result['translated'], bool)
        self.assertEqual(result['version'], list(sys.version_info[:3]))


if __name__ == '__main__':
    unittest.main()
