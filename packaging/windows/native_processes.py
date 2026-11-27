"""Windows process/window observations for acceptance, without extra packages."""
import ctypes
from ctypes import wintypes
import os
import subprocess
import time


def process_snapshot():
    if os.name != 'nt':
        raise RuntimeError('Native Windows acceptance must run on Windows')

    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result, entry = {}, ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while valid:
            result[entry.th32ProcessID] = {'parent': entry.th32ParentProcessID, 'exe': entry.szExeFile}
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return result


def descendants(pid, snapshot=None):
    snapshot = process_snapshot() if snapshot is None else snapshot
    found = {pid}
    while True:
        children = {child for child, info in snapshot.items() if info['parent'] in found}
        if children <= found:
            return found - {pid}
        found.update(children)


def wait_for(predicate, timeout=30, description='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Timed out waiting for ' + description)


def kill_tree(pid):
    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def visible_windows(pids):
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    results = []

    @callback_type
    def callback(handle, _parameter):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if pid.value in pids and user.IsWindowVisible(handle):
            title, rect = ctypes.create_unicode_buffer(512), wintypes.RECT()
            user.GetWindowTextW(handle, title, len(title))
            user.GetWindowRect(handle, ctypes.byref(rect))
            results.append({'handle': int(handle), 'pid': pid.value, 'title': title.value,
                            'width': rect.right - rect.left, 'height': rect.bottom - rect.top})
        return True

    if not user.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return results


def close_window(handle):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    if not user.PostMessageW(handle, 0x10, 0, 0):  # WM_CLOSE follows the app's normal shutdown handler.
        raise ctypes.WinError(ctypes.get_last_error())
