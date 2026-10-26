"""Request guards and startup wiring.

* POST without Content-Length (chunked) → 411 JSON: Werkzeug doesn't apply MAX_CONTENT_LENGTH to chunked
  bodies, so one request could stream form fields into PID 1's memory.
* `python scan_app.py` starts recover_orphan_workdirs in a background thread (the page must come up
  first), plus cleaner and reap_orphans, then app.run — checked by running the file as __main__ with
  threading.Thread / Flask.run replaced by recorders (nothing is started, no port is opened)."""
import io
import os
import runpy
import threading

import pytest

LENGTH_MSG = "Invalid request format. Refresh the page and try again"
POST_ROUTES = ["/scan", "/upload", "/print", "/cancel", "/delete", "/copy"]


def chunked_post(client, path, body=b"name=x"):
    return client.post(path, input_stream=io.BytesIO(body),
                       headers={"Transfer-Encoding": "chunked",
                                "Content-Type": "application/x-www-form-urlencoded"})


@pytest.mark.parametrize("path", POST_ROUTES)
def test_post_without_content_length_is_411_json(m, client, path):
    r = chunked_post(client, path)
    assert r.status_code == 411 and r.is_json and r.get_json() == {"error": LENGTH_MSG}


def test_chunked_scan_never_takes_the_scanner(m, client, monkeypatch):
    called = []
    monkeypatch.setattr(m, "run_scan", lambda *a: called.append(a))
    assert chunked_post(client, "/scan", b"source=Flatbed&res=300&mode=Color&size=full").status_code == 411
    assert not m.scan_lock.locked() and called == [] and m._jobs == {}


def test_length_guard_runs_before_login(m, client, monkeypatch):
    monkeypatch.setattr(m, "STATION_PASSWORD", "pw")
    assert chunked_post(client, "/delete").status_code == 411


def test_empty_body_with_content_length_is_fine(m, client):
    r = client.post("/delete", data=b"", headers={"Content-Length": "0",
                                                   "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400 and r.get_json()["error"] == "Invalid filename"     # reached the route


def test_gets_are_not_affected(m, client):
    assert client.get("/files").status_code == 200
    assert client.get("/status").status_code == 200


# ---------------------------------------------------------------- startup

class _RecThread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **kw):
        self.target, self.daemon = target, daemon

    def start(self):
        _RecThread.started.append((getattr(self.target, "__name__", repr(self.target)), self.daemon))


def test_main_starts_recovery_in_background(m, tmp_path, monkeypatch):
    import flask
    home = tmp_path / "home"
    work = home / "scans" / ".work-20260923-101112-abcdef"
    work.mkdir(parents=True)
    (work / "p001.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-really")
    order = []
    _RecThread.started = []

    def fake_run(self, *a, **kw):
        order.append(("run", kw.get("host"), kw.get("port")))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(threading, "Thread", _RecThread)
    monkeypatch.setattr(flask.Flask, "run", fake_run)
    runpy.run_path(m.__file__, run_name="__main__")         # the same scan_app.py the suite loaded
    names = [n for n, _ in _RecThread.started]
    assert names == ["recover_orphan_workdirs", "cleaner", "reap_orphans"], names
    assert all(daemon for _, daemon in _RecThread.started)
    assert order == [("run", "0.0.0.0", 8071)]
    assert work.exists(), "recovery must not run inline before the server starts"
    assert os.path.realpath(m.SCANS) != os.path.realpath(str(home / "scans"))   # the suite's app untouched


# ---------------------------------------------------------------- cross-site POSTs (R2 §6 suggestion)

@pytest.mark.parametrize("hdr", [{"Origin": "http://evil.example"}, {"Referer": "http://evil.example/page"},
                                 {"Origin": "null"}])
def test_cross_site_post_refused(m, client, hdr):
    r = client.post("/delete", data={"name": "scan-20260923-101500-1p.pdf"}, headers=hdr)
    assert r.status_code == 403 and "did not come from the station page" in r.get_json()["error"]


@pytest.mark.parametrize("hdr", [{"Origin": "http://localhost"}, {"Referer": "http://localhost/"}, {}])
def test_same_origin_or_headerless_post_allowed(m, client, hdr):
    r = client.post("/delete", data={"name": "scan-20260923-101500-1p.pdf"}, headers=hdr)
    assert r.status_code == 200 and r.get_json()["ok"] is True
