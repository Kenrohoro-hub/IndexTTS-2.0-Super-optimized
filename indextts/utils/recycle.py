"""Windows Recycle Bin support for user data files."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    )


def recycle_path(path: os.PathLike | str) -> None:
    """Move an existing absolute path to the Windows Recycle Bin."""
    if os.name != "nt":
        raise NotImplementedError("回收站仅支持 Windows")

    target = Path(path)
    if not target.is_absolute():
        raise ValueError("回收路径必须是绝对路径")
    if not target.exists():
        raise FileNotFoundError(target)

    operation = _SHFILEOPSTRUCTW(
        None,
        3,
        str(target) + "\0\0",
        None,
        0x0004 | 0x0010 | 0x0040 | 0x0400,
        False,
        None,
        None,
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    result = shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise OSError(result or 1, f"无法将路径移入回收站：{target}")
