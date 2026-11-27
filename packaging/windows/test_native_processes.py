"""Actual Windows process-tree termination; never substitutes mocked taskkill."""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'engine/scripts')]
from native_processes import descendants, kill_tree, process_snapshot, wait_for


def spawn_descendant(ready):
    child = subprocess.Popen([sys.executable, '-B', __file__, '--leaf'])
    Path(ready).write_text(json.dumps({'parent': os.getpid(), 'child': child.pid}), encoding='utf-8')
    time.sleep(90)


@unittest.skipUnless(os.name == 'nt', 'requires actual Windows taskkill and process enumeration')
class NativeProcessTreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='img-switch-native-')
        self.addCleanup(self.temporary.cleanup)
        self.ready = Path(self.temporary.name) / 'ready.json'

    def assert_tree_exits(self, pids):
        wait_for(lambda: not (set(pids) & set(process_snapshot())), timeout=8,
                 description='all owned process descendants to exit')

    def test_engine_stop_worker_reaps_real_grandchild(self):
        import failover_imagegen_tool as engine
        process = multiprocessing.get_context('spawn').Process(target=spawn_descendant, args=(str(self.ready),))
        process.start()
        self.addCleanup(lambda: kill_tree(process.pid) if process.is_alive() else None)
        wait_for(self.ready.exists, description='request worker and child to start')
        pids = json.loads(self.ready.read_text())
        self.assertIn(pids['child'], descendants(process.pid))
        engine._stop_worker(process)
        self.assertFalse(process.is_alive())
        self.assert_tree_exits(pids.values())

    def test_controller_close_reaps_uncooperative_real_tree(self):
        import gui_tasks
        jobs = Path(self.temporary.name) / 'jobs'
        controller = gui_tasks.TaskController(None,
            [sys.executable, '-B', __file__, '--parent', str(self.ready)], dict(os.environ), lambda: None, jobs)
        task_id = controller.generate('local', 'mock', '64x64', 60, 'local test')
        process = controller.processes[task_id]
        self.addCleanup(lambda: kill_tree(process.pid) if process.poll() is None else None)
        wait_for(self.ready.exists, description='uncooperative engine and child to start')
        pids = json.loads(self.ready.read_text())
        self.assertIn(pids['child'], descendants(process.pid))
        # The fixture acknowledges cancel without exiting, forcing the production
        # controller's taskkill /T fallback after its grace period.
        (jobs / (task_id + '.json')).write_text('{}', encoding='utf-8')
        controller.close()
        self.assertEqual(controller.cancellation(task_id)['status'], 'requested')
        self.assertIsNotNone(process.poll())
        self.assert_tree_exits(pids.values())


if __name__ == '__main__':
    multiprocessing.freeze_support()
    if '--leaf' in sys.argv:
        time.sleep(90)
    elif '--parent' in sys.argv:
        if 'cancel' not in sys.argv:
            spawn_descendant(sys.argv[sys.argv.index('--parent') + 1])
    else:
        unittest.main()
