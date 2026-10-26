"""_run_scanimage stall/hard-limit watchdog (kills the whole process group), run_group (same for
ocrmypdf/unoconv/soffice), _scan_pages (0-byte pages don't count), _readable_pages (half-written
last page dropped)."""
import io
import os
import subprocess
import sys
import time

import pytest
from PIL import Image

import station_support as sup

GROUP_SCRIPT = """#!/bin/sh
# leader + a helper child in the same process group, like scanimage + a backend helper
[ "$2" = warm ] && exit 0
sleep 60 &
echo $! > "$1/child.pid"
echo $$ > "$1/leader.pid"
%s
wait
"""


def group_script(path, body=""):
    """Write the script and exec it once: the first exec of a brand-new file on macOS can take
    >0.5 s (security scan), which would otherwise race the watchdog under test."""
    sup.write_exe(path, GROUP_SCRIPT % body)
    subprocess.run([path, os.path.dirname(path), "warm"], check=True, timeout=30)
    return path


def _pids(d):
    for name in ("leader.pid", "child.pid"):
        assert os.path.exists(os.path.join(d, name)), "script was killed before it started its helper"
    return int(open(os.path.join(d, "leader.pid")).read()), int(open(os.path.join(d, "child.pid")).read())


def test_run_scanimage_returns_rc_and_stderr(m, tmp_path, fast_time):
    work = tmp_path / "w"
    work.mkdir()
    script = sup.write_exe(str(tmp_path / "s.sh"), "#!/bin/sh\necho 'feeder empty' >&2\nexit 7\n")
    rc, err = m._run_scanimage([script], str(work))
    assert rc == 7 and err.strip() == "feeder empty"


def test_stall_kills_whole_process_group(m, tmp_path, fast_time, monkeypatch):
    monkeypatch.setattr(m, "SCAN_STALL_SEC", 0.8)
    work = tmp_path / "w"
    work.mkdir()
    script = group_script(str(tmp_path / "g.sh"))
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        m._run_scanimage([script, str(tmp_path)], str(work))
    assert time.time() - t0 < 5
    leader, child = _pids(str(tmp_path))
    assert sup.pid_gone(leader) and sup.pid_gone(child), "process group not killed"


def test_pages_keep_resetting_the_stall_timer_until_hard_limit(m, tmp_path, fast_time, monkeypatch):
    monkeypatch.setattr(m, "SCAN_STALL_SEC", 0.8)
    monkeypatch.setattr(m, "SCAN_HARD_SEC", 1.8)
    work = tmp_path / "w"
    work.mkdir()
    loop = ('i=0; while true; do i=$((i+1)); printf x > "%s/p$(printf %%03d $i).png"; sleep 0.25; done &'
            % work)
    script = group_script(str(tmp_path / "e.sh"), loop)
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        m._run_scanimage([script, str(tmp_path)], str(work))
    took = time.time() - t0
    assert 1.7 < took < 5, "a steadily-feeding batch must only stop at the hard limit (took %.1fs)" % took
    assert len(m._scan_pages(str(work))) >= 4
    leader, child = _pids(str(tmp_path))
    assert sup.pid_gone(leader) and sup.pid_gone(child)


def test_run_group_timeout_kills_grandchildren(m, tmp_path):
    script = group_script(str(tmp_path / "g.sh"))
    with pytest.raises(subprocess.TimeoutExpired):
        m.run_group([script, str(tmp_path)], timeout=0.8)
    leader, child = _pids(str(tmp_path))
    assert sup.pid_gone(leader) and sup.pid_gone(child), "orphaned helper left running"


def test_run_group_normal_result(m, tmp_path):
    r = m.run_group([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"],
                    timeout=10)
    assert (r.returncode, r.stdout.strip(), r.stderr) == (3, "out", "err")


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (200, 300), (250, 250, 250)).save(buf, "PNG", dpi=(100, 100))
    return buf.getvalue()


def test_scan_pages_ignores_zero_byte_and_non_pages(m, tmp_path):
    good = _png_bytes()
    (tmp_path / "p001.png").write_bytes(good)
    (tmp_path / "p002.png").write_bytes(b"")
    (tmp_path / "p003.png").write_bytes(good)
    (tmp_path / "scanimage.err").write_text("x")
    (tmp_path / "q001.png").write_bytes(good)
    assert [os.path.basename(p) for p in m._scan_pages(str(tmp_path))] == ["p001.png", "p003.png"]


def test_readable_pages_drops_truncated(m, tmp_path):
    good = _png_bytes()
    paths = []
    for name, data in (("p001.png", good), ("p002.png", good[: len(good) // 2]), ("p003.png", b"junk")):
        (tmp_path / name).write_bytes(data)
        paths.append(str(tmp_path / name))
    assert m._readable_pages(paths) == paths[:1]
