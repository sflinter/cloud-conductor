import os
import threading
import time

from conductor.filelock import locked, lock_path


def test_lock_path_is_hidden_sibling():
    assert lock_path("/a/b/state.json") == "/a/b/.state.json.lock"


def test_locked_serialises_threads(tmp_dir):
    path = os.path.join(tmp_dir, "state.json")
    counter = {"n": 0, "max_concurrent": 0, "active": 0}
    guard = threading.Lock()

    def worker():
        for _ in range(20):
            with locked(path):
                with guard:
                    counter["active"] += 1
                    counter["max_concurrent"] = max(counter["max_concurrent"], counter["active"])
                n = counter["n"]
                time.sleep(0.0005)
                counter["n"] = n + 1
                with guard:
                    counter["active"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter["max_concurrent"] == 1
    assert counter["n"] == 80
    assert os.path.exists(lock_path(path))


def test_locked_releases_on_exception(tmp_dir):
    path = os.path.join(tmp_dir, "x.json")
    try:
        with locked(path):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Re-acquiring must not block
    with locked(path):
        pass
