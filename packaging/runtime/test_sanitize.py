import importlib.util
from pathlib import Path
import unittest
import tempfile
import zipfile
import subprocess
import sys
import ctypes
from unittest import mock

spec = importlib.util.spec_from_file_location('sanitize', Path(__file__).with_name('sanitize.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class SanitizeTests(unittest.TestCase):
    def test_framework_symlink_metadata_is_preserved_without_opening_its_target(self):
        entries = [('Python', 'Python.framework/Versions/3.12/Python', 'SYMLINK'),
                   ('Python.framework/Versions/Current', '3.12', 'SYMLINK')]
        with tempfile.TemporaryDirectory() as directory:
            for scrub in (module.scrub_binaries, module.scrub_datas):
                with self.subTest(scrub=scrub.__name__):
                    self.assertEqual(scrub(entries, Path(directory) / scrub.__name__), entries)

    def test_missing_regular_runtime_data_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                module.scrub_datas([('missing.pyc', str(Path(directory) / 'missing.pyc'), 'DATA')],
                                   Path(directory) / 'copy')

    def test_only_cpython_documentation_example_is_removed_without_size_change(self):
        line = b"        splitroot('C:/Users/test-user') == ('C:', '/', 'Users/test-user')\n"
        data = b'before\x00\n' + line + b'\x00after'
        output, count = module.remove_example(data)
        self.assertEqual(count, 1)
        self.assertEqual(len(output), len(data))
        self.assertTrue(output.startswith(b'before\x00'))
        self.assertTrue(output.endswith(b'\x00after'))
        self.assertNotIn(b'/Users/', output)

    def test_unrelated_path_is_not_rewritten(self):
        data = b'/Users/test-user/private'
        self.assertEqual(module.remove_example(data), (data, 0))

    def test_loose_standard_library_bytecode_is_copied_and_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'ntpath.pyc'
            line = b"        splitroot('C:/Users/test-user') == ('C:', '/', 'Users/test-user')\n"
            original.write_bytes(b'header\n' + line)
            before = original.read_bytes()
            result = module.scrub_datas([('ntpath.pyc', str(original), 'DATA')], root / 'copy')
            self.assertEqual(original.read_bytes(), before)
            self.assertNotIn(b'/Users/', Path(result[0][1]).read_bytes())

    def test_unknown_spec_structure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / 'app.spec'
            spec.write_text('unexpected', encoding='utf-8')
            with self.assertRaises(ValueError):
                module.patch_spec(spec, Path(tmp) / 'runtime')

    def test_spec_scrubs_bootstrap_dependencies_before_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / 'app.spec'
            spec.write_text('a = Analysis([], noarchive=True)\npyz = PYZ(a.pure)\nexe = EXE(pyz)\n', encoding='utf-8')
            module.patch_spec(spec, Path(tmp) / 'runtime')
            text = spec.read_text(encoding='utf-8')
            self.assertLess(text.index('pyz = PYZ(a.pure)'), text.index('pyz.dependencies ='))
            self.assertLess(text.index('pyz.dependencies ='), text.index('exe = EXE('))

    def test_framework_python_binary_is_copied_and_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'Python'
            line = b"        splitroot('C:/Users/test-user') == ('C:', '/', 'Users/test-user')\n"
            original.write_bytes(b'header\n' + line)
            before = original.read_bytes()
            with mock.patch.object(module.subprocess, 'run'), mock.patch.object(module, 'scrub_cpython_source_paths'):
                result = module.scrub_binaries([('Python', str(original), 'BINARY')], root / 'copy')
            self.assertEqual(original.read_bytes(), before)
            self.assertFalse(b'/Users/' in Path(result[0][1]).read_bytes())

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS clang and Mach-O')
    def test_strips_only_extension_modules_not_nested_engine(self):
        with tempfile.TemporaryDirectory(prefix='sanitize-debug-') as tmp:
            root = Path(tmp)
            source, binary, engine = root / 'module.c', root / 'module.so', root / 'img-switch-engine'
            source.write_text('int sample(void) { return 42; }\n', encoding='utf-8')
            subprocess.run(['cc', '-dynamiclib', '-g', '-fdebug-prefix-map=' + tmp + '=/Users/example-user/work', str(source), '-o', str(binary)], check=True, capture_output=True)
            engine.write_bytes(b'engine-archive')
            before = binary.read_bytes()
            self.assertIn(b'/Users/', before)
            result = module.scrub_binaries([('lib-dynload/module.so', str(binary), 'BINARY'),
                                           ('engine/bin/img-switch-engine', str(engine), 'BINARY')], root / 'copy')
            self.assertEqual(binary.read_bytes(), before)
            self.assertFalse(b'/Users/' in Path(result[0][1]).read_bytes())
            self.assertEqual(result[1][1], str(engine))

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS clang and Mach-O')
    def test_scrubs_cpython_source_file_diagnostic_without_changing_loadability(self):
        with tempfile.TemporaryDirectory(prefix='sanitize-file-') as tmp:
            root = Path(tmp)
            source, binary = root / 'sample.c', root / 'sample.so'
            source.write_text('const char *sample(void) { return __FILE__; }\n', encoding='utf-8')
            prefix = '/Users/example-user/build/v3.12.10/Modules'
            subprocess.run(['cc', '-dynamiclib', '-ffile-prefix-map=' + tmp + '=' + prefix,
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            before = binary.read_bytes()
            self.assertTrue(b'/Users/' in before)
            before_load_commands = subprocess.check_output(['otool', '-L', str(binary)], text=True).splitlines()[1:]
            result = module.scrub_binaries([('lib-dynload/sample.so', str(binary), 'BINARY')], root / 'copy')
            output = Path(result[0][1])
            self.assertEqual(binary.read_bytes(), before)
            self.assertFalse(b'/Users/' in output.read_bytes())
            self.assertEqual(before_load_commands, subprocess.check_output(['otool', '-L', str(output)], text=True).splitlines()[1:])
            subprocess.run(['codesign', '-s', '-', '-f', str(output)], check=True, capture_output=True)
            lib = ctypes.CDLL(str(output))
            lib.sample.restype = ctypes.c_char_p
            self.assertTrue(lib.sample().endswith(b'Modules/sample.c'))

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS clang and Mach-O')
    def test_framework_vpath_is_neutralized_only_in_string_section(self):
        with tempfile.TemporaryDirectory(prefix='sanitize-vpath-') as tmp:
            root = Path(tmp)
            source, binary = root / 'sample.c', root / 'Python'
            source.write_text('__attribute__((section("__TEXT,__cstring"))) const char entries[] = "VPATH\\0/Users/example-user/build/v3.12.10\\0PLATLIBDIR\\0";\n', encoding='utf-8')
            subprocess.run(['cc', '-dynamiclib', str(source), '-o', str(binary)], check=True, capture_output=True)
            self.assertTrue(b'/Users/' in binary.read_bytes())
            self.assertEqual(module.scrub_cpython_source_paths(binary, replace_vpath=True), 1)
            self.assertFalse(b'/Users/' in binary.read_bytes())
            subprocess.run(['codesign', '-s', '-', '-f', str(binary)], check=True, capture_output=True)
            ctypes.CDLL(str(binary))

if __name__ == '__main__':
    unittest.main()
