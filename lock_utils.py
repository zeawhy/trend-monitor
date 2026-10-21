import fcntl
import os
import threading
import time

_local = threading.local()


class ConcurrencyLock:
    """基于文件锁(fcntl.flock)的可重入跨进程并发锁。
    同一进程内支持递归/嵌套调用 (re-entrant)，避免嵌套调用时自锁。
    避免 --fast (每30分钟) 与 6小时全量任务或 CLI 同时读写 seen_terms / opportunities 等状态文件。
    """

    def __init__(self, lock_file=None, timeout=60.0, poll_interval=0.2):
        if not lock_file:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            lock_file = os.path.join(base_dir, "hidden_files", ".trend_watch.lock")
        self.lock_file = os.path.abspath(lock_file)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.fd = None

    def acquire(self):
        if not hasattr(_local, "locks"):
            _local.locks = {}

        # 检查本线程是否已持有该锁 (可重入)
        current_depth = _local.locks.get(self.lock_file, 0)
        if current_depth > 0:
            _local.locks[self.lock_file] = current_depth + 1
            return True

        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        self.fd = open(self.lock_file, "a+")
        start_time = time.time()
        while True:
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                _local.locks[self.lock_file] = 1
                return True
            except (BlockingIOError, IOError):
                if time.time() - start_time >= self.timeout:
                    try:
                        self.fd.close()
                    except Exception:
                        pass
                    self.fd = None
                    raise TimeoutError(f"无法获取并发锁 {self.lock_file} (等待超时 {self.timeout}s)")
                time.sleep(self.poll_interval)

    def release(self):
        if not hasattr(_local, "locks"):
            _local.locks = {}

        current_depth = _local.locks.get(self.lock_file, 0)
        if current_depth > 1:
            _local.locks[self.lock_file] = current_depth - 1
            return

        if current_depth == 1:
            _local.locks[self.lock_file] = 0

        if self.fd:
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self.fd.close()
            except Exception:
                pass
            self.fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
