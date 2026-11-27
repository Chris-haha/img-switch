"""Release regressions that never create ZIPs or run a native build."""
import importlib.util
import marshal
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent


def module(relative):
    spec = importlib.util.spec_from_file_location('audit_' + relative.replace('/', '_'), ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def bytecode(filename='module.py', source='value = 1'):
    return importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(compile(source, filename, 'exec'))


def carchive(audit, entries):
    data, table = bytearray(), bytearray()
    for name, kind, raw in entries:
        encoded = name.encode() + b'\0'
        table += audit.ENTRY.pack(audit.ENTRY.size + len(encoded), len(data), len(raw), len(raw), 0, kind) + encoded
        data += raw
    size = len(data) + len(table) + audit.COOKIE.size
    return bytes(data + table) + audit.COOKIE.pack(audit.COOKIE_MAGIC, size, len(data), len(table),
                                                 sys.version_info.major * 100 + sys.version_info.minor, b'python')


class NoZipPackagingTests(unittest.TestCase):
    def test_default_source_delivery_is_complete_directory_without_zip_writer(self):
        build = module('packaging/source/build_source.py')
        required = {'gui_tasks.py', 'version.py', 'test_reliability.py', 'test_engine_jobs.py',
                    'test_gui_workflows.py', 'test_secret_redaction.py', 'test_packaging_nozip.py',
                    'test-fixtures/valid-64.jpg', 'engine/scripts/task_store.py', 'engine/scripts/image_decode.py',
                    'engine/scripts/jpeg_entropy.py', 'test_image_decode.py',
                    'test-fixtures/imageio-color-progressive.jpg', 'test-fixtures/imageio-gray-progressive.jpg'}
        self.assertFalse(required - set(build.FILES), 'source delivery omits runtime modules or regression inputs')
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source', Path(temp) / 'delivery'
            for name in build.FILES:
                p = source / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'allowlisted')
            (source / 'engine/config.json').write_bytes(b'PRIVATE')
            with mock.patch.object(build.zipfile, 'ZipFile', side_effect=AssertionError('ZIP creation forbidden')):
                with mock.patch.object(sys, 'argv', ['build_source', '--root', str(source), '--output', str(output)]):
                    self.assertEqual(build.main(), 0)
            self.assertTrue(output.is_dir())
            self.assertEqual({p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}, set(build.FILES))
            with self.assertRaises(FileExistsError):
                build.create_directory(source, output)
            self.assertEqual((output / 'app.py').read_bytes(), b'allowlisted')

    def test_source_symlink_rejected_before_destination_created(self):
        build = module('packaging/source/build_source.py')
        self.assertTrue(hasattr(build, 'create_directory'))
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'source', Path(temp) / 'delivery'
            source.mkdir()
            (source / 'outside').write_bytes(b'PRIVATE')
            if sys.platform == 'win32':
                self.skipTest('symlink privileges differ on Windows')
            (source / 'app.py').symlink_to(source / 'outside')
            with mock.patch.object(build, 'FILES', ('app.py',)):
                with self.assertRaises(ValueError):
                    build.create_directory(source, output)
            self.assertFalse(output.exists())

    def test_staging_contains_transitive_runtime_modules(self):
        expected = {'engine/scripts/task_store.py', 'engine/scripts/image_decode.py'}
        mac = module('packaging/macos/stage.py')
        win = module('packaging/windows/build_windows.py')
        self.assertTrue(expected | {'gui_tasks.py', 'version.py'} <= set(mac.REQUIRED))
        self.assertTrue(expected <= set(win.ENGINE_FILES))
        self.assertTrue({'gui_tasks.py', 'version.py'} <= set(win.APP_FILES))
        with tempfile.TemporaryDirectory() as temp:
            source, dest = Path(temp) / 'source', Path(temp) / 'dest'
            for name in win.APP_FILES:
                p = source / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('runtime')
            win.stage_app(source, dest)
            self.assertEqual({p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file()}, set(win.APP_FILES))

    def test_loose_pyc_sanitized_without_mutating_source_or_creating_zip(self):
        sanitize = module('packaging/runtime/sanitize.py')
        doc = "\n        splitroot('C:/Users/test-user') == ('C:', '/', 'Users/test-user')\n"
        original = bytecode(source='text = ' + repr(doc))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / 'ntpath.pyc'
            src.write_bytes(original)
            with mock.patch.object(sanitize.zipfile, 'ZipFile', side_effect=AssertionError('ZIP creation forbidden')):
                result = sanitize.scrub_datas([('ntpath.pyc', str(src), 'DATA')], root / 'copy')
            self.assertEqual(src.read_bytes(), original)
            copied = Path(result[0][1]).read_bytes()
            self.assertNotIn(b'/Users/', copied)
            marshal.loads(copied[16:])

    def test_windows_scans_loose_pyc_code_filenames(self):
        audit = module('packaging/windows/audit_privacy.py')
        empty_pyz = b'PYZ\0' + b'0000' + struct.pack('!i', 12) + marshal.dumps([])
        data = carchive(audit, [('PYZ.pyz', b'z', empty_pyz),
                              ('module.pyc', b'x', bytecode('/build/private/source.py'))])
        scanner = audit.Scanner()
        scanner.archive(data, False)
        self.assertGreater(scanner.counts['absolute_code_path'], 0)

    def test_macos_scans_loose_pyc_code_filenames(self):
        audit = module('packaging/macos/audit_privacy.py')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'App.app'
            contents = root / 'Contents/Resources'
            contents.mkdir(parents=True)
            (contents / 'module.pyc').write_bytes(bytecode('/build/private/source.py'))
            scanner = audit.Scanner()
            with mock.patch.object(scanner, 'pyinstaller'):
                scanner.app(root)
            self.assertGreater(scanner.counts['absolute_code_path'], 0)

    def test_builds_use_noarchive_and_release_transport_never_creates_zip(self):
        mac = (ROOT / 'packaging/macos/build.sh').read_text()
        win = (ROOT / 'packaging/windows/build_windows.py').read_text()
        workflow = (ROOT / '.github/workflows/test-installers.yml').read_text()
        checks = (ROOT / 'packaging/ci_checks.py').read_text()
        self.assertEqual(mac.count('--debug noarchive'), 2)
        self.assertIn("'--debug', 'noarchive'", win)
        self.assertNotIn('actions/upload-artifact', workflow)
        self.assertNotIn('actions/download-artifact', workflow)
        self.assertNotIn('gh release', workflow)
        self.assertNotIn('source.zip', workflow)
        for test in ('test_packaging_nozip.py', 'test_gui_workflows.py', 'test_engine_jobs.py', 'test_secret_redaction.py'):
            self.assertIn(test, workflow + checks)
        self.assertIn('needs: [source, windows, macos]', workflow)
        self.assertIn('codex/release-', workflow)

    def test_version_metadata_is_injected_before_bundle_creation(self):
        versions = module('packaging/runtime/release_version.py')
        version = {'VERSION': '1.2.3-beta.4', 'NUMERIC_VERSION': '1.2.3', 'BUILD_NUMBER': '4'}
        with tempfile.TemporaryDirectory() as temp:
            spec = Path(temp) / 'app.spec'
            spec.write_text("app = BUNDLE('payload', name='IMG Switch.app')\n")
            versions.patch_bundle(spec, version)
            captured = {}
            def bundle(*args, **kwargs):
                captured.update(kwargs)
            exec(compile(spec.read_text(), 'app.spec', 'exec'), {'BUNDLE': bundle})
            self.assertEqual(captured['info_plist'], {'CFBundleShortVersionString': '1.2.3',
                                                    'CFBundleVersion': '4', 'IMGSwitchVersion': '1.2.3-beta.4'})

    def test_archive_enabled_spec_is_rejected_before_pyinstaller_runs(self):
        sanitize = module('packaging/runtime/sanitize.py')
        with tempfile.TemporaryDirectory() as temp:
            spec = Path(temp) / 'bad.spec'
            spec.write_text('a = Analysis([], noarchive=False)\npyz = PYZ(a.pure)\n')
            before = spec.read_bytes()
            with self.assertRaises(ValueError):
                sanitize.patch_spec(spec, Path(temp) / 'runtime')
            self.assertEqual(spec.read_bytes(), before)

    def test_sysconfig_pyc_relocates_private_prefix_only_in_staged_copy(self):
        sanitize = module('packaging/runtime/sanitize.py')
        prefix = '/Users/buildperson/private/python'
        values = {'prefix': prefix, 'BINDIR': prefix + '/bin', 'DESTDIRS': prefix + '/bin ' + prefix + '/lib',
                  'SOABI': 'cpython-314-darwin', 'SIZEOF_VOID_P': 8, 'FLAGS': ('-O3', '-fPIC')}
        original = bytecode('_sysconfigdata__darwin_darwin.py', 'build_time_vars = ' + repr(values))
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'sysconfig.pyc'
            source.write_bytes(original)
            with mock.patch.object(sanitize.sys, 'prefix', prefix):
                result = sanitize.scrub_datas([('_sysconfigdata__darwin_darwin.pyc', str(source), 'DATA')], Path(temp) / 'copy')
            self.assertEqual(source.read_bytes(), original)
            sanitized = Path(result[0][1]).read_bytes()
            self.assertNotIn(b'/Users/', sanitized)
            namespace = {}
            with mock.patch.object(sys, '_MEIPASS', '/runtime/bundle', create=True):
                exec(marshal.loads(sanitized[16:]), namespace)
            actual = namespace['build_time_vars']
            self.assertEqual(actual['BINDIR'], '/runtime/bundle/bin')
            self.assertEqual(actual['DESTDIRS'], '/runtime/bundle/bin /runtime/bundle/lib')
            for key in ('SOABI', 'SIZEOF_VOID_P', 'FLAGS'):
                self.assertEqual(actual[key], values[key])
            self.assertEqual(actual.keys(), values.keys())

    def test_build_cache_is_task_local(self):
        mac = (ROOT / 'packaging/macos/build.sh').read_text()
        win = (ROOT / 'packaging/windows/build_windows.py').read_text()
        self.assertIn('export PYINSTALLER_CONFIG_DIR="$work/pyinstaller-cache"', mac)
        self.assertIn('PYINSTALLER_CONFIG_DIR', win)
        self.assertIn('env=build_env', win)


if __name__ == '__main__':
    unittest.main()
