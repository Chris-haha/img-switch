#!/usr/bin/env python3
"""Accept the *installed* Windows binaries using native observations and loopback HTTP.

The controlling Python is invoked by absolute path. Every application/engine child
inherits a PATH containing only Windows system tools, never a Python installation.
This is Windows runtime evidence; running portable unit tests is not a substitute.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid

from native_processes import close_window, descendants, kill_tree, process_snapshot, visible_windows, wait_for


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Acceptance:
    def __init__(self, args):
        require(os.name == 'nt' and sys.maxsize > 2**32, 'Run this acceptance on native 64-bit Windows')
        self.args = args
        self.work, self.app, self.evidence = args.work.resolve(), args.app.resolve(), args.evidence.resolve()
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.report_path = self.evidence / 'windows-native-smoke.json'
        self.report = json.loads(self.report_path.read_text(encoding='utf-8')) if self.report_path.exists() else {
            'platform': platform.platform(), 'machine': platform.machine(),
            'python_controller': sys.version.split()[0], 'checks': [], 'status': 'running',
            'network': 'loopback mock only; no paid provider API',
        }
        self.home = Path(os.environ['FAILOVER_IMAGEGEN_APP_HOME'])
        self.config_dir = Path(os.environ['APPDATA']) / 'failover-imagegen-tool'
        for path in (self.home, self.config_dir, self.app):
            require(path.resolve().is_relative_to(self.work),
                    'Refusing to touch a home/config/application outside the isolated smoke directory: ' + str(path))
        self.config, self.settings = self.config_dir / 'config.json', self.config_dir / 'app-settings.json'
        self.jobs = self.config_dir / 'jobs'
        self.env = dict(os.environ, FAILOVER_IMAGEGEN_TOOL_CONFIG=str(self.config),
                        FAILOVER_IMAGEGEN_JOBS_DIR=str(self.jobs))
        for name in ('PYTHONHOME', 'PYTHONPATH', 'FAILOVER_IMAGEGEN_APP_SMOKE', 'FAILOVER_IMAGEGEN_APP_SELFTEST'):
            self.env.pop(name, None)
        self.sequence = 0

    def save(self):
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def passed(self, name, **evidence):
        self.report['checks'].append({'name': name, 'status': 'passed', **evidence})
        self.save()
        print('PASS: ' + name, flush=True)

    def spawn(self, command, label):
        self.sequence += 1
        log = self.evidence / ('%02d-%s.txt' % (self.sequence, label))
        child_log = self.work / ('child-output-%02d.txt' % self.sequence)
        env = dict(self.env, FAILOVER_IMAGEGEN_CHILD_OUT=str(child_log))
        process = subprocess.Popen([str(part) for part in command], cwd=self.work, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        return process, log, child_log

    def finish(self, running, timeout=90):
        process, log, child_log = running
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(process.pid)
            output, _ = process.communicate(timeout=10)
            log.write_text(output or '', encoding='utf-8')
            raise AssertionError('Installed binary timed out: ' + log.name)
        if child_log.exists():
            child_output = child_log.read_text(encoding='utf-8')
            if child_output:
                output = child_output
        log.write_text(output or '', encoding='utf-8')
        return process.returncode, output or ''

    def run(self, command, label, success=True):
        code, output = self.finish(self.spawn(command, label))
        require((code == 0) == success, '%s: unexpected exit %s; %s' % (label, code, output))
        return output

    def gui(self, label):
        running = self.spawn([self.app, 'gui'], label)
        process = running[0]
        try:
            def main_window():
                require(process.poll() is None, 'Installed GUI exited before a visible window appeared')
                ids = {process.pid} | descendants(process.pid)
                return next((window for window in visible_windows(ids) if window['title'] == 'IMG Switch'), None)
            window = wait_for(main_window, timeout=60, description='installed IMG Switch native window')
            require(window['width'] >= 880 and window['height'] >= 640, 'Installed GUI has invalid window bounds')
            # Let the Tk event loop finish startup, including connected-skill upgrade.
            time.sleep(.5)
            close_window(window['handle'])
            code, output = self.finish(running, timeout=30)
            require(code == 0, 'Normal GUI close failed: ' + output)
            self.passed(label, window={k: v for k, v in window.items() if k != 'handle'})
        finally:
            if process.poll() is None:
                kill_tree(process.pid)
                process.communicate(timeout=10)

    def requests(self):
        with urllib.request.urlopen('http://127.0.0.1:%d/__requests' % self.args.port, timeout=5) as response:
            return json.load(response)

    def generate(self, command, label, mode, success):
        task_id = str(uuid.uuid4())
        outdir = self.work / 'generated' / label
        before = len(self.requests())
        self.run(command + ['generate', '--prompt', 'windows-smoke:' + mode, '--size', '64x64',
            '--name', label, '--outdir', outdir, '--task-id', task_id, '--jobs-dir', self.jobs, '--deadline', '60'],
            label, success=success)
        record_path = self.jobs / (task_id + '.json')
        raw = record_path.read_text(encoding='utf-8')
        task = json.loads(raw)
        require('windows-local-test-key' not in raw, 'Task journal leaked the configured credential')
        require(task['status'] == ('succeeded' if success else 'failed'), 'Unexpected task status: ' + raw)
        require(len(task['items']) == 1 and len(task['items'][0]['attempts']) == 1, 'Expected one recorded attempt')
        require(len(self.requests()) == before + 1, 'Generation retried unexpectedly')
        files = list(outdir.glob('*'))
        if success:
            images = [path for path in files if path.suffix in ('.png', '.jpg', '.jpeg')]
            metadata = list(outdir.glob('*.meta.json'))
            require(len(images) == 1 and len(metadata) == 1, 'Expected exactly one image and metadata pair')
            meta = json.loads(metadata[0].read_text(encoding='utf-8'))
            require(meta['source']['name'] == 'test-source', 'Metadata used an unexpected source')
            require(meta['size_actual'] == {'width': 64, 'height': 64}, 'Decoded size was not 64 x 64')
            fixture = Path(__file__).resolve().parents[2] / 'test-fixtures/valid-64.jpg'
            if mode == 'jpeg':
                require(images[0].read_bytes() == fixture.read_bytes(), 'Output JPEG differs from accepted fixture')
            else:
                require(images[0].read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), 'Expected PNG output')
            require(Path(task['items'][0]['output']['image']) == images[0], 'Journal output path is incorrect')
        else:
            require(not files, 'Rejected truncated image was published as output')
            require(task['items'][0].get('output') is None, 'Rejected task contains an output image')
        self.passed(label, task_id=task_id, task_status=task['status'], requests=1, result_files=len(files))
        return task_id

    def cancel(self, command, label):
        task_id = str(uuid.uuid4())
        outdir = self.work / 'generated' / label
        before = len(self.requests())
        running = self.spawn(command + ['generate', '--prompt', 'windows-smoke:slow', '--size', '64x64',
            '--task-id', task_id, '--jobs-dir', self.jobs, '--outdir', outdir, '--deadline', '90'], label)
        process = running[0]
        observed = set()
        try:
            def received():
                require(process.poll() is None, 'Engine exited before the pending request reached the relay')
                observed.update(descendants(process.pid))
                return len(self.requests()) > before
            wait_for(received, timeout=45, description='packaged request worker to reach local relay')
            observed.update(descendants(process.pid))
            require(len(observed) >= 2, 'Did not observe frozen engine and request-worker descendants')
            self.run(command + ['cancel', '--task-id', task_id, '--jobs-dir', self.jobs], label + '-request')
            code, output = self.finish(running, timeout=30)
            require(code != 0, 'Cancelled in-flight task incorrectly reported success: ' + output)
            wait_for(lambda: not ((observed | {process.pid}) & set(process_snapshot())), timeout=10,
                     description='packaged engine and all observed descendants to exit')
            task = json.loads((self.jobs / (task_id + '.json')).read_text(encoding='utf-8'))
            require(task['status'] == 'unknown' and task['cancel_requested'], 'Cancellation did not preserve unknown outcome')
            require(len(task['items'][0]['attempts']) == 1 and len(self.requests()) == before + 1,
                    'Cancellation caused an automatic retry')
            require(not list(outdir.glob('*')), 'Cancelled request unexpectedly published output')
            self.passed(label, task_id=task_id, task_status=task['status'],
                        observed_descendant_pids=sorted(observed), no_descendants_survive=True, requests=1)
        finally:
            if process.poll() is None:
                kill_tree(process.pid)
                process.communicate(timeout=10)

    def exercise(self):
        require(self.app.is_file(), 'Installed GUI executable is missing')
        require(not self.config.exists() and not self.settings.exists(),
                'Refusing to overwrite an existing user configuration, even inside the test directory')
        for name in ('python', 'python.exe', 'python3', 'py.exe'):
            require(shutil.which(name, path=self.env['PATH']) is None, 'Python remains accessible through application PATH')
        require(shutil.which('powershell.exe', path=self.env['PATH']), 'System PowerShell is required for native JPEG decode')
        self.passed('python-absent-from-application-path', path=self.env['PATH'],
                    installed_executable_sha256=hashlib.sha256(self.app.read_bytes()).hexdigest())
        self.config_dir.mkdir(parents=True, exist_ok=True)
        config = {'version': 1, 'sources': [{'name': 'test-source', 'base_url': 'http://127.0.0.1:%d/v1' % self.args.port,
                  'api_key': 'windows-local-test-key', 'model': 'mock-image-1', 'enabled': True}],
                  'user_preserved_option': {'text': '原字节保留', 'value': 42}}
        config_bytes = (json.dumps(config, ensure_ascii=False, indent=3).replace('\n', '\r\n') + '\r\n \r\n').encode('utf-8')
        settings_bytes = b'{ "disabled_hosts" : [], "user_option" : "keep exact whitespace" }\r\n'
        self.config.write_bytes(config_bytes)
        self.settings.write_bytes(settings_bytes)
        self.gui('installed-gui-window-and-normal-close')
        self.run([self.app, 'on'], 'connect-agent')
        skill = self.home / '.agents/skills/failover-imagegen-tool'
        engine = skill / 'bin/img-switch-engine.exe'
        require(engine.is_file(), 'Standalone engine was not installed in managed skill')
        require('& "<skill_dir>/bin/img-switch-engine.exe"' in (skill / 'SKILL.md').read_text(encoding='utf-8'),
                'Installed skill does not document the PowerShell invocation')
        output = self.run([engine, '--list-sources'], 'standalone-list-sources')
        require('test-source' in output, 'Standalone engine did not read the isolated source')
        original_hash = hashlib.sha256(engine.read_bytes()).hexdigest()
        engine.write_bytes(b'old-release-placeholder\r\n')
        legacy = {'config.json': b'legacy-private-config\r\n', 'config.json.tmp': b'legacy-unsaved-config\r\n'}
        for name, data in legacy.items():
            (skill / name).write_bytes(data)
        self.gui('installed-gui-startup-upgrades-old-connected-skill')
        require(hashlib.sha256(engine.read_bytes()).hexdigest() == original_hash, 'GUI startup did not restore current engine')
        backup_bytes = [path.read_bytes() for path in (self.config_dir / 'migration-backups').iterdir()]
        require(all(data in backup_bytes for data in legacy.values()), 'Legacy local configuration bytes were not backed up')
        require(not any((skill / name).exists() for name in legacy), 'Legacy config remained in upgraded skill')
        self.passed('old-skill-upgrade-and-legacy-config-preservation', engine_sha256=original_hash,
                    legacy_configs_preserved=len(legacy),
                    fixture='managed connected skill with deliberately outdated engine bytes, not a historical installer')
        expected_tasks = []
        for label, command in (('standalone', [engine]), ('gui-child', [self.app, '__tool'])):
            for mode in ('png', 'jpeg', 'truncated-png', 'truncated-jpeg'):
                expected_tasks.append(self.generate(command, label + '-' + mode, mode, not mode.startswith('truncated')))
            self.cancel(command, label + '-cancel-process-tree')
        history = json.loads(self.run([engine, 'history', '--jobs-dir', self.jobs, '--json'], 'standalone-history'))
        require(set(expected_tasks) <= {task['id'] for task in history}, 'Persistent history lost an image task')
        require(len(history) == 10, 'Expected eight image-validation tasks and two cancellations')
        self.passed('task-history-survives-separate-processes', task_count=len(history))
        self.run([self.app, 'off'], 'disconnect-agent')
        require(not skill.exists(), 'Disconnect left the managed skill installed')
        require(self.config.read_bytes() == config_bytes, 'Application operations modified original config bytes')
        require(self.settings.read_bytes() == settings_bytes, 'Application operations modified settings bytes')
        self.passed('disconnect-removes-managed-skill-and-keeps-config-bytes')
        # Keep byte copies outside the installer-owned tree, then compare them
        # after the real uninstaller has exited. Hashes alone are not the assertion.
        snapshots = {str(path): base64.b64encode(path.read_bytes()).decode('ascii')
                     for path in self.config_dir.rglob('*') if path.is_file()}
        (self.work / 'before-uninstall.json').write_text(json.dumps(snapshots), encoding='utf-8')
        self.report['status'] = 'awaiting-real-uninstall'
        self.save()

    def verify_uninstall(self):
        wait_for(lambda: not self.app.exists(), timeout=30, description='installed executable removal')
        snapshots = json.loads((self.work / 'before-uninstall.json').read_text(encoding='utf-8'))
        for filename, data in snapshots.items():
            require(Path(filename).is_file(), 'Uninstaller deleted user data: ' + filename)
            require(Path(filename).read_bytes() == base64.b64decode(data), 'Uninstaller changed user data bytes: ' + filename)
        self.passed('real-uninstall-preserves-config-settings-backups-and-task-journals-byte-for-byte',
                    preserved_files=len(snapshots), config_sha256=hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.report['status'] = 'passed'
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('exercise', 'verify-uninstall'), required=True)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    acceptance = Acceptance(args)
    try:
        if args.phase == 'exercise':
            acceptance.exercise()
        else:
            acceptance.verify_uninstall()
    except Exception:
        acceptance.report['status'] = 'failed'
        acceptance.report['error'] = traceback.format_exc()
        acceptance.save()
        raise


if __name__ == '__main__':
    main()
