"""reap_orphans (container PID 1 zombie reaper): /proc parsing + the two-consecutive-passes rule,
driven through a fake os/open/time. A zombie seen in only one pass is NOT reaped (it may be a
subprocess child that subprocess itself is about to wait for); one seen in two passes IS."""
import builtins
import io
import os

import pytest

import station_support as sup


class StopReaper(Exception):
    pass


def stat_line(pid, comm, state, ppid):
    return "%d (%s) %s %d 1 1 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 100 0 0\n" % (pid, comm, state, ppid)


class FakeProc:
    """passes[i] = {pid: (comm, state, ppid) | OSError} as seen during pass i (1-based)."""

    def __init__(self, passes, fail_listdir_on=()):
        self.passes = passes
        self.fail_listdir_on = set(fail_listdir_on)
        self.n = 0                        # current pass number (incremented by sleep)
        self.reaped = []                  # (pass, pid, flags)
        self.gone = set()

    # --- os
    def getpid(self):
        return 1

    def listdir(self, path):
        assert path == "/proc"
        if self.n in self.fail_listdir_on:
            raise PermissionError("/proc unreadable")
        table = self.passes.get(self.n, {})
        return ["self", "sys", "1"] + [str(p) for p in table if p not in self.gone] + ["cpuinfo"]

    def waitpid(self, pid, flags):
        self.reaped.append((self.n, pid, flags))
        if pid == 106:
            raise ChildProcessError(10, "No child processes")
        self.gone.add(pid)
        return pid, 0

    # --- builtins.open replacement for /proc/<pid>/stat
    def open(self, path, *a, **kw):
        if not str(path).startswith("/proc/"):
            return builtins.open(path, *a, **kw)
        pid = int(str(path).split("/")[2])
        if pid == 1:
            return io.StringIO(stat_line(1, "python3", "S", 0))
        entry = self.passes.get(self.n, {}).get(pid)
        if entry is None or isinstance(entry, Exception) or pid in self.gone:
            raise entry if isinstance(entry, Exception) else FileNotFoundError(path)
        return io.StringIO(stat_line(pid, *entry))


class OsProxy:
    def __init__(self, fake):
        self._fake = fake

    def __getattr__(self, name):
        if name in ("getpid", "listdir", "waitpid"):
            return getattr(self._fake, name)
        return getattr(os, name)


@pytest.fixture()
def reaper(m, monkeypatch):
    def run(passes, max_passes=3, **kw):
        fake = FakeProc(passes, **kw)
        clock = sup.FakeClock()

        def sleep(s):
            assert s == 30
            if fake.n >= max_passes:
                raise StopReaper()
            fake.n += 1
        clock.sleep = sleep
        monkeypatch.setattr(m, "os", OsProxy(fake))
        monkeypatch.setattr(m, "open", fake.open, raising=False)
        monkeypatch.setattr(m, "time", clock)
        with pytest.raises(StopReaper):
            m.reap_orphans()
        return fake
    return run


Z = ("gpg-agent", "Z", 1)


def test_two_pass_rule(m, reaper):
    fake = reaper({
        1: {100: Z, 101: Z, 102: Z, 103: ("gpgsm", "Z", 55), 105: OSError("vanished"), 106: Z,
            107: ("soffice.bin", "S", 1)},
        2: {100: Z, 102: ("gpg-agent", "S", 1), 103: ("gpgsm", "Z", 55), 104: ("weird) name (x", "Z", 1),
            106: Z, 107: ("soffice.bin", "S", 1)},
        3: {102: Z, 103: ("gpgsm", "Z", 55), 104: ("weird) name (x", "Z", 1), 107: ("soffice.bin", "S", 1)},
    })
    assert sorted(fake.reaped) == [(2, 100, os.WNOHANG), (2, 106, os.WNOHANG), (3, 104, os.WNOHANG)]
    reaped = {pid for _, pid, _ in fake.reaped}
    assert 101 not in reaped          # zombie in one pass only
    assert 102 not in reaped          # zombie, alive, zombie: never two passes in a row
    assert 103 not in reaped          # not our child (ppid 55)
    assert 107 not in reaped          # running child is never touched


def test_not_pid1_does_nothing(m, monkeypatch):
    called = []
    monkeypatch.setattr(m, "os", type("O", (), {"getpid": staticmethod(lambda: 4242),
                                                "listdir": staticmethod(lambda p: called.append(p))})())
    clock = sup.FakeClock()
    monkeypatch.setattr(m, "time", clock)
    assert m.reap_orphans() is None
    assert clock.sleeps == [] and called == []


def test_error_in_a_pass_is_logged_and_loop_survives(m, reaper):
    fake = reaper({1: {100: Z}, 2: {100: Z}, 3: {100: Z}}, fail_listdir_on={2})
    # pass 2 failed → pass 3 compares against pass 1's set (seen is only updated on success)
    assert [(n, pid) for n, pid, _ in fake.reaped] == [(3, 100)]
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\treaper-error\t" in f.read()
