"""A closed configuration window must be safe to collect outside the UI thread."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCENARIO = r'''
import gc
import os
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from unittest import mock

sys.path.insert(0, str(Path(sys.argv[1]) / 'engine/scripts'))
import setup
import failovercfg

# Keep unreachable Tk reference cycles until the explicit worker collection.
# This makes the crash deterministic instead of depending on a later HTTP thread.
gc.disable()
with tempfile.TemporaryDirectory(prefix='img-switch-legacy-gui-') as directory:
    path = Path(directory) / 'config.json'
    failovercfg.save_config({'sources': []}, str(path))
    original = path.read_bytes()
    def mainloop(window):
        window.update()
        window.destroy()
    with mock.patch.dict(os.environ, {failovercfg.CONFIG_ENV: str(path)}), \
         mock.patch.object(tk.Tk, 'mainloop', mainloop):
        setup.cmd_gui(None)
    assert path.read_bytes() == original, 'closing the GUI changed configuration'

worker = threading.Thread(target=gc.collect)
worker.start()
worker.join(30)
assert not worker.is_alive(), 'worker collection did not finish'
print('legacy window closed and worker collection completed')
'''


class LegacyGuiLifecycleTests(unittest.TestCase):
    def test_closed_window_survives_worker_garbage_collection(self):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        environment.pop('FAILOVER_IMAGEGEN_TOOL_GUI_SMOKE', None)
        environment.pop('FAILOVER_IMAGEGEN_TOOL_GUI_SELFTEST', None)
        result = subprocess.run([sys.executable, '-B', '-c', SCENARIO, str(ROOT)],
                                env=environment, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('legacy window closed and worker collection completed', result.stdout)
        self.assertNotIn('Exception ignored', result.stderr)
        self.assertNotIn('invalid command name', result.stderr)


if __name__ == '__main__':
    unittest.main()
