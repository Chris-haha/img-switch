#!/usr/bin/env python3
"""Loopback-only deterministic image relay for native Windows acceptance.

Only test fixture bytes are returned; no upstream or paid API is contacted.
The request audit deliberately omits Authorization and provider credentials.
"""
import base64
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'engine/tests'))
from mock_relay import make_png


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_json(self, status, value):
        payload = json.dumps(value).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
            if self.command == 'POST':
                print('Windows smoke relay: response status=%d bytes=%d' % (status, len(payload)), flush=True)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Cancellation intentionally closes an in-flight request.

    def do_GET(self):
        if self.path == '/__requests':
            with self.server.audit_lock:
                requests = list(self.server.requests)
            self.send_json(200, requests)
        else:
            self.send_json(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        if self.path != '/v1/images/generations':
            self.send_json(404, {'error': {'message': 'unsupported smoke endpoint'}})
            return
        length = int(self.headers['Content-Length'])
        print('Windows smoke relay: receiving request body bytes=%d' % length, flush=True)
        request = json.loads(self.rfile.read(length))
        prompt = request.get('prompt', '')
        mode = prompt.removeprefix('windows-smoke:') if prompt.startswith('windows-smoke:') else 'png'
        with self.server.audit_lock:
            self.server.requests.append({'mode': mode, 'path': self.path, 'model': request.get('model')})
        print('Windows smoke relay: request body received', flush=True)
        if mode == 'slow':
            self.server.stop_event.wait(120)
            mode = 'png'
        if mode in ('jpeg', 'truncated-jpeg'):
            raw = (root / 'test-fixtures/valid-64.jpg').read_bytes()
            if mode == 'truncated-jpeg':
                sos = raw.index(b'\xff\xda')
                start = sos + 2 + int.from_bytes(raw[sos + 2:sos + 4], 'big')
                end = raw.rindex(b'\xff\xd9')
                raw = raw[:start + (end - start) // 2] + b'\xff\xd9'
        elif mode in ('png', 'truncated-png'):
            raw = make_png(64, 64)
            if mode == 'truncated-png':
                raw = raw[:-16]
        else:
            self.send_json(400, {'error': {'message': 'unknown smoke fixture'}})
            return
        self.send_json(200, {'data': [{'b64_json': base64.b64encode(raw).decode('ascii')}]})


def start_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests, server.audit_lock = [], threading.Lock()
    server.stop_event = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def publish_port(path, port):
    """Publish readiness atomically; an empty file would imply HTTP port 80."""
    pending = path.with_name(path.name + '.pending')
    if path.exists() or pending.exists():
        raise FileExistsError('Refusing to reuse an existing mock readiness file')
    try:
        pending.write_text(str(port), encoding='ascii')
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


if __name__ == '__main__':
    server = start_server()
    publish_port(Path(sys.argv[1]), server.server_port)
    print('Windows smoke relay: listening on 127.0.0.1:%d' % server.server_port, flush=True)
    threading.Event().wait()
