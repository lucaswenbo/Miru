"""Local file transactions shared by threads and cooperating processes."""

from contextlib import contextmanager
import os
import tempfile
import threading
import weakref


_locks = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()
_held = threading.local()


@contextmanager
def file_transaction(path):
    """Lock an entire read/modify/write, including nested calls on this path.

    The sidecar lock file stays in place: unlinking it would let another
    process lock a different inode while the original lock is still held.
    """
    path = os.path.realpath(os.fspath(path))
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _locks[path] = lock
    with lock:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if path in held:
            yield
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a+b") as handle:
            if os.name == "nt":
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held.add(path)
            try:
                yield
            finally:
                held.remove(path)
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path, text):
    """Replace a UTF-8 file using a private, unique temporary sibling.

    Callers doing read/modify/write must also hold file_transaction(path).
    A failed serialization/write leaves the previous file intact.
    """
    path = os.path.realpath(os.fspath(path))
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".miru-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
