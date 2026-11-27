"""Durable image tasks: local relay only, no real keys, accounts or archives."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'engine/scripts'), str(ROOT / 'engine/tests')]
import failover_imagegen_tool as engine
import failovercfg
from mock_relay import make_png


class JobRelay(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        mode = self.path.split('/')[1]
        self.server.requests.append((mode, data))
        self.server.received.set()
        if self.server.jobs.exists():
            records = [json.loads(p.read_text()) for p in self.server.jobs.glob('*.json')]
            self.server.persisted.append(any(i['status'] == 'running' and i['attempts'] for r in records for i in r['items']))
        if mode == 'drop':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if mode in ('slow', 'headerslow', 'errorheaderslow'):
            if mode in ('headerslow', 'errorheaderslow'):
                self.send_response(500 if mode == 'errorheaderslow' else 200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Request-ID', 'header-before-timeout')
                self.end_headers()
                self.wfile.flush()
            time.sleep(3)
        error = None
        if mode.startswith('http') and mode[4:].isdigit():
            error = (int(mode[4:]), {'message': 'relay returned an upstream failure after accepting the request'})
        elif mode == 'policymsg403':
            error = (403, {'message': 'Rejected by content policy safety filter'})
        elif mode == 'policy403':
            error = (403, {'code': 'content_policy_violation', 'message': 'content policy'})
        elif mode == 'auth':
            error = (401, {'message': 'wrong key'})
        elif mode == 'unsupported':
            error = (400, {'code': 'unsupported_size', 'param': 'size', 'message': 'size unsupported'})
        elif mode == 'policy':
            error = (400, {'code': 'content_policy_violation', 'message': 'content policy'})
        elif mode == 'generic':
            error = (422, {'message': 'invalid request'})
        elif data['prompt'] == 'fail' and self.server.fail_prompt:
            error = (400, {'message': 'user prompt invalid'})
        if error:
            code, detail = error
            payload = {'error': detail}
        else:
            code = 200
            payload = {'data': [{'b64_json': base64.b64encode(make_png(64, 64)).decode()}]}
        if mode == 'url_dead':
            payload = {'data': [{'url': 'http://127.0.0.1:1/image.png?token=download-secret'}]}
        if mode == 'badjpeg':
            raw = (ROOT / 'test-fixtures/valid-64.jpg').read_bytes()
            start = raw.index(b'\xff\xda')
            offset = start + 2 + int.from_bytes(raw[start + 2:start + 4], 'big')
            payload = {'data': [{'b64_json': base64.b64encode(raw[:offset] + b'\x00\xff\xd9').decode()}]}
        if mode.startswith('unsupportedjpeg-'):
            raw = bytearray((ROOT / 'test-fixtures/valid-64.jpg').read_bytes())
            frame = raw.index(b'\xff\xc0')
            if mode.endswith('-sof'):
                raw[frame + 1] = 0xc3  # Lossless JPEG is outside the strict decoder.
            elif '-depth' in mode:
                raw[frame + 4] = 12
                if mode.endswith('-reordered'):
                    length = 2 + int.from_bytes(raw[frame + 2:frame + 4], 'big')
                    segment = raw[frame:frame + length]
                    del raw[frame:frame + length]
                    scan = raw.index(b'\xff\xda')
                    raw[scan:scan] = segment
                    table = raw.index(b'\xff\xc4')
                    raw[table + 21] = 12  # Valid 12-bit DC category, unsupported by 8-bit checker.
            elif mode.endswith('-dnl'):
                raw[frame + 5:frame + 7] = b'\x00\x00'
            payload = {'data': [{'b64_json': base64.b64encode(raw).decode()}]}
        if mode == 'echo':
            payload = {'unexpected': data['prompt'] + ' opaque-private-key-92834'}
        body = json.dumps(payload).encode()
        try:
            if mode not in ('headerslow', 'errorheaderslow'):
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Request-ID', 'req-' + mode)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class EngineJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.jobs = self.work / 'jobs'
        self.cfg = self.work / 'config.json'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), JobRelay)
        self.server.requests = []
        self.server.persisted = []
        self.server.received = threading.Event()
        self.server.jobs = self.jobs
        self.server.fail_prompt = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = dict(os.environ, FAILOVER_IMAGEGEN_TOOL_CONFIG=str(self.cfg), PYTHONDONTWRITEBYTECODE='1')
        self.configure('good')

    def configure(self, *modes, extra=None):
        sources = [{'name': mode, 'api_key': 'opaque-private-key-92834', 'model': 'test-image',
                    'base_url': 'http://127.0.0.1:%d/%s/v1' % (self.server.server_port, mode)} for mode in modes]
        if extra:
            sources[0].update(extra)
        self.cfg.write_text(json.dumps({'sources': sources}))

    def command(self, *args):
        return [sys.executable, '-B', str(Path(engine.__file__)), *args, '--jobs-dir', str(self.jobs)]

    def run_cli(self, *args):
        return subprocess.run(self.command(*args), cwd=self.work, env=self.env, capture_output=True, text=True, timeout=12)

    def records(self):
        return [json.loads(p.read_text()) for p in self.jobs.glob('*.json')]

    def test_create_private_task_before_request_and_json_history(self):
        ident = str(uuid.uuid4())
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64', '--task-id', ident)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        history = self.run_cli('history', '--json')
        self.assertEqual(history.returncode, 0, history.stderr)
        records = json.loads(history.stdout)
        self.assertEqual(len(records), 1)
        task = records[0]
        self.assertEqual((task['id'], task['status']), (ident, 'succeeded'))
        self.assertEqual(task['items'][0]['attempts'][0]['request_id'], 'req-good')
        self.assertTrue(all(self.server.persisted), 'attempt must be durably visible before POST')
        self.assertTrue(Path(task['items'][0]['output']['image']).is_file())
        self.assertNotIn('opaque-private-key-92834', (self.jobs / (ident + '.json')).read_text())
        if os.name != 'nt':
            self.assertEqual((self.jobs / (ident + '.json')).stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.jobs.stat().st_mode & 0o777, 0o700)

    def test_deadline_unknown_stops_without_backup_and_preserves_request_id(self):
        self.configure('headerslow', 'good')
        before = time.monotonic()
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64', '--deadline', '0.8')
        self.assertNotEqual(p.returncode, 0)
        self.assertLess(time.monotonic() - before, 2.5)
        records = self.records()
        self.assertEqual(len(records), 1, p.stdout + p.stderr)
        self.assertEqual(records[0]['status'], 'unknown')
        self.assertEqual([m for m, _ in self.server.requests], ['headerslow'])
        self.assertEqual(records[0]['items'][0]['attempts'][0]['request_id'], 'header-before-timeout')
        retried = self.run_cli('retry', '--task-id', records[0]['id'])
        self.assertNotEqual(retried.returncode, 0)
        self.assertEqual(len(self.server.requests), 1)

    def test_ambiguous_http_failures_never_repeat_a_paid_post(self):
        for code in (408, 500, 502, 503, 504, 520, 524):
            with self.subTest(code=code):
                mode = 'http' + str(code)
                self.configure(mode, 'good', extra={'models': ['first-model', 'second-model']})
                self.server.requests.clear()
                ident = str(uuid.uuid4())
                p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64', '--task-id', ident)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                task = json.loads((self.jobs / (ident + '.json')).read_text())
                self.assertEqual(task['status'], 'unknown')
                self.assertEqual([(m, data['model']) for m, data in self.server.requests], [(mode, 'first-model')])
                attempt = task['items'][0]['attempts'][0]
                self.assertEqual(attempt['request_id'], 'req-' + mode)
                self.assertEqual(attempt['status'], 'unknown')
                self.assertIn(str(code), attempt['error'])
                self.assertNotEqual(self.run_cli('retry', '--task-id', ident).returncode, 0)
                self.assertEqual(len(self.server.requests), 1)

    def test_http_error_headers_are_saved_before_a_stalled_error_body(self):
        self.configure('errorheaderslow', 'good')
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64', '--deadline', '0.8')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        task = self.records()[0]
        self.assertEqual(task['status'], 'unknown')
        self.assertEqual(task['items'][0]['attempts'][0]['request_id'], 'header-before-timeout')
        self.assertEqual(len(self.server.requests), 1)

    def test_disconnect_after_post_is_unknown(self):
        self.configure('drop', 'good')
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64')
        self.assertNotEqual(p.returncode, 0)
        records = self.records()
        self.assertEqual(len(records), 1, p.stdout + p.stderr)
        self.assertEqual(records[0]['status'], 'unknown')
        self.assertEqual([m for m, _ in self.server.requests], ['drop'])

    def test_batch_retry_only_failed_and_caches_auth_source(self):
        self.configure('auth', 'good')
        prompts = self.work / 'prompts.txt'
        prompts.write_text('ok\nfail\nlast\n')
        p = self.run_cli('batch', '--prompts-file', str(prompts), '--size', '64x64')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        records = self.records()
        self.assertEqual(len(records), 1, p.stdout + p.stderr)
        task = records[0]
        self.assertEqual(task['status'], 'partial')
        self.assertEqual([i['status'] for i in task['items']], ['succeeded', 'failed', 'succeeded'])
        self.assertEqual([m for m, _ in self.server.requests].count('auth'), 1)
        paths = [i['output'] for i in task['items'] if i['output']]
        self.server.fail_prompt = False
        self.server.requests.clear()
        p = self.run_cli('retry', '--task-id', task['id'])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([d['prompt'] for _, d in self.server.requests], ['fail', 'fail'])
        self.assertEqual(self.records()[0]['status'], 'succeeded')
        self.assertEqual([self.records()[0]['items'][i]['output'] for i in (0, 2)], paths)

    def test_capability_skip_and_structured_error_do_not_resize(self):
        self.configure('good', 'backup', extra={'supported_sizes': ['1024x1024']})
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([m for m, _ in self.server.requests], ['backup'])
        self.assertEqual(self.server.requests[0][1]['size'], '64x64')
        self.server.requests.clear()
        self.configure('unsupported', 'backup')
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([m for m, _ in self.server.requests], ['unsupported', 'backup'])
        self.assertTrue(all(d['size'] == '64x64' for _, d in self.server.requests))

    def test_policy_and_unrecognized_4xx_remain_fatal(self):
        for mode in ('policy', 'policy403', 'policymsg403', 'generic'):
            self.configure(mode, 'backup')
            self.server.requests.clear()
            p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64')
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertEqual([m for m, _ in self.server.requests], [mode])

    def test_cancel_running_and_concurrent_retry_does_not_duplicate(self):
        self.configure('slow', 'good')
        ident = str(uuid.uuid4())
        proc = subprocess.Popen(self.command('generate', '--prompt', 'portrait', '--size', '64x64', '--task-id', ident),
                                cwd=self.work, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        self.assertTrue(self.server.received.wait(4), 'generation never reached local relay')
        retried = self.run_cli('retry', '--task-id', ident)
        self.assertNotEqual(retried.returncode, 0)
        cancel = self.run_cli('cancel', '--task-id', ident)
        self.assertEqual(cancel.returncode, 0, cancel.stdout + cancel.stderr)
        out, err = proc.communicate(timeout=3)
        self.assertNotEqual(proc.returncode, 0, out + err)
        task = self.records()[0]
        self.assertEqual(task['status'], 'unknown')
        self.assertTrue(task['cancel_requested'])
        self.assertEqual(len(self.server.requests), 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX interrupt signal')
    def test_ctrl_c_stops_pending_batch_items(self):
        import signal
        self.configure('slow', 'good')
        prompts = self.work / 'prompts.txt'
        prompts.write_text('first\nsecond\n')
        ident = str(uuid.uuid4())
        proc = subprocess.Popen(self.command('batch', '--prompts-file', str(prompts), '--size', '64x64', '--task-id', ident),
                                cwd=self.work, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        self.assertTrue(self.server.received.wait(4))
        os.kill(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=6)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertEqual([data['prompt'] for _, data in self.server.requests], ['first'])
        self.assertEqual([item['status'] for item in self.records()[0]['items']], ['unknown', 'cancelled'])

    @unittest.skipIf(os.name == 'nt', 'POSIX termination signal')
    def test_sigterm_cleans_request_process_group_and_cancels_pending_items(self):
        import signal
        self.configure('slow', 'good')
        prompts = self.work / 'prompts.txt'
        prompts.write_text('first\nsecond\n')
        ident = str(uuid.uuid4())
        proc = subprocess.Popen(self.command('batch', '--prompts-file', str(prompts), '--size', '64x64', '--task-id', ident),
                                cwd=self.work, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        self.assertTrue(self.server.received.wait(4))
        listing = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,command='], text=True)
        workers = [int(fields[0]) for line in listing.splitlines()
                   if len(fields := line.split(None, 2)) == 3
                   and fields[1] == str(proc.pid) and 'spawn_main' in fields[2]]
        self.assertEqual(len(workers), 1, 'the bounded request must run in one child')
        # Keep failed-regression cleanup local: never leave a test worker alive.
        def stop_orphan(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in workers:
            self.addCleanup(stop_orphan, pid)
        started = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        out, err = proc.communicate(timeout=4)
        self.assertLess(time.monotonic() - started, 2.0, "termination waited for an orphaned HTTP worker")
        self.assertNotEqual(proc.returncode, 0, out + err)
        live = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True)
        self.assertFalse(any(line.split(None, 1)[0] == str(workers[0]) for line in live.splitlines() if line.strip()),
                         'request worker survived termination of its parent engine')
        task = self.records()[0]
        self.assertTrue(task['cancel_requested'])
        self.assertEqual([item['status'] for item in task['items']], ['unknown', 'cancelled'])
        self.assertEqual([data['prompt'] for _, data in self.server.requests], ['first'])

    def test_stale_running_recovery_never_replays_unknown(self):
        ident = str(uuid.uuid4())
        self.jobs.mkdir()
        task = {'id': ident, 'name': 'crash', 'kind': 'generate', 'status': 'running',
                'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z',
                'items': [{'id': '1', 'status': 'running', 'prompt': 'portrait', 'attempts': [{'status': 'running'}]}],
                'sources': ['good'], 'size': [64, 64], 'options': {'outdir': str(self.work / 'generated'), 'deadline': 10}}
        (self.jobs / (ident + '.json')).write_text(json.dumps(task))
        p = self.run_cli('history', '--json')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        recovered = json.loads(p.stdout)[0]
        self.assertEqual(recovered['status'], 'unknown')
        self.assertEqual(recovered['items'][0]['attempts'][0]['status'], 'unknown')
        self.assertNotEqual(self.run_cli('retry', '--task-id', ident).returncode, 0)
        self.assertFalse(self.server.requests)
        retried = self.run_cli('retry', '--task-id', ident, '--confirm-unknown')
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(len(self.server.requests), 1)

    def test_image_download_failure_is_unknown_not_a_second_generation(self):
        self.configure('url_dead', 'good')
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual([m for m, _ in self.server.requests], ['url_dead'])
        self.assertEqual(self.records()[0]['status'], 'unknown')
        self.assertNotIn('download-secret', p.stdout + p.stderr + json.dumps(self.records()))

    def test_jpeg_entropy_is_decoded_before_accepting_success(self):
        self.configure('badjpeg', 'good')
        p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([m for m, _ in self.server.requests], ['badjpeg', 'good'])
        self.assertEqual(self.records()[0]['items'][0]['output']['source'], 'good')

    def test_unsupported_jpeg_coding_stops_without_paying_a_backup(self):
        for suffix in ('sof', 'depth', 'depth-reordered', 'dnl'):
            with self.subTest(coding=suffix):
                mode = 'unsupportedjpeg-' + suffix
                self.configure(mode, 'good')
                self.server.requests.clear()
                ident = str(uuid.uuid4())
                p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64', '--task-id', ident)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertEqual([name for name, _ in self.server.requests], [mode])
                task = json.loads((self.jobs / (ident + '.json')).read_text())
                self.assertEqual(task['status'], 'failed')
                self.assertIsNone(task['items'][0]['output'])
                attempt = task['items'][0]['attempts'][0]
                self.assertEqual(attempt['strategy'], 'fatal')
                self.assertIn('不支持', attempt['error'])
                self.assertEqual(attempt['request_id'], 'req-' + mode)

    def test_missing_decoder_stops_before_another_paid_attempt(self):
        import image_decode
        raw = (ROOT / 'test-fixtures/valid-64.jpg').read_bytes()
        src = json.loads(self.cfg.read_text())['sources'][0]
        payload = {'data': [{'b64_json': base64.b64encode(raw).decode()}]}
        with mock.patch.object(engine, '_http_json', return_value=(200, payload, b'')), \
             mock.patch.object(image_decode, 'validate_jpeg', side_effect=image_decode.DecoderUnavailable('decoder unavailable')):
            with self.assertRaises(engine.AttemptError) as raised:
                engine.attempt_source(src, 'portrait', (64, 64), None, 'test-image')
        self.assertEqual(raised.exception.kind, 'fatal')

    def test_all_saved_and_printed_text_masks_opaque_configured_key(self):
        self.configure('echo', 'good')
        p = self.run_cli('generate', '--prompt', 'portrait opaque-private-key-92834', '--size', '64x64')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        material = p.stdout + p.stderr + ''.join(path.read_text() for path in self.work.rglob('*.json') if path != self.cfg)
        self.assertNotIn('opaque-private-key-92834', material)
        self.assertIn('redacted', material)

    def test_nonfinite_deadline_rejected_before_request(self):
        for value in ('nan', 'inf', '-1'):
            p = self.run_cli('generate', '--prompt', 'portrait', '--size', '64x64', '--deadline=' + value)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertFalse(self.server.requests)
        self.assertEqual(len(self.records()), 3)

    def test_validation_failure_is_also_a_durable_job(self):
        self.cfg.unlink()
        p = self.run_cli('generate', '--prompt', 'portrait')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(self.records()[0]['status'], 'failed')
        self.assertIn('No config found', self.records()[0]['items'][0]['error'])

    def test_malformed_json_records_are_visible_without_crashing_history(self):
        self.jobs.mkdir()
        bad_values = [None, 'bad', [None], ['bad'], [{'status': 'unknown'}]]
        for index, items in enumerate(bad_values):
            ident = str(uuid.uuid4())
            (self.jobs / (ident + '.json')).write_text(json.dumps({
                'id': ident, 'name': 'broken', 'status': 'running', 'items': items,
                'sources': [], 'size': [64, 64], 'options': {}, 'created_at': '2026-01-01'}))
        p = self.run_cli('history', '--json')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        records = json.loads(p.stdout)
        self.assertEqual(len(records), len(bad_values))
        self.assertTrue(all('损坏' in r.get('error', '') for r in records))
        self.assertEqual(len(list(self.jobs.glob('*.json'))), len(bad_values))

    def test_bad_options_counts_and_timestamps_are_not_trusted(self):
        self.jobs.mkdir()
        for field, value in [('options', []), ('counts', []), ('counts', {'total': -1}),
                             ('sources', [{}]), ('created_at', {}), ('status', [])]:
            ident = str(uuid.uuid4())
            record = {'id': ident, 'name': 'broken', 'kind': 'generate', 'status': 'failed',
                      'items': [{'id': '1', 'status': 'failed', 'prompt': 'portrait'}],
                      'sources': [], 'size': [64, 64], 'options': {}, 'created_at': '2026-01-01'}
            record[field] = value
            (self.jobs / (ident + '.json')).write_text(json.dumps(record))
        p = self.run_cli('history', '--json')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(all('损坏' in r.get('error', '') for r in json.loads(p.stdout)))

    def test_windows_cleanup_includes_native_decoder_descendants(self):
        worker = mock.Mock(pid=98765)
        worker.is_alive.side_effect = [True, False]
        with mock.patch.object(engine.os, 'name', 'nt'), mock.patch('subprocess.run') as run:
            engine._stop_worker(worker)
        self.assertTrue(run.called, 'Windows cancellation must terminate the process tree')
        command = run.call_args.args[0]
        self.assertEqual(command, ['taskkill', '/PID', '98765', '/T', '/F'])

    def test_standalone_entry_runs_freeze_support_before_argument_parsing(self):
        import multiprocessing
        import runpy
        with mock.patch.object(multiprocessing, 'freeze_support', side_effect=SystemExit(77)), \
             mock.patch.object(sys, 'argv', [engine.__file__, '--multiprocessing-fork']), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(engine.__file__, run_name='__main__')
        self.assertEqual(raised.exception.code, 77)

    def test_concurrent_default_output_names_preserve_both_images_and_metadata(self):
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(2)
        unique = engine.unique_path
        counter = [0]
        guard = threading.Lock()
        def both_observe_missing(path):
            result = unique(path)
            with guard:
                counter[0] += 1
                call = counter[0]
            if call <= 2:
                barrier.wait(timeout=3)
            return result
        def produce(index):
            raw = make_png(64 + index, 64)
            result = {'image_bytes': raw, 'ext': 'png', 'width': 64 + index, 'height': 64,
                      'elapsed': 0.1, 'source_name': 'local', 'source_host': 'localhost', 'model': 'image'}
            paths = engine.save_outputs(str(self.work / 'generated'), 'same', False, (64, 64),
                                        result, 'prompt-' + str(index), None, None, [], time.time())
            return paths, raw
        with mock.patch.object(engine.time, 'strftime', return_value='20260101-120000'), \
             mock.patch.object(engine, 'unique_path', side_effect=both_observe_missing), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(produce, (0, 1)))
        self.assertEqual(len({paths[0] for paths, _ in results}), 2, 'two jobs must reserve distinct output paths')
        self.assertEqual(len(list((self.work / 'generated').glob('*.png'))), 2)
        for index, (paths, raw) in enumerate(results):
            image, metadata, record = paths
            self.assertEqual(Path(image).read_bytes(), raw)
            saved = json.loads(Path(metadata).read_text())
            self.assertEqual(saved['prompt'], 'prompt-' + str(index))
            self.assertEqual(saved['output_file'], os.path.abspath(image))

    def test_failed_forced_image_write_preserves_existing_image(self):
        outdir = self.work / 'generated'
        outdir.mkdir()
        target = outdir / 'same-20260101-120000-64x64.png'
        original = make_png(65, 64)
        target.write_bytes(original)
        result = {'image_bytes': make_png(64, 64), 'ext': 'png', 'width': 64, 'height': 64,
                  'elapsed': 0.1, 'source_name': 'local', 'source_host': 'localhost', 'model': 'image'}
        with mock.patch.object(engine.time, 'strftime', return_value='20260101-120000'), \
             mock.patch.object(failovercfg.os, 'replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                engine.save_outputs(str(outdir), 'same', True, (64, 64), result, 'portrait', None, None, [], time.time())
        self.assertEqual(target.read_bytes(), original)

    def test_task_store_path_api_and_lock(self):
        spec = importlib.util.find_spec('task_store')
        self.assertIsNotNone(spec, 'task_store module is required')
        import task_store
        with mock.patch.dict(os.environ, {'FAILOVER_IMAGEGEN_JOBS_DIR': str(self.jobs)}):
            self.assertEqual(Path(task_store.default_jobs_dir()), self.jobs)
        ident = str(uuid.uuid4())
        with task_store.task_lock(str(self.jobs), ident):
            with self.assertRaises(RuntimeError):
                with task_store.task_lock(str(self.jobs), ident):
                    pass

    def test_atomic_replace_updates_path_after_snapshot_closes(self):
        path = self.work / 'snapshot.json'
        original, updated = b'{"value": "before"}', b'{"value": "after"}'
        path.write_bytes(original)
        with failovercfg.shared_read(path) as reader:
            snapshot = reader.read()
        failovercfg.atomic_write(str(path), updated)
        self.assertEqual(snapshot, original)
        self.assertEqual(path.read_bytes(), updated)

    def test_windows_atomic_replace_retries_only_local_conflicts(self):
        # Inject the native error on other platforms too; the second call still
        # performs a real atomic replace, using the same staged bytes.
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                path = self.work / ('sharing-%s.json' % code)
                original, updated = b'before', b'after'
                path.write_bytes(original)
                conflict = PermissionError(13, 'simulated Windows sharing conflict')
                conflict.winerror = code
                real_replace = os.replace
                attempts = []
                def conflict_once(source, target):
                    attempts.append((source, target))
                    if len(attempts) == 1:
                        self.assertEqual(Path(target).read_bytes(), original)
                        raise conflict
                    return real_replace(source, target)
                windows_os = mock.Mock(wraps=os)
                windows_os.name = 'nt'
                windows_os.replace.side_effect = conflict_once
                with mock.patch.object(failovercfg, 'os', windows_os):
                    failovercfg.atomic_write(str(path), updated)
                self.assertEqual(path.read_bytes(), updated)
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[0], attempts[1])
                self.assertEqual(list(self.work.glob('.img-switch-*.tmp')), [])
        self.assertEqual(self.server.requests, [], 'retry only local file replacement')

    def test_windows_atomic_replace_exhaustion_preserves_bytes_and_error(self):
        path = self.work / 'permanent-denial.json'
        path.write_bytes(b'before')
        denied = PermissionError(13, 'simulated persistent permission denial')
        denied.winerror = 5
        windows_os = mock.Mock(wraps=os)
        windows_os.name = 'nt'
        windows_os.replace.side_effect = denied
        started = time.monotonic()
        with mock.patch.object(failovercfg, 'os', windows_os):
            with self.assertRaises(PermissionError) as failure:
                failovercfg.atomic_write(str(path), b'after')
        self.assertIs(failure.exception, denied)
        self.assertGreater(windows_os.replace.call_count, 1)
        self.assertLess(time.monotonic() - started, 3, 'local retry must have a short bound')
        self.assertEqual(path.read_bytes(), b'before')
        self.assertEqual(list(self.work.glob('.img-switch-*.tmp')), [])
        self.assertEqual(self.server.requests, [])

    def test_atomic_replace_does_not_retry_other_os_errors(self):
        for platform, code in (('posix', 5), ('nt', None), ('nt', 112)):
            with self.subTest(platform=platform, winerror=code):
                path = self.work / 'unrelated-error.json'
                path.write_bytes(b'before')
                error = OSError(13, 'not a native transient sharing error')
                if code is not None:
                    error.winerror = code
                operating_system = mock.Mock(wraps=os)
                operating_system.name = platform
                operating_system.replace.side_effect = error
                with mock.patch.object(failovercfg, 'os', operating_system):
                    with self.assertRaises(OSError) as failure:
                        failovercfg.atomic_write(str(path), b'after')
                self.assertIs(failure.exception, error)
                self.assertEqual(operating_system.replace.call_count, 1)
                self.assertEqual(path.read_bytes(), b'before')
                self.assertEqual(list(self.work.glob('.img-switch-*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows native target-handle replacement semantics')
    def test_windows_atomic_replace_timeout_preserves_open_target(self):
        path = self.work / 'held-reader.json'
        original = b'{"value": "before"}'
        path.write_bytes(original)
        real_replace = os.replace
        with failovercfg.shared_read(path) as reader:
            # A genuine open target blocks MoveFileExW, even with SHARE_DELETE.
            with mock.patch.object(failovercfg.os, 'replace', wraps=real_replace) as replace:
                with self.assertRaises(PermissionError) as conflict:
                    failovercfg.atomic_write(str(path), b'{"value": "after"}')
                self.assertIn(conflict.exception.winerror, (5, 32, 33))
                self.assertGreater(replace.call_count, 1)
            self.assertEqual(reader.read(), original)
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.work.glob('.img-switch-*.tmp')), [])
        self.assertEqual(self.server.requests, [])

    @unittest.skipUnless(os.name == 'nt', 'Windows native target-handle replacement semantics')
    def test_windows_journal_replace_waits_until_reader_releases_handle(self):
        import task_store
        ident = str(uuid.uuid4())
        record = task_store.create_task(str(self.jobs), {
            'id': ident, 'name': 'before', 'status': 'queued', 'sources': [],
            'items': [{'id': '1', 'status': 'queued', 'prompt': 'local fixture'}]})
        opened, release, conflicted = threading.Event(), threading.Event(), threading.Event()
        results, failures = [], []
        shared_read, real_replace = failovercfg.shared_read, os.replace
        reader_thread = None

        @contextlib.contextmanager
        def hold_reader(filename):
            with shared_read(filename) as stream:
                if threading.current_thread() is reader_thread:
                    opened.set()
                    if not release.wait(4):
                        raise RuntimeError('test did not release the reader')
                yield stream

        def observe_replace(source, target):
            try:
                return real_replace(source, target)
            except PermissionError:
                conflicted.set()
                raise

        def read_task():
            try:
                results.append(task_store.load_task(str(self.jobs), ident))
            except Exception as error:
                failures.append(error)

        def write_task():
            try:
                record['name'] = 'after'
                task_store.save_task(str(self.jobs), record)
            except Exception as error:
                failures.append(error)

        with mock.patch.object(failovercfg, 'shared_read', hold_reader), \
             mock.patch.object(failovercfg.os, 'replace', side_effect=observe_replace):
            reader_thread = threading.Thread(target=read_task)
            writer_thread = threading.Thread(target=write_task)
            reader_thread.start()
            try:
                self.assertTrue(opened.wait(3), 'reader did not acquire its real Windows handle')
                writer_thread.start()
                self.assertTrue(conflicted.wait(3), 'real replacement must encounter the held target')
            finally:
                release.set()
                reader_thread.join(5)
                if writer_thread.ident is not None:
                    writer_thread.join(5)
        self.assertFalse(reader_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0]['name'], 'before')
        self.assertEqual(task_store.load_task(str(self.jobs), ident)['name'], 'after')
        self.assertEqual(self.server.requests, [], 'journal replacement must not generate an image')


if __name__ == '__main__':
    unittest.main()
