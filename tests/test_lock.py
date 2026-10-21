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

    def test_timeout_when_locked(self):
        lock1 = ConcurrencyLock(self.lock_file, timeout=2.0)
        lock2 = ConcurrencyLock(self.lock_file, timeout=0.5, poll_interval=0.1)

        lock1.acquire()
        try:
            with self.assertRaises(TimeoutError):
                lock2.acquire()
        finally:
            lock1.release()

        # Now lock2 should be able to acquire
        self.assertTrue(lock2.acquire())
        lock2.release()


if __name__ == "__main__":
    unittest.main()
