"""Extraction safety checks; fixtures never create ZIP archives."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

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
    def test_dependency_exclusion_selects_latest_compatible_wheel(self):
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.tags import Tag
        entries = {
            '2024.8.26': [{'filename': 'pefile-2024.8.26-py3-none-any.whl'}],
            '2023.2.7': [{'filename': 'pefile-2023.2.7-py3-none-any.whl'}],
            '2022.5.30': [{'filename': 'pefile-2022.5.30-py3-none-any.whl'}],
        }
        index = {'info': {'version': '2024.8.26'}, 'urls': entries['2024.8.26'], 'releases': entries}
        selected = {'info': {'version': '2023.2.7'}, 'urls': entries['2023.2.7']}
        with mock.patch.object(prepare_tools, 'read_url', side_effect=[json.dumps(index).encode(), json.dumps(selected).encode()]) as read:
            document, wheel = prepare_tools.select_document(Requirement('pefile!=2024.8.26,>=2022.5.30'), {Tag('py3', 'none', 'any'): 0})
        self.assertEqual(document['info']['version'], '2023.2.7')
        self.assertEqual(wheel['filename'], 'pefile-2023.2.7-py3-none-any.whl')
        self.assertEqual(read.call_args.args[0], 'https://pypi.org/pypi/pefile/2023.2.7/json')

    def test_yanked_or_wrong_python_wheels_do_not_win(self):
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.tags import Tag
        files = [{'filename': 'example-2.0-py3-none-any.whl', 'yanked': True},
                 {'filename': 'example-2.0-py3-none-any.whl', 'requires_python': '>=99'}]
        index = {'info': {'version': '2.0'}, 'urls': files,
                 'releases': {'2.0': files, '1.0': [{'filename': 'example-1.0-py3-none-any.whl'}]}}
        selected = {'info': {'version': '1.0'}, 'urls': index['releases']['1.0']}
        with mock.patch.object(prepare_tools, 'read_url', side_effect=[json.dumps(index).encode(), json.dumps(selected).encode()]):
            document, _wheel = prepare_tools.select_document(Requirement('example>=1'), {Tag('py3', 'none', 'any'): 0})
        self.assertEqual(document['info']['version'], '1.0')

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
