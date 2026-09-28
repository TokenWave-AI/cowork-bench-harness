"""Host-local prepare/grading limits shared by independent worker processes."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import time


class ProcessSlots:
    def __init__(self, root, name, capacity, cancelled=lambda: False):
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError('Invalid process slot capacity')
        self.directory = Path(root) / name
        self.capacity = capacity
        self.cancelled = cancelled

    @contextmanager
    def lease(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = None
        try:
            while descriptor is None:
                if self.cancelled():
                    raise InterruptedError('Stopped while waiting for a process slot')
                for number in range(self.capacity):
                    fd = os.open(self.directory/f'{number:03d}.lock', os.O_CREAT | os.O_RDWR, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        descriptor = fd
                        break
                    except BlockingIOError:
                        os.close(fd)
                if descriptor is None:
                    time.sleep(0.2)
            yield
        finally:
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def __enter__(self):
        self._lease = self.lease()
        return self._lease.__enter__()

    def __exit__(self, *args):
        return self._lease.__exit__(*args)
