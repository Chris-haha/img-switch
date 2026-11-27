"""Ensure macOS packaging rejects an unsuitable Python before creating output."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipIf(sys.platform == 'win32', 'macOS build entry uses a POSIX shell')
class BuildPreflightTests(unittest.TestCase):
    def test_selector_failure_prevents_build_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'fake-python'
            python.write_text(
                '#!/bin/sh\n'
                'case "$*" in\n'
                ' *select_python.py*) echo "native Tk probe rejected" >&2; exit 42;;\n'
                ' *platform.machine*) echo x86_64; exit 0;;\n'
                ' *) exit 41;;\n'
                'esac\n')
            python.chmod(0o755)
            output = root / 'output'
            result = subprocess.run(['bash', str(Path(__file__).with_name('build.sh')), str(output)],
                                    env=dict(os.environ, PYTHON=str(python)),
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertIn('native Tk probe rejected', result.stderr)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
