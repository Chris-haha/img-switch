"""Extraction safety checks; fixtures never create ZIP archives."""
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest

spec = importlib.util.spec_from_file_location('prepare_tools', Path(__file__).parent / 'packaging/prepare_tools.py')
prepare_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_tools)


class Entries:
    def __init__(self, content):
        self.content = content
    def infolist(self):
        return [types.SimpleNamespace(filename=name, external_attr=0, is_dir=lambda: False) for name in self.content]
    def read(self, entry):
        return self.content[entry.filename]


class ExtractionTests(unittest.TestCase):
    def test_archive_entry_is_rejected_before_any_files_written(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                prepare_tools.extract_files(Entries({'normal.py': b'a=1', 'nested.zip': b'not a zip fixture'}), directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_parent_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                prepare_tools.extract_files(Entries({'../outside.py': b'bad'}), directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_regular_files_are_installed_without_overwriting_existing_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_tools.extract_files(Entries({'module/__init__.py': b'a=1'}), directory)
            path = Path(directory) / 'module/__init__.py'
            self.assertEqual(path.read_bytes(), b'a=1')
            with self.assertRaises(ValueError):
                prepare_tools.extract_files(Entries({'module/__init__.py': b'a=2'}), directory)
            self.assertEqual(path.read_bytes(), b'a=1')


if __name__ == '__main__':
    unittest.main()
