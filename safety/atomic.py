"""Atomic no-overwrite rename. Never fall back to an overwriting rename."""
import ctypes
import errno
import os
import sys


def rename_no_replace(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        result = libc.renamex_np(os.fsencode(source), os.fsencode(target), ctypes.c_uint(4))  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), ctypes.c_uint(1))  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, '当前平台缺少原子禁止覆盖移动接口，未执行')
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(target))
