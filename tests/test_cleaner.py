"""cleaner(): one pass (its 30 s sleep is made to raise). Expired scans / print jobs / orphan work
dirs / .part files go; the running scan's work dir and in-flight .part never do; TMPDIR 'tmp' stays."""
import os
import time

import pytest

import station_support as sup


class StopCleaner(Exception):
    pass


def one_pass(m, monkeypatch):
    clock = sup.FastTime()

    def sleep(s):
        raise StopCleaner()
    clock.sleep = sleep
    monkeypatch.setattr(m, "time", clock)
    with pytest.raises(StopCleaner):
        m.cleaner()


def touch(path, age, is_dir=False):
    if is_dir:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "x"), "w") as f:
            f.write("x")
    else:
        with open(path, "wb") as f:
            f.write(b"%PDF-")
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def test_expired_things_are_removed_fresh_things_kept(m, monkeypatch):
    old, new = m.RETAIN_SEC + 60, 60
    s, j = m.SCANS, m.JOBS
    gone = [touch(os.path.join(s, "scan-20260923-080000-1p.pdf"), old),
            touch(os.path.join(j, "a" * 32), old, True),
            touch(os.path.join(s, ".work-20260923-080000-abcdef"), old, True),
            touch(os.path.join(s, ".part-scan-20260923-080000-1p.pdf"), old)]
    kept = [touch(os.path.join(s, "scan-20260923-090000-2p.pdf"), new),
            touch(os.path.join(j, "b" * 32), new, True),
            touch(os.path.join(j, "tmp"), old, True)]
    one_pass(m, monkeypatch)
    for p in gone:
        assert not os.path.exists(p), p
    for p in kept:
        assert os.path.exists(p), p


def test_running_scan_is_never_cleaned(m, monkeypatch):
    old = m.RETAIN_SEC + 60
    active = touch(os.path.join(m.SCANS, ".work-20260923-080000-active"), old, True)
    orphan = touch(os.path.join(m.SCANS, ".work-20260923-070000-orphan"), old, True)
    part = touch(os.path.join(m.SCANS, ".part-scan-20260923-080000-9p.pdf"), old)   # long OCR in flight
    assert m.scan_lock.acquire(blocking=False)
    try:
        m.scanner.update(workdir=active, job="j")
        one_pass(m, monkeypatch)
    finally:
        m.scan_lock.release()
    assert os.path.exists(active) and os.path.exists(part)
    assert not os.path.exists(orphan)


def test_tmpdir_entries_older_than_a_day_are_cleared(m, monkeypatch):
    """JOBS/tmp is the container TMPDIR: leftovers of killed OSD/LibreOffice/ocrmypdf runs go after 24 h;
    younger ones (possibly still in use) stay, and the directory itself is never removed."""
    tmp = os.path.join(m.JOBS, "tmp")
    os.makedirs(tmp)
    day = 24 * 3600
    gone = [touch(os.path.join(tmp, "tmpab12cd.png"), day + 60),
            touch(os.path.join(tmp, "lu1234.tmp"), day + 60, True)]
    kept = [touch(os.path.join(tmp, "tmpfresh.png"), 2 * 3600),         # older than RETAIN_SEC, < 1 day
            touch(os.path.join(tmp, "ocrmypdf.io.x"), day - 60, True)]
    t = time.time() - 10 * day
    os.utime(tmp, (t, t))                               # the dir itself looks ancient
    one_pass(m, monkeypatch)
    assert os.path.isdir(tmp), "TMPDIR itself must never be deleted"
    for p in gone:
        assert not os.path.exists(p), p
    for p in kept:
        assert os.path.exists(p), p


def test_errors_do_not_kill_the_loop(m, monkeypatch):
    def boom(pattern):
        raise OSError("disk went away")
    monkeypatch.setattr(m.glob, "glob", boom)
    one_pass(m, monkeypatch)                     # reached the sleep: the exception was contained
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tcleaner-error\t" in f.read()
