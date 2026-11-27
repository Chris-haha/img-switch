#!/usr/bin/env python3
"""Source directory deliveries preserve an allowlist and never write archives."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
import importlib.util
from unittest import mock


PROJECT = Path(__file__).resolve().parents[2]
BUILDER = PROJECT / "packaging" / "source" / "build_source.py"
SPEC = importlib.util.spec_from_file_location('source_builder', BUILDER)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
SENTINEL = b"DO-" + b"NOT-" + b"PUBLISH"


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "snapshot"
        for name, data in builder.collect(PROJECT):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name in ("engine/config.json", "engine/config.json.tmp", ".env", "._README.md", "packaging/macos/secret.key"):
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(SENTINEL)

    def build(self, output):
        return subprocess.run([sys.executable, str(BUILDER), "--root", str(self.project), "--output", str(output)],
                              capture_output=True, text=True, timeout=30)

    def test_deterministic_allowlisted_files_and_metadata(self):
        first, second = self.base / "one", self.base / "two"
        r1, r2 = self.build(first), self.build(second)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        contents = lambda root: {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        delivered = contents(first)
        self.assertEqual(delivered, contents(second))
        self.assertEqual(set(delivered), set(builder.FILES))
        self.assertFalse(any(SENTINEL in data for data in delivered.values()))
        self.assertFalse(any(zipfile.is_zipfile(p) for p in first.rglob('*') if p.is_file()))

    def test_existing_output_is_never_overwritten(self):
        output = self.base / "existing-output"
        output.write_bytes(b"do-not-overwrite")
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), b"do-not-overwrite")

    @unittest.skipIf(os.name == "nt", "symlinks may require Windows privileges")
    def test_rejects_symlinked_allowlisted_file(self):
        readme = self.project / "README.md"
        readme.unlink()
        readme.symlink_to(self.base / "outside")
        (self.base / "outside").write_text("PRIVATE", encoding="utf-8")
        output = self.base / "delivery"
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_zip_requires_explicit_format_without_creating_archive(self):
        with mock.patch.object(builder, 'create_archive') as archive:
            with mock.patch.object(builder, 'create_directory') as directory:
                with mock.patch.object(sys, 'argv', ['build_source', '--output', 'reviewed-output', '--format', 'zip']):
                    with mock.patch('builtins.print'):
                        self.assertEqual(builder.main(), 0)
                archive.assert_called_once()
                directory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
