#!/usr/bin/env python3
"""Prepare isolated build tools without writing downloaded ZIP/WHL archives.

Published wheels are read in memory, hash checked, then extracted as ordinary
files. No dependency build or automatic install into the user's Python occurs.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import urllib.request
import zipfile


def extract_files(archive, target):
    """Validate every entry before extracting, preserving existing identical files."""
    target = Path(target).resolve()
    entries = []
    for member in archive.infolist():
        if member.is_dir():
            continue
        relative = PurePosixPath(member.filename)
        mode = member.external_attr >> 16
        if (relative.is_absolute() or '..' in relative.parts or '\\' in member.filename
                or ':' in member.filename or stat.S_ISLNK(mode)):
            raise ValueError('Unsafe dependency path: ' + member.filename)
        payload = archive.read(member)
        if relative.suffix.lower() in ('.zip', '.whl') or payload.startswith((b'PK\x03\x04', b'PK\x05\x06')):
            raise ValueError('Dependency contains an embedded ZIP; no files extracted')
        output = target.joinpath(*relative.parts)
        if not output.resolve().is_relative_to(target):
            raise ValueError('Dependency path leaves target')
        if output.exists() and output.read_bytes() != payload:
            raise ValueError('Different existing dependency file: ' + member.filename)
        entries.append((output, payload, mode))
    for output, payload, mode in entries:
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.exists():
            with output.open('xb') as stream:
                stream.write(payload)
            if mode & 0o111:
                output.chmod(0o755)


def read_url(address):
    with urllib.request.urlopen(address, timeout=60) as response:
        return response.read()


def select_document(requirement, rank):
    """Respect exclusions, Python requirements and platform wheels before download."""
    from pip._vendor.packaging.utils import parse_wheel_filename, canonicalize_name
    from pip._vendor.packaging.version import Version, InvalidVersion
    from pip._vendor.packaging.specifiers import SpecifierSet
    current_python = Version('.'.join(map(str, sys.version_info[:3])))

    def compatible(entries, requires_python=''):
        if requires_python and current_python not in SpecifierSet(requires_python):
            return []
        result = []
        for entry in entries:
            if entry.get('yanked') or not entry['filename'].endswith('.whl'):
                continue
            supported = entry.get('requires_python')
            if supported and current_python not in SpecifierSet(supported):
                continue
            _name, _version, _build, tags = parse_wheel_filename(entry['filename'])
            matches = [rank[tag] for tag in tags if tag in rank]
            if matches:
                result.append((min(matches), entry))
        return result

    name = canonicalize_name(requirement.name)
    exact = next((value.version for value in requirement.specifier if value.operator == '==' and '*' not in value.version), None)
    address = 'https://pypi.org/pypi/' + name
    document = json.loads(read_url(address + ('/' + exact if exact else '') + '/json'))
    version = document['info']['version']
    choices = compatible(document['urls'], document['info'].get('requires_python'))
    if Version(version) in requirement.specifier and choices:
        return document, min(choices, key=lambda item: item[0])[1]
    index = document if 'releases' in document else json.loads(read_url(address + '/json'))
    versions = []
    for candidate in index['releases']:
        try:
            parsed = Version(candidate)
        except InvalidVersion:
            continue
        if parsed in requirement.specifier:
            versions.append((parsed, candidate))
    for _parsed, candidate in sorted(versions, reverse=True):
        if not compatible(index['releases'][candidate]):
            continue
        document = json.loads(read_url(address + '/' + candidate + '/json'))
        choices = compatible(document['urls'], document['info'].get('requires_python'))
        if choices:
            return document, min(choices, key=lambda item: item[0])[1]
    raise RuntimeError('No compatible published dependency selected: ' + name)


def prepare(target, requirements=None):
    # pip is present in the supported build interpreters. Its already-installed
    # compatibility parser avoids bootstrapping another archive onto disk.
    try:
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.utils import canonicalize_name
        from pip._vendor.packaging.tags import sys_tags
        from pip._vendor.packaging.version import Version
    except ImportError:
        raise RuntimeError('The build Python must already include pip; no installer was downloaded') from None
    if requirements is None:
        requirements = ['pyinstaller==' + ('6.16.0' if os.name == 'nt' else '6.22.3'),
                        'certifi==2026.10.23']
        if sys.platform == 'darwin':
            requirements.append('pycdlib==1.22.0')
    queue, installed, digests = list(requirements), {}, {}
    rank = {tag: index for index, tag in enumerate(sys_tags())}
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    while queue:
        requirement = Requirement(queue.pop(0))
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        if name in installed:
            if Version(installed[name]) not in requirement.specifier:
                raise RuntimeError('Incompatible dependency versions: ' + name)
            continue
        document, entry = select_document(requirement, rank)
        version = document['info']['version']
        raw = read_url(entry['url'])
        digest = hashlib.sha256(raw).hexdigest()
        if digest != entry['digests']['sha256']:
            raise RuntimeError('Dependency hash mismatch: ' + name)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            extract_files(archive, target)
        installed[name], digests[name] = version, digest
        queue.extend(document['info'].get('requires_dist') or [])
        print(name, version, flush=True)
    record = {'method': 'Existing published wheels read in memory; no ZIP/WHL files written',
              'packages': installed, 'published_sha256': digests}
    (target.parent / 'build-tools.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    options = parser.parse_args()
    prepare(options.target)
