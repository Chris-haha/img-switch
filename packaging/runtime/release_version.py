"""Read the checked-in version and inject macOS version metadata before signing."""
import argparse
from pathlib import Path
import re
import runpy


def read_version(root):
    version = runpy.run_path(str(Path(root) / 'version.py'))
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version['VERSION']):
        raise ValueError('invalid release VERSION')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version['NUMERIC_VERSION']):
        raise ValueError('invalid NUMERIC_VERSION')
    if not re.fullmatch(r'\d+', version['BUILD_NUMBER']):
        raise ValueError('invalid BUILD_NUMBER')
    if version['VERSION'].split('-', 1)[0] != version['NUMERIC_VERSION']:
        raise ValueError('VERSION and NUMERIC_VERSION disagree')
    return {key: version[key] for key in ('VERSION', 'NUMERIC_VERSION', 'BUILD_NUMBER')}


def patch_bundle(spec, version):
    spec = Path(spec)
    text = spec.read_text(encoding='utf-8')
    marker = 'app = BUNDLE('
    if text.count(marker) != 1:
        raise ValueError('Unexpected macOS bundle spec')
    info = {'CFBundleShortVersionString': version['NUMERIC_VERSION'],
            'CFBundleVersion': version['BUILD_NUMBER'], 'IMGSwitchVersion': version['VERSION']}
    wrapper = ("def _versioned_bundle(*args, **kwargs):\n"
               "    kwargs['version'] = %r\n"
               "    kwargs['info_plist'] = %r\n"
               "    return BUNDLE(*args, **kwargs)\n\n" % (version['NUMERIC_VERSION'], info))
    spec.write_text(text.replace(marker, wrapper + 'app = _versioned_bundle('), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--field', choices=('VERSION', 'NUMERIC_VERSION', 'BUILD_NUMBER'))
    parser.add_argument('--patch-spec', type=Path)
    args = parser.parse_args()
    version = read_version(args.root)
    if args.patch_spec:
        patch_bundle(args.patch_spec, version)
    if args.field:
        print(version[args.field])
