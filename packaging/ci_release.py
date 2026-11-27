"""Publish only native-verified raw installers; never use ZIP artifact transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('windows-x64', 'macos-arm64', 'macos-x86_64')


def artifact_name(version, target):
    suffix = {'windows-x64': 'Windows-x64-unsigned-setup.exe',
              'macos-arm64': 'macOS-arm64-unsigned.dmg',
              'macos-x86_64': 'macOS-x86_64-unsigned.dmg'}[target]
    return 'IMG-Switch-' + version + '-' + suffix


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != 'https' or not (target.hostname == 'github.com' or
                target.hostname.endswith('.github.com') or target.hostname.endswith('.githubusercontent.com')):
            raise RuntimeError('Unexpected GitHub download redirect')
        if target.hostname != urllib.parse.urlsplit(request.full_url).hostname:
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repo):
            raise RuntimeError('Invalid repository')
        self.token = os.environ['GITHUB_TOKEN']
        self.base = 'https://api.github.com/repos/' + self.repo
        self.opener = urllib.request.build_opener(Redirect())

    def request(self, path, method='GET', value=None, raw=None, binary=False, missing_ok=False):
        address = path if path.startswith('https://') else self.base + path
        host = urllib.parse.urlsplit(address).hostname
        if host not in ('api.github.com', 'uploads.github.com'):
            raise RuntimeError('Unsupported API host')
        headers = {'Authorization': 'Bearer ' + self.token, 'X-GitHub-Api-Version': '2022-11-28',
                   'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
                   'User-Agent': 'img-switch-release'}
        data = raw
        if value is not None:
            data = json.dumps(value).encode()
            headers['Content-Type'] = 'application/json'
        elif raw is not None:
            headers['Content-Type'] = 'application/octet-stream'
        request = urllib.request.Request(address, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 404 and missing_ok:
                return None
            raise RuntimeError('GitHub API request failed: HTTP ' + str(status)) from None
        return payload if binary else json.loads(payload)

    def upload(self, release, name, payload):
        if name.lower().endswith(('.zip', '.whl')) or payload.startswith((b'PK\x03\x04', b'PK\x05\x06')):
            raise RuntimeError('ZIP transport is forbidden')
        existing = next((asset for asset in release.get('assets', []) if asset['name'] == name), None)
        if existing:
            previous = self.request('/releases/assets/' + str(existing['id']), binary=True)
            if previous == payload:
                return existing
            raise RuntimeError('Different existing asset will not be overwritten; rerun the source job and its dependents for a fresh draft: ' + name)
        address = release['upload_url'].split('{', 1)[0] + '?' + urllib.parse.urlencode({'name': name})
        asset = self.request(address, 'POST', raw=payload)
        if asset.get('size') != len(payload):
            raise RuntimeError('Uploaded asset size mismatch')
        digest = hashlib.sha256(payload).hexdigest()
        actual = asset.get('digest')
        if actual and actual != 'sha256:' + digest:
            raise RuntimeError('GitHub asset checksum mismatch')
        return asset


def identity():
    version = runpy.run_path(str(ROOT / 'version.py'))['VERSION']
    commit = os.environ['GITHUB_SHA']
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('Invalid source commit')
    if os.environ.get('GITHUB_REF') != 'refs/heads/codex/release-v' + version:
        raise RuntimeError('Publishing is only allowed on the explicit version release branch')
    return version, commit


def validate_evidence(record, target, commit):
    if record.get('commit') != commit or record.get('platform') != target:
        raise RuntimeError('Verification belongs to a different platform or source commit')
    if not record.get('tests_passed') or not record.get('installed_smoke_passed'):
        raise RuntimeError('Native tests and installed smoke must both pass')
    checks = record.get('checks')
    if not checks or any(check.get('passed') is not True for check in checks):
        raise RuntimeError('Verification contains missing or failed checks')
    if not any(check.get('name') == 'installed_smoke' for check in checks):
        raise RuntimeError('Installed application was not exercised')


def owned_draft(api, release_id, commit):
    release = api.request('/releases/' + str(release_id))
    marker = '<!-- img-switch commit:' + commit + ' -->'
    if not release['draft'] or release.get('target_commitish') != commit or marker not in release.get('body', ''):
        raise RuntimeError('Refusing to modify a published or unrelated release')
    return release


def prepare(api, version, commit):
    # The tested branch must already have been merged before release preparation.
    main = api.request('/git/ref/heads/main')
    if main['object']['sha'] != commit:
        raise RuntimeError('Release commit must be the current main branch head')
    if api.request('/git/ref/tags/v' + version, missing_ok=True) is not None:
        raise RuntimeError('Version tag already exists; it will not be overwritten')
    if api.request('/releases/tags/v' + version, missing_ok=True) is not None:
        raise RuntimeError('Version release already exists; choose a new version')
    run_id, attempt = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT']
    draft_tag = 'verify-' + version + '-' + run_id + '-' + attempt
    releases = api.request('/releases?per_page=100')
    for release in releases:
        if release['tag_name'] == draft_tag:
            return owned_draft(api, release['id'], commit)['id']
    release = api.request('/releases', 'POST', value={
        'tag_name': draft_tag, 'target_commitish': commit, 'name': 'IMG Switch v' + version + ' — 验收中',
        'body': '<!-- img-switch commit:' + commit + ' -->', 'draft': True, 'prerelease': True})
    return release['id']


def upload(api, release_id, version, commit, target, package, evidence):
    release = owned_draft(api, release_id, commit)
    record = json.loads(evidence.read_text(encoding='utf-8'))
    validate_evidence(record, target, commit)
    if package.name != artifact_name(version, target) or package.is_symlink():
        raise RuntimeError('Unexpected installer filename')
    raw = package.read_bytes()
    record['artifact'] = {'name': package.name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    record['ci_run'] = 'https://github.com/' + api.repo + '/actions/runs/' + os.environ['GITHUB_RUN_ID']
    api.upload(release, package.name, raw)
    api.upload(release, 'verification-' + target + '.json',
               (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
    print('Uploaded verified raw installer: ' + package.name)


def publish(api, release_id, version, commit):
    release = api.request('/releases/' + str(release_id))
    marker = '<!-- img-switch commit:' + commit + ' -->'
    already_published = not release['draft']
    if release.get('target_commitish') != commit or marker not in release.get('body', ''):
        raise RuntimeError('Refusing to modify a published or unrelated release')
    if already_published and release.get('tag_name') != 'v' + version:
        raise RuntimeError('Refusing to modify a published or unrelated release')
    assets = {asset['name']: asset for asset in release['assets']}
    expected = {artifact_name(version, target) for target in PLATFORMS}
    expected |= {'verification-' + target + '.json' for target in PLATFORMS}
    if not expected <= set(assets) or set(assets) - expected - {'SHA256SUMS.txt'}:
        raise RuntimeError('The draft does not contain exactly all three verified platforms')
    checksums = []
    for target in PLATFORMS:
        evidence = assets['verification-' + target + '.json']
        record = json.loads(api.request('/releases/assets/' + str(evidence['id']), binary=True))
        validate_evidence(record, target, commit)
        artifact = record['artifact']
        name = artifact_name(version, target)
        if artifact['name'] != name or artifact['bytes'] != assets[name]['size']:
            raise RuntimeError('Installer evidence mismatch')
        # Re-read the actual uploaded binary, never trust a local manifest alone.
        binary = api.request('/releases/assets/' + str(assets[name]['id']), binary=True)
        if hashlib.sha256(binary).hexdigest() != artifact['sha256']:
            raise RuntimeError('Downloaded installer checksum mismatch')
        checksums.append(artifact['sha256'] + '  ' + name)
    checksum_bytes = ('\n'.join(checksums) + '\n').encode()
    if 'SHA256SUMS.txt' in assets:
        previous = api.request('/releases/assets/' + str(assets['SHA256SUMS.txt']['id']), binary=True)
        if previous != checksum_bytes:
            raise RuntimeError('Existing checksum file differs; refusing overwrite')
    elif already_published:
        raise RuntimeError('Published release is missing its checksum file; no changes made')
    else:
        api.upload(release, 'SHA256SUMS.txt', checksum_bytes)
    if already_published:
        print('Verified existing published release ' + release['html_url'])
        return
    if api.request('/git/ref/tags/v' + version, missing_ok=True) is not None:
        raise RuntimeError('Version tag appeared during verification; refusing overwrite')
    notes = (ROOT / 'docs/releases' / ('v' + version + '.md')).read_text(encoding='utf-8')
    notes += '\n\n原生验收：Windows x64、macOS Intel、macOS Apple Silicon。各平台 verification JSON 和 SHA256SUMS 随安装包提供。'
    notes += '\n\n验收源码：`' + commit + '`。'
    notes += '\n\n[完整构建与运行记录](https://github.com/' + api.repo + '/actions/runs/' + os.environ['GITHUB_RUN_ID'] + ')'
    notes += '\n\n' + marker
    result = api.request('/releases/' + str(release_id), 'PATCH', value={
        'tag_name': 'v' + version, 'target_commitish': commit, 'name': 'IMG Switch v' + version + '（测试版）',
        'body': notes, 'draft': False, 'prerelease': True})
    print('Published ' + result['html_url'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'upload', 'publish'))
    parser.add_argument('--release-id', type=int)
    parser.add_argument('--platform', choices=PLATFORMS)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    version, commit = identity()
    api = GitHub()
    if args.action == 'prepare':
        release_id = prepare(api, version, commit)
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('release_id=' + str(release_id) + '\n')
        print('Prepared private draft ' + str(release_id))
    elif args.action == 'upload':
        if not all((args.release_id, args.platform, args.package, args.evidence)):
            parser.error('upload requires release ID, platform, package and evidence')
        upload(api, args.release_id, version, commit, args.platform, args.package, args.evidence)
    else:
        if not args.release_id:
            parser.error('publish requires release ID')
        publish(api, args.release_id, version, commit)


if __name__ == '__main__':
    main()
