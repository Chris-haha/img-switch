"""Exercise the loopback fixtures used by the installed Windows acceptance test."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'engine/scripts'))
import image_decode


class SmokeRelayTests(unittest.TestCase):
    def test_ready_port_is_never_visible_before_its_contents(self):
        # Widen the real open-before-write window without changing HTTP logic.
        # A consumer must never see an empty ready file and fall back to port 80.
        wrapper = r'''
from pathlib import Path
import runpy, sys, time
script, port, observed = sys.argv[1:]
original = Path.write_text
def delayed(self, data, *args, **kwargs):
    if self == Path(port):
        with self.open('w', encoding='ascii') as output:
            original(Path(observed), 'empty port file visible', encoding='ascii')
            time.sleep(2)
            return output.write(data)
    return original(self, data, *args, **kwargs)
Path.write_text = delayed
sys.argv = [script, port]
runpy.run_path(script, run_name='__main__')
'''
        with tempfile.TemporaryDirectory() as directory:
            port, observed = Path(directory) / 'port', Path(directory) / 'observed'
            process = subprocess.Popen([sys.executable, '-B', '-c', wrapper,
                str(Path(__file__).with_name('mock_server.py')), str(port), str(observed)])
            try:
                deadline = time.monotonic() + 10
                while not (port.exists() or observed.exists()) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(port.exists(), 'mock server failed to publish ready port')
                self.assertRegex(port.read_text(encoding='ascii'), r'^[0-9]+$',
                                 'An empty ready file can route the test to default HTTP port 80')
                self.assertFalse(observed.exists(), 'ready port was opened before its contents were complete')
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_distinct_valid_and_truncated_fixtures_and_request_audit(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        port_file = Path(temporary.name) / 'port'
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('mock_server.py')), str(port_file)])
        def stop():
            process.terminate()
            process.wait(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not port_file.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(port_file.exists(), 'mock server failed to start')
        port = port_file.read_text(encoding='ascii').strip()
        self.assertRegex(port, r'^[0-9]+$', 'mock ready port must not be empty or malformed')
        self.assertTrue(1 <= int(port) <= 65535, 'mock port is outside TCP range')
        base = 'http://127.0.0.1:' + port
        images = {}
        for mode in ('png', 'jpeg', 'truncated-png', 'truncated-jpeg'):
            request = urllib.request.Request(base + '/v1/images/generations',
                data=json.dumps({'prompt': 'windows-smoke:' + mode, 'model': 'mock', 'size': '64x64'}).encode(),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer local-secret'}, method='POST')
            with urllib.request.urlopen(request, timeout=5) as response:
                images[mode] = base64.b64decode(json.load(response)['data'][0]['b64_json'], validate=True)
        self.assertTrue(images['png'].startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertLess(len(images['truncated-png']), len(images['png']))
        self.assertEqual(image_decode.validate_jpeg(images['jpeg'], (64, 64)), (64, 64))
        with self.assertRaises(image_decode.InvalidImage):
            image_decode.validate_jpeg(images['truncated-jpeg'], (64, 64))
        with urllib.request.urlopen(base + '/__requests', timeout=5) as response:
            raw = response.read()
            requests = json.loads(raw)
        self.assertEqual([request['mode'] for request in requests], list(images))
        self.assertNotIn(b'local-secret', raw)


if __name__ == '__main__':
    unittest.main()
