import os
import shutil
import tempfile
import unittest
from lock_utils import ConcurrencyLock


class TestConcurrencyLock(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.lock_file = os.path.join(self.test_dir, ".test.lock")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_acquire_and_release(self):
        lock = ConcurrencyLock(self.lock_file, timeout=1.0)
        with lock:
            self.assertTrue(os.path.exists(self.lock_file))
        # After exit, lock should be released
        lock2 = ConcurrencyLock(self.lock_file, timeout=1.0)
        with lock2:
            self.assertTrue(True)

    def test_reentrancy(self):
        lock1 = ConcurrencyLock(self.lock_file, timeout=1.0)
        lock2 = ConcurrencyLock(self.lock_file, timeout=1.0)
        with lock1:
            # Nested acquire on same thread should succeed without blocking
            with lock2:
                self.assertTrue(True)

    def test_timeout_when_locked(self):
        import threading
        acquired_evt = threading.Event()
        release_evt = threading.Event()

        def worker():
            lock1 = ConcurrencyLock(self.lock_file, timeout=2.0)
            lock1.acquire()
            acquired_evt.set()
            release_evt.wait()
            lock1.release()

        t = threading.Thread(target=worker)
        t.start()
        acquired_evt.wait()

        lock2 = ConcurrencyLock(self.lock_file, timeout=0.3, poll_interval=0.05)
        try:
            with self.assertRaises(TimeoutError):
                lock2.acquire()
        finally:
            release_evt.set()
            t.join()

        # Now lock2 should be able to acquire
        self.assertTrue(lock2.acquire())
        lock2.release()


if __name__ == "__main__":
    unittest.main()
