"""/scan lock hand-over, parameter validation, /status per-job isolation, printer-status cache."""
import json
import re
import threading

import pytest

import station_support as sup

FORM = {"source": "Flatbed", "res": "300", "mode": "Color", "size": "full"}
REFRESH = "The station was updated. Refresh this page before continuing"      # legacy msg/pmsg for pre-deploy tabs


@pytest.fixture()
def blocking_scan(m, monkeypatch):
    """run_scan replaced by a stub that blocks until .release is set; records its arguments."""
    ctl = type("Ctl", (), {})()
    ctl.release = threading.Event()
    ctl.started = threading.Event()
    ctl.calls = []

    def fake_run_scan(jid, *args):
        ctl.calls.append((jid, args))
        ctl.started.set()
        ctl.release.wait(10)
        m.job_update(jid, state="done", file="scan-20260923-101010-1p.pdf",
                     msg="完成：scan-20260923-101010-1p.pdf")
    monkeypatch.setattr(m, "run_scan", fake_run_scan)
    yield ctl
    ctl.release.set()


def _post_scan(m, form=FORM):
    """Posts like the current page: with this build's fingerprint (the station refuses pages from other builds)."""
    return m.app.test_client().post("/scan", data=dict({"build": m.BUILD_ID}, **form))


def test_two_concurrent_posts_one_202_one_409(m, blocking_scan):
    barrier = threading.Barrier(2)
    results = []

    def go():
        barrier.wait()
        r = _post_scan(m)
        results.append((r.status_code, r.get_json()))
    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    codes = sorted(c for c, _ in results)
    assert codes == [202, 409], results
    ok = next(j for c, j in results if c == 202)
    busy = next(j for c, j in results if c == 409)
    assert re.fullmatch(r"[0-9a-f]{16}", ok["job"])
    assert "Another user is scanning" in busy["error"] and "job" not in busy
    assert blocking_scan.started.wait(5)
    assert [c[0] for c in blocking_scan.calls] == [ok["job"]]     # only the winner's job runs
    assert m.scan_lock.locked()
    blocking_scan.release.set()
    assert sup.wait_until(lambda: not m.scan_lock.locked(), 5), "lock not released after the worker ended"
    assert m.job_view(ok["job"], "scan")["state"] == "done"
    r = _post_scan(m)                                              # scanner free again
    assert r.status_code == 202
    assert sup.wait_until(lambda: not m.scan_lock.locked(), 5)


def test_worker_args_are_mapped_from_the_form(m, blocking_scan):
    blocking_scan.release.set()
    form = dict(FORM, source="ADF Duplex", res="300", mode="Gray", size="A4",
                blank="1", ocr="1", enh="1", deskew="0", paper_white="1")
    r = _post_scan(m, form)
    assert r.status_code == 202
    assert sup.wait_until(lambda: blocking_scan.calls and not m.scan_lock.locked(), 5)
    jid, args = blocking_scan.calls[0]
    assert jid == r.get_json()["job"]
    assert args == ("ADF Duplex", "300", "Gray", "A4", True, True, True, False, True)


def test_600_dpi_is_no_longer_offered(m, blocking_scan):
    # owner, 2026-10-19: 300 dpi maximum
    assert m.RES[-1] == "300" and "600" not in m.RES
    r = _post_scan(m, dict(FORM, res="600"))
    assert r.status_code == 400 and not blocking_scan.calls


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_lock_released_even_if_run_scan_raises(m, monkeypatch, threads_before):
    def broken(jid, *a):
        raise RuntimeError("worker crashed")
    monkeypatch.setattr(m, "run_scan", broken)
    assert _post_scan(m).status_code == 202
    assert sup.wait_until(lambda: not m.scan_lock.locked(), 5)
    assert not sup.wait_threads_back(threads_before, 5)     # let the worker's traceback land in THIS test


def test_lock_released_if_request_fails_after_acquiring(m, monkeypatch):
    def boom(kind, **kw):
        raise RuntimeError("job table broken")
    monkeypatch.setattr(m, "new_job", boom)
    r = _post_scan(m)
    assert r.status_code == 500
    assert r.is_json and "Station error" in r.get_json()["error"]
    assert not m.scan_lock.locked()


@pytest.mark.parametrize("field,value", [
    ("source", "Bogus"), ("res", "250"), ("res", "1200"), ("mode", "Sepia"), ("size", "A0"),
    ("source", ""),
])
def test_invalid_params_400_json(m, blocking_scan, field, value):
    r = _post_scan(m, dict(FORM, **{field: value}))
    assert r.status_code == 400 and r.is_json
    assert r.get_json()["error"] == "Invalid scan settings. Refresh the page and retry"
    assert not m.scan_lock.locked() and not blocking_scan.calls


@pytest.mark.parametrize("src", ["ADF", "ADF Duplex"])
def test_adf_a3_accepted(m, blocking_scan, src):
    # 2026-10-19: the feeder takes 297 mm (the old "254 mm" was scanimage showing 297-43); A3 is no longer refused
    r = _post_scan(m, dict(FORM, source=src, size="A3"))
    assert r.status_code == 202 and r.is_json and r.get_json()["job"]


def test_flatbed_a3_allowed(m, blocking_scan):
    blocking_scan.release.set()
    assert _post_scan(m, dict(FORM, size="A3")).status_code == 202


# ---------------------------------------------------------------- /status

def _status(client, **q):
    r = client.get("/status", query_string=q)
    assert r.status_code == 200
    return r.get_json()


def test_status_isolation_and_phases(m, blocking_scan, monkeypatch):
    monkeypatch.setattr(m, "printer_online", lambda: (True, "1.2.3.4"))
    a, b = m.app.test_client(), m.app.test_client()
    ra = a.post("/scan", data=dict(FORM, build=m.BUILD_ID))
    assert ra.status_code == 202
    ja = ra.get_json()["job"]
    assert blocking_scan.started.wait(5)
    jb_print = m.new_job("print", cups="M437-7")
    m.job_update(jb_print, state="running", msg="Sent to printer（B 的作业），正在打印…")

    s = _status(a, scan=ja)                                    # owner
    assert s["scanner"]["busy"] is True and s["scanner"]["mine"] is True
    assert s["scan"]["state"] == "running"
    for key in ("busy", "msg", "pmsg"):                         # old-page compatibility keys
        assert key in s
    assert s["busy"] is True and s["msg"] == s["pmsg"] == REFRESH    # old tabs are told to reload

    for q in ({}, {"scan": "0123456789abcdef"}, {"scan": jb_print}, {"print": ja}):
        s = _status(b, **q)                                    # anybody else
        assert s["scanner"]["busy"] is True and s["scanner"]["mine"] is False
        assert s["scan"] is None and s["print"] is None        # never another client's job / wrong kind
    s = _status(b, print=jb_print)
    assert s["print"]["msg"].startswith("Sent to printer") and s["scan"] is None

    m.scanner.update(phase="scanning", pages=3)                # what run_scan sets while scanning
    s = _status(b)
    assert s["scanner"]["phase"] == "scanning" and s["scanner"]["pages"] == 3
    assert s["scanner"]["msg"] == "Scanner in use (3 pages scanned)" and s["msg"] == REFRESH
    m.scanner.update(phase="processing")                       # scanner idle, but still this job's turn
    s = _status(b)
    assert s["scanner"]["phase"] == "processing"
    assert "Another scan is being processed" in s["scanner"]["msg"] and s["msg"] == REFRESH

    blocking_scan.release.set()
    assert sup.wait_until(lambda: not m.scan_lock.locked(), 5)
    s = _status(a, scan=ja)
    assert s["scan"]["state"] == "done" and s["scan"]["file"] == "scan-20260923-101010-1p.pdf"
    assert s["scanner"]["busy"] is False and s["scanner"]["mine"] is False
    for q in ({}, {"scan": "0123456789abcdef"}, {"print": jb_print}):
        s = _status(b, **q)
        body = json.dumps(s, ensure_ascii=False)
        assert "scan-20260923-101010-1p.pdf" not in body       # A's result never leaks to B
        assert s["scan"] is None and s["scanner"]["msg"] == "Ready"
        assert s["msg"] == s["pmsg"] == REFRESH


def test_job_view_kind_and_unknown_ids(m):
    j = m.new_job("scan")
    assert m.job_view(j, "scan") == {"state": "running", "msg": "", "file": ""}
    assert m.job_view(j, "print") is None
    assert m.job_view("", "scan") is None and m.job_view(None, "scan") is None
    assert m.job_view("nope", "scan") is None


def test_old_jobs_expire(m, monkeypatch):
    old = m.new_job("scan")
    with m._jobs_lock:
        m._jobs[old]["t"] -= m.JOB_KEEP_SEC + 1
    m.new_job("scan")                                          # creating a job prunes expired ones
    assert m.job_view(old, "scan") is None


def test_printer_status_probe_is_cached(m, client, monkeypatch):
    calls = []
    gate = threading.Event()

    def probe():
        calls.append(1)
        gate.wait(5)
        return True, "1.2.3.4"
    monkeypatch.setattr(m, "printer_online", probe)
    s1 = _status(client)
    assert s1["printer"] == {"online": None, "ip": ""}          # first call: unknown, probe started
    s2 = _status(client)                                       # probe still running → no second probe
    gate.set()
    assert sup.wait_until(lambda: not m._pstate["probing"], 5)
    s3 = _status(client)                                       # fresh cache → no probe
    assert len(calls) == 1
    assert s2["printer"]["online"] is None
    assert s3["printer"] == {"online": True, "ip": "1.2.3.4"}


@pytest.mark.parametrize("build", [None, "0123456789ab"])
def test_page_from_another_build_is_told_to_refresh(m, blocking_scan, build):
    """Tabs opened before an update post the old form (darkening and rotation checked by default there) and cannot
    read the new result states: refused with the refresh text before the scanner is touched."""
    form = dict(FORM, enh="1", deskew="1")
    if build is not None:
        form["build"] = build
    r = m.app.test_client().post("/scan", data=form)
    assert r.status_code == 400
    assert r.get_json() == {"error": REFRESH, "stale": True}
    assert not m.scan_lock.locked() and blocking_scan.calls == []
