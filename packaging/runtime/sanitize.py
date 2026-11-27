"""Remove CPython build paths from copies used to assemble the distribution.

The splitroot documentation example is replaced with equal-length spaces.
On macOS, debug symbols are stripped from extension-module copies and matching
CPython source-directory prefixes in Mach-O string sections are replaced with
equal-length placeholders. Original runtime files and licenses are untouched.
"""
from pathlib import Path
import ast
import importlib.util
import marshal
import types
import re
import sys
import zipfile
import hashlib
import shutil
import subprocess

EXAMPLE_LINE = re.compile(
    rb"(?m)^[ \t]*splitroot\('C:/Users/([A-Za-z0-9_-]+)'\) == "
    rb"\('C:', '/', 'Users/\1'\)[ \t]*\n"
)
CPYTHON_SOURCE = re.compile(
    rb'/Users/[^/\x00\s]+/build/v[0-9]+\.[0-9]+\.[0-9]+/'
    rb'((?:Modules|Parser|Objects|Python)/[A-Za-z0-9_./-]+\.(?:c|h))(?=\x00)'
)
CPYTHON_VPATH = re.compile(rb'(?<=VPATH\x00)/Users/[^/\x00\s]+/build/v[0-9]+\.[0-9]+\.[0-9]+(?=\x00PLATLIBDIR\x00)')
PRIVATE_PREFIX = re.compile(r'(?i)(?:/users/[^/\s\x00]+|/home/[^/\s\x00]+|[a-z]:\\users\\[^\\\s\x00]+)')


def scrub_sysconfig_pyc(data, name):
    """Relocate copied CPython build-time prefixes to the frozen runtime at import.

    Only the interpreter's known private installation prefixes are replaced.
    Code objects remain code objects so the privacy scanner can inspect them.
    The source interpreter and its sysconfig dictionaries are never modified.
    """
    if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
        raise ValueError('invalid or mismatched sysconfig bytecode')
    roots = sorted({value for value in (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)
                    if isinstance(value, str) and PRIVATE_PREFIX.search(value)}, key=len, reverse=True)
    marker = '__IMG_SWITCH_RUNTIME_PREFIX__'
    count = 0
    def scrub(value):
        nonlocal count
        if isinstance(value, str):
            for root in roots:
                count += value.count(root)
                value = value.replace(root, marker)
            if PRIVATE_PREFIX.search(value):
                raise ValueError('unknown private sysconfig build path requires explicit review')
            return value
        if isinstance(value, types.CodeType):
            return value.replace(co_consts=tuple(scrub(item) for item in value.co_consts),
                                 co_filename=scrub(value.co_filename))
        if isinstance(value, tuple):
            return tuple(scrub(item) for item in value)
        if isinstance(value, frozenset):
            return frozenset(scrub(item) for item in value)
        return value
    original = scrub(marshal.loads(data[16:]))
    if not count:
        return data, 0
    # Replace a LOAD_CONST string with the sanitized code object, then restore
    # runtime paths after its ordinary build_time_vars assignment has executed.
    wrapper = compile(
        "exec('__IMG_SWITCH_SYSCONFIG_CODE__')\n"
        "import sys as _img_switch_runtime_sys\n"
        "build_time_vars = {key: value.replace('__IMG_SWITCH_RUNTIME_PREFIX__', "
        "getattr(_img_switch_runtime_sys, '_MEIPASS', _img_switch_runtime_sys.prefix)) "
        "if isinstance(value, str) else value for key, value in build_time_vars.items()}\n"
        "del _img_switch_runtime_sys\n", Path(name).with_suffix('.py').name, 'exec')
    wrapper = wrapper.replace(co_consts=tuple(original if item == '__IMG_SWITCH_SYSCONFIG_CODE__' else item
                                              for item in wrapper.co_consts))
    return data[:16] + marshal.dumps(wrapper), count

def remove_example(data):
    return EXAMPLE_LINE.subn(lambda match: b' ' * (len(match.group()) - 1) + b'\n', data)

def scrub_cpython_source_paths(path, *, replace_vpath=False):
    """Remove only CPython __FILE__ prefixes inside Mach-O __cstring sections."""
    from macholib.MachO import MachO
    path = Path(path)
    data = bytearray(path.read_bytes())
    count = 0
    for header in MachO(str(path)).headers:
        for _load, _command, sections in header.commands:
            if not isinstance(sections, list):
                continue
            for section in sections:
                if section.sectname.rstrip(b'\0') != b'__cstring':
                    continue
                start = header.offset + section.offset
                end = start + section.size
                if start < 0 or end > len(data):
                    raise ValueError('Mach-O string section extends outside binary')
                for match in CPYTHON_SOURCE.finditer(data, start, end):
                    left, right = match.start(), match.start(1)
                    data[left:right] = b'.' * (right - left)
                    count += 1
                if replace_vpath:
                    for match in CPYTHON_VPATH.finditer(data, start, end):
                        # The frozen app supplies its own Python home; the original
                        # build-tree location must not participate in path discovery.
                        data[match.start():match.end()] = b'.' + b'\0' * (len(match.group()) - 1)
                        count += 1
    if count:
        path.write_bytes(data)
        print('Removed CPython source-build paths from runtime binary:', count)
    return count

def scrub_binaries(binaries, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for name, source, kind in binaries:
        filename = Path(name).name.lower()
        if sys.platform == 'darwin' and filename.endswith('.so'):
            subdir = directory / hashlib.sha256(name.encode('utf-8')).hexdigest()
            subdir.mkdir(parents=True, exist_ok=True)
            replacement = subdir / Path(name).name
            shutil.copyfile(source, replacement)
            subprocess.run(['strip', '-S', str(replacement)], check=True, capture_output=True)
            scrub_cpython_source_paths(replacement)
            source = str(replacement)
        elif filename == 'python' or (filename.startswith('libpython') and filename.endswith('.dylib')) or re.fullmatch(r'python3\d+\.dll', filename):
            if sys.platform == 'darwin':
                replacement = directory / Path(name).name
                shutil.copyfile(source, replacement)
                subprocess.run(['strip', '-S', str(replacement)], check=True, capture_output=True)
                scrub_cpython_source_paths(replacement, replace_vpath=True)
                source = str(replacement)
            data = Path(source).read_bytes()
            data, count = remove_example(data)
            if count:
                replacement = directory / Path(name).name
                replacement.write_bytes(data)
                source = str(replacement)
                print('Removed CPython documentation example from copied runtime:', count)
        result.append((name, source, kind))
    return result

def scrub_datas(datas, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for name, source, kind in datas:
        if Path(name).suffix.lower() == '.zip' or zipfile.is_zipfile(source):
            raise ValueError('ZIP runtime data is forbidden; rebuild with --debug noarchive')
        if Path(name).suffix.lower() == '.pyc':
            data, count = remove_example(Path(source).read_bytes())
            if Path(name).name.startswith('_sysconfigdata_'):
                data, relocated = scrub_sysconfig_pyc(data, name)
                count += relocated
            if count:
                replacement = directory / hashlib.sha256(name.encode('utf-8')).hexdigest() / Path(name).name
                replacement.parent.mkdir(parents=True, exist_ok=True)
                replacement.write_bytes(data)
                source = str(replacement)
                print('Sanitized copied CPython bytecode constants:', count)
        result.append((name, source, kind))
    return result

def patch_spec(spec, directory):
    spec = Path(spec)
    text = spec.read_text(encoding='utf-8')
    analyses = [node for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == 'Analysis']
    if len(analyses) != 1 or not any(keyword.arg == 'noarchive' and isinstance(keyword.value, ast.Constant)
                                    and keyword.value.value is True for keyword in analyses[0].keywords):
        raise ValueError('Refusing ZIP-producing spec: Analysis(noarchive=True) is required')
    marker = 'pyz = PYZ(a.pure)'
    if text.count(marker) != 1:
        raise ValueError('Unexpected PyInstaller spec structure')
    injection = ("import runpy\n"
                 "_scrub = runpy.run_path(%r)\n"
                 "a.binaries = _scrub['scrub_binaries'](a.binaries, %r)\n"
                 "a.datas = _scrub['scrub_datas'](a.datas, %r)\n\n" %
                 (str(Path(__file__).resolve()), str(Path(directory).resolve()), str(Path(directory).resolve())))
    bootstrap = "\npyz.dependencies = _scrub['scrub_binaries'](pyz.dependencies, %r)" % str(Path(directory).resolve() / 'bootstrap')
    spec.write_text(text.replace(marker, injection + marker + bootstrap), encoding='utf-8')

if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: sanitize.py spec-file copied-runtime-directory')
    patch_spec(sys.argv[1], sys.argv[2])
