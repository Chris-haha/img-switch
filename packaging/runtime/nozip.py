"""Reject ZIP files in newly created build directories without creating archives."""
from pathlib import Path
import importlib.util
import marshal
import sys
import types
import zipfile


def is_complete_bytecode(stream):
    """Check one complete current-interpreter pyc without executing its code."""
    position = stream.tell()
    try:
        stream.seek(0)
        header = stream.read(16)
        if (len(header) != 16 or header[:4] != importlib.util.MAGIC_NUMBER
                or int.from_bytes(header[4:8], 'little') not in (0, 1, 3)):
            return False
        try:
            code = marshal.load(stream)
        except (EOFError, ValueError, TypeError):
            return False
        return isinstance(code, types.CodeType) and stream.read(1) == b''
    finally:
        stream.seek(position)


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
        # zipimport's bytecode contains a complete empty EOCD constant, so even
        # ZipFile can accept that ordinary pyc as an empty archive. An exact
        # current-version code object with no trailing bytes identifies it.
        if is_complete_bytecode(source):
            return False
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
