"""Reject ZIP files in newly created build directories without creating archives."""
from pathlib import Path
import sys
import zipfile


def check_tree(root):
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError('build output to inspect does not exist')
    files = [root] if root.is_file() else root.rglob('*')
    count = 0
    for path in files:
        if path.is_file():
            if path.suffix.lower() == '.zip' or zipfile.is_zipfile(path):
                raise ValueError('ZIP file found in build output: ' + str(path.relative_to(root) if path != root else path.name))
            count += 1
    return count


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Usage: nozip.py new-build-directory [new-build-directory ...]')
    print('No ZIP files:', sum(check_tree(path) for path in sys.argv[1:]), 'files inspected')
