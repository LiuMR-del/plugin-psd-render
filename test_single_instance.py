import os
import subprocess
import sys
import tempfile
import unittest

import single_instance


class SingleInstanceTests(unittest.TestCase):
    def test_second_process_cannot_take_the_lock(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lock-"), "app.lock")
        handle = single_instance.acquire(path)
        self.assertIsNotNone(handle)
        code = ("import single_instance, sys; "
                f"sys.exit(0 if single_instance.acquire({path!r}) is None else 1)")
        result = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(single_instance.__file__))
        self.assertEqual(result.returncode, 0, "second process must see the app as running")
        handle.close()
        result = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(single_instance.__file__))
        self.assertEqual(result.returncode, 1, "lock is free again after the first process releases it")


if __name__ == "__main__":
    unittest.main()
