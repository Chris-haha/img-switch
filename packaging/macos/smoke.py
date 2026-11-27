#!/usr/bin/env python3
"""Exercise an installed test app against a local mock without a Python on PATH."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'engine' / 'tests'))
import mock_relay


def run_app(app_bundle):
    app = Path(app_bundle) / 'Contents/MacOS/IMG Switch'
    if not app.is_file():
        raise RuntimeError('Pass the path to an IMG Switch.app bundle')
    server = mock_relay.start_mock('ok_b64')
    try:
        with tempfile.TemporaryDirectory(prefix='img-switch-test-') as temp:
            root = Path(temp)
            home = root / 'home with space'
            home.mkdir()
            config_root = home / '.config'
            cfg = config_root / 'failover-imagegen-tool/config.json'
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({'version': 1, 'sources': [{
                'name': 'mock', 'base_url': 'http://127.0.0.1:%s/v1' % server.server_address[1],
                'api_key': 'test-only', 'model': 'mock-image-1', 'enabled': True
            }]}), encoding='utf-8')
            config_before = cfg.read_bytes()
            env = dict(os.environ, HOME=str(home), FAILOVER_IMAGEGEN_APP_HOME=str(home),
                       XDG_CONFIG_HOME=str(config_root), PATH='/nonexistent',
                       FAILOVER_IMAGEGEN_APP_SMOKE='1')
            env.pop('FAILOVER_IMAGEGEN_TOOL_CONFIG', None)
            gui = subprocess.run([str(app), 'gui'], env=env, text=True, capture_output=True, timeout=30)
            if gui.returncode != 0 or 'app smoke ok' not in gui.stdout:
                raise RuntimeError('GUI smoke failed: %s %s' % (gui.stdout, gui.stderr))
            install = subprocess.run([str(app), 'on'], env=env, text=True, capture_output=True, timeout=30)
            if install.returncode:
                raise RuntimeError('installation failed: %s %s' % (install.stdout, install.stderr))
            skill = home / '.agents/skills/failover-imagegen-tool'
            engine = skill / 'bin/img-switch-engine'
            if not engine.is_file():
                raise RuntimeError('installed engine missing')
            engine.write_bytes(b'outdated managed engine')
            upgrade = subprocess.run([str(app), 'gui'], env=env, text=True,
                                     capture_output=True, timeout=30)
            bundled_engine = Path(app_bundle) / 'Contents/Resources/engine/bin/img-switch-engine'
            if upgrade.returncode or engine.read_bytes() != bundled_engine.read_bytes():
                raise RuntimeError('GUI launch did not upgrade the connected engine')
            p = subprocess.run([str(engine), 'generate', '--prompt', 'test image', '--name', 'smoke', '--size', '64x64'], cwd=root, env=env, text=True, capture_output=True, timeout=60)
            if p.returncode:
                raise RuntimeError('generation failed: %s %s' % (p.stdout, p.stderr))
            images = list((root / 'generated').glob('*.png'))
            metas = list((root / 'generated').glob('*.meta.json'))
            if len(images) != 1 or len(metas) != 1 or not images[0].read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
                raise RuntimeError('expected PNG and metadata are missing')
            helper_out = root / 'gui helper results'
            helper = subprocess.run([str(app), '__tool', 'generate', '--prompt', 'GUI helper smoke',
                                     '--name', 'helper', '--size', '64x64', '--outdir', str(helper_out)],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=30)
            if helper.returncode or len(list(helper_out.glob('*.png'))) != 1:
                raise RuntimeError('frozen GUI helper failed: %s %s' % (helper.stdout, helper.stderr))
            # The server still uses the production response parser; only its image
            # bytes change, so this exercises the frozen native JPEG path as well.
            jpeg = (ROOT / 'test-fixtures/valid-64.jpg').read_bytes()
            old_png = mock_relay.make_png
            mock_relay.make_png = lambda *_args, **_kwargs: jpeg
            try:
                decoded = subprocess.run([str(engine), 'generate', '--prompt', 'JPEG smoke',
                                          '--name', 'jpeg', '--size', '64x64'], cwd=root, env=env,
                                         text=True, capture_output=True, timeout=30)
                if decoded.returncode or len(list((root / 'generated').glob('*.jpg'))) != 1:
                    raise RuntimeError('frozen JPEG decode failed: %s %s' % (decoded.stdout, decoded.stderr))
                sos = jpeg.index(b'\xff\xda')
                start = sos + 2 + int.from_bytes(jpeg[sos + 2:sos + 4], 'big')
                broken = jpeg[:start] + jpeg[start:-2][:len(jpeg[start:-2]) // 2] + b'\xff\xd9'
                mock_relay.make_png = lambda *_args, **_kwargs: broken
                rejected = subprocess.run([str(engine), 'generate', '--prompt', 'truncated JPEG smoke',
                                           '--name', 'broken', '--size', '64x64'], cwd=root, env=env,
                                          text=True, capture_output=True, timeout=30)
                if rejected.returncode == 0 or list((root / 'generated').glob('broken*')):
                    raise RuntimeError('frozen engine accepted a half-truncated JPEG')
            finally:
                mock_relay.make_png = old_png
            journals = list((cfg.parent / 'jobs').glob('*.json'))
            states = [json.loads(path.read_text())['status'] for path in journals]
            if sorted(states) != ['failed', 'succeeded', 'succeeded', 'succeeded']:
                raise RuntimeError('unexpected durable frozen task states: %r' % states)
            off = subprocess.run([str(app), 'off'], env=env, text=True, capture_output=True, timeout=30)
            if off.returncode or skill.exists() or not cfg.is_file() or cfg.read_bytes() != config_before:
                raise RuntimeError('uninstall did not preserve configuration')
            print('PASS GUI, skill install/upgrade, no-Python engine and GUI helper, strict JPEG decode/rejection, durable tasks, uninstall/config bytes preservation')
    finally:
        server.shutdown()
        server.server_close()

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python3 packaging/macos/smoke.py "/path/to/IMG Switch.app"')
    run_app(sys.argv[1])
