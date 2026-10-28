"""One advisory library lock shared by file-plan execution and undo."""
from contextlib import contextmanager


@contextmanager
def library_lock(sb):
    import fcntl

    folder = sb.root / '.oplog'
    if folder.is_symlink():
        raise ValueError('内部执行目录或锁不能是符号链接')
    folder.mkdir(exist_ok=True)
    lock = folder / 'file-plans.lock'
    if lock.is_symlink():
        raise ValueError('内部执行目录或锁不能是符号链接')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('资料库已有计划正在执行或撤销，请稍后重试') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
