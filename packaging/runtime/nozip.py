"""Reject ZIP files in newly created build directories without creating archives."""
from pathlib import Path
import sys
import zipfile


def is_zip_payload(source):
    """Recognize ZIP contents without mistaking Python signature constants for it.

    Older is_zipfile implementations only find an EOCD signature near EOF;
    zipfile's own bytecode contains that constant. Validate the central directory
    through a read-only ZipFile before classifying an embedded/renamed archive.
    A ZIP header at the beginning is rejected even if the archive is truncated.
    """
    if not hasattr(source, 'read'):
        with open(source, 'rb') as stream:
            return is_zip_payload(stream)
    position = source.tell()
    try:
        source.seek(0)
        if source.read(4) in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'):
            return True
        if not zipfile.is_zipfile(source):
            return False
        try:
            with zipfile.ZipFile(source, mode='r'):
                return True
        except (zipfile.BadZipFile, ValueError, OSError):
            return False
    finally:
        source.seek(position)


def check_tree(root):
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError('build output to inspect does not exist')
    files = [root] if root.is_file() else root.rglob('*')
    count = 0
    for path in files:
        if path.is_file():
            if path.suffix.lower() == '.zip' or is_zip_payload(path):
                raise ValueError('ZIP file found in build output: ' + str(path.relative_to(root) if path != root else path.name))
            count += 1
    return count


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Usage: nozip.py new-build-directory [new-build-directory ...]')
    print('No ZIP files:', sum(check_tree(path) for path in sys.argv[1:]), 'files inspected')
