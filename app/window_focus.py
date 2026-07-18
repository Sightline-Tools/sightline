from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


def get_foreground_process_name() -> str | None:
    """Return active foreground executable name in lowercase, or None on failure."""
    if sys.platform != "win32":
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetModuleBaseNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    psapi.GetModuleBaseNameW.restype = wintypes.DWORD

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if process_id.value == 0:
        return None

    process_handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
        False,
        process_id.value,
    )
    if not process_handle:
        return None

    try:
        buffer_size = 260
        name_buffer = ctypes.create_unicode_buffer(buffer_size)
        written = psapi.GetModuleBaseNameW(process_handle, None, name_buffer, buffer_size)
        if written == 0:
            return None
        executable = os.path.basename(name_buffer.value).strip().lower()
        return executable or None
    finally:
        kernel32.CloseHandle(process_handle)
