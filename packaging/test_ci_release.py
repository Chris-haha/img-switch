"""Publishing must be gated by native evidence and the actual uploaded bytes."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('ci_release', Path(__file__).with_name('ci_release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = 'a' * 40
VERSION = '0.1.0-beta.6'


def evidence(target):
    return {'commit': SHA, 'platform': target, 'tests_passed': True, 'installed_smoke_passed': True,
            'checks': [{'name': 'regressions', 'passed': True}, {'name': 'installed_smoke', 'passed': True}]}


class FakeAPI:
    repo = 'owner/repository'

    def __init__(self):
        self.calls, self.uploads, self.payloads = [], [], {}
        self.release = {'id': 1, 'draft': True, 'target_commitish': SHA,
                        'body': '<!-- img-switch commit:' + SHA + ' -->', 'assets': []}
        for index, target in enumerate(release.PLATFORMS):
            name = release.artifact_name(VERSION, target)
            binary = ('native-image-' + target).encode()
            record = evidence(target)
            record['artifact'] = {'name': name, 'bytes': len(binary), 'sha256': hashlib.sha256(binary).hexdigest()}
            for asset_id, asset_name, payload in ((index * 2 + 1, name, binary),
                    (index * 2 + 2, 'verification-' + target + '.json', json.dumps(record).encode())):
                self.release['assets'].append({'id': asset_id, 'name': asset_name, 'size': len(payload)})
                self.payloads['/releases/assets/' + str(asset_id)] = payload

    def request(self, path, method='GET', **kwargs):
        self.calls.append((path, method, kwargs))
        if method == 'PATCH':
            return {'html_url': 'https://github.com/owner/repository/releases/tag/v' + VERSION}
        if path == '/releases/1':
            return copy.deepcopy(self.release)
        if path.startswith('/git/ref/tags/'):
            return None
        if path in self.payloads:
            return self.payloads[path]
        raise AssertionError('Unexpected API call: ' + path)

    def upload(self, _draft, name, payload):
        self.uploads.append((name, payload))


class ReleaseGates(unittest.TestCase):
    def test_failed_or_missing_native_smoke_and_wrong_commit_are_rejected(self):
        for field, value in (('commit', 'b' * 40), ('platform', 'macos-x86_64'),
                             ('tests_passed', False), ('installed_smoke_passed', False), ('checks', [])):
            record = evidence('windows-x64')
            record[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                release.validate_evidence(record, 'windows-x64', SHA)

    def test_missing_platform_never_publishes(self):
        api = FakeAPI()
        api.release['assets'].pop()
        with self.assertRaisesRegex(RuntimeError, 'all three'):
            release.publish(api, 1, VERSION, SHA)
        self.assertFalse(api.uploads)
        self.assertFalse(any(method == 'PATCH' for _, method, _ in api.calls))

    def test_corrupted_uploaded_binary_never_publishes(self):
        api = FakeAPI()
        api.payloads['/releases/assets/1'] = b'corrupted'
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            release.publish(api, 1, VERSION, SHA)
        self.assertFalse(any(method == 'PATCH' for _, method, _ in api.calls))

    def test_already_public_release_is_never_modified(self):
        api = FakeAPI()
        api.release['draft'] = False
        with self.assertRaisesRegex(RuntimeError, 'published'):
            release.publish(api, 1, VERSION, SHA)
        self.assertFalse(api.uploads)

    def test_publish_checks_all_bytes_and_uploads_plain_checksums(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            notes = root / 'docs/releases' / ('v' + VERSION + '.md')
            notes.parent.mkdir(parents=True)
            notes.write_text('Release notes', encoding='utf-8')
            with mock.patch.object(release, 'ROOT', root), mock.patch.dict(os.environ, GITHUB_RUN_ID='123'):
                release.publish(api, 1, VERSION, SHA)
        self.assertEqual(len([call for call in api.calls if call[2].get('binary')]), 6)
        self.assertEqual(api.uploads[0][0], 'SHA256SUMS.txt')
        self.assertEqual(len(api.uploads[0][1].decode().splitlines()), 3)
        patch = [kwargs['value'] for _, method, kwargs in api.calls if method == 'PATCH'][0]
        self.assertEqual(patch['tag_name'], 'v' + VERSION)
        self.assertFalse(patch['draft'])
        self.assertEqual(patch['target_commitish'], SHA)

    def test_publish_resumes_after_checksum_upload_without_overwriting_it(self):
        api = FakeAPI()
        lines = []
        for index, target in enumerate(release.PLATFORMS):
            raw = api.payloads['/releases/assets/' + str(index * 2 + 1)]
            lines.append(hashlib.sha256(raw).hexdigest() + '  ' + release.artifact_name(VERSION, target))
        payload = ('\n'.join(lines) + '\n').encode()
        api.payloads['/releases/assets/7'] = payload
        api.release['assets'].append({'id': 7, 'name': 'SHA256SUMS.txt', 'size': len(payload)})
        with mock.patch.dict(os.environ, GITHUB_RUN_ID='123'):
            release.publish(api, 1, VERSION, SHA)
        self.assertFalse(api.uploads)
        self.assertTrue(any(method == 'PATCH' for _, method, _ in api.calls))
        api.release.update(draft=False, tag_name='v' + VERSION,
                           html_url='https://github.com/owner/repository/releases/tag/v' + VERSION)
        release.publish(api, 1, VERSION, SHA)
        self.assertEqual(sum(method == 'PATCH' for _, method, _ in api.calls), 1)
        self.assertFalse(api.uploads)

    def test_upload_refuses_archive_transport_and_existing_asset(self):
        api = release.GitHub.__new__(release.GitHub)
        api.request = mock.Mock()
        draft = {'assets': [{'id': 1, 'name': 'existing.exe'}], 'upload_url': 'https://uploads.github.com/test{?name}'}
        for name, payload in (('source.zip', b'not-an-archive'), ('existing.exe', b'new')):
            with self.assertRaises(RuntimeError):
                api.upload(draft, name, payload)
        api.request.assert_called_once_with('/releases/assets/1', binary=True)

    def test_identical_uploaded_asset_can_be_reused_after_interrupted_upload(self):
        api = release.GitHub.__new__(release.GitHub)
        api.request = mock.Mock(return_value=b'original')
        asset = {'id': 1, 'name': 'installer.exe', 'size': 8}
        self.assertEqual(api.upload({'assets': [asset]}, 'installer.exe', b'original'), asset)

    def test_release_only_runs_on_explicit_version_branch(self):
        with mock.patch.dict(os.environ, GITHUB_SHA=SHA, GITHUB_REF='refs/heads/main'):
            with self.assertRaisesRegex(RuntimeError, 'explicit'):
                release.identity()


if __name__ == '__main__':
    unittest.main()
