"""Session setup + per-test sandbox for the scan+print station suite.

* scan_app.py is imported ONCE per session (it computes SCANS/JOBS from ~ at import), with HOME
  pointed at a throw-away temp dir; SCANS/JOBS/job table/scanner state are wiped around every test.
* Every test runs with guard stubs first on PATH (lp, lpstat, cancel, scanimage, soffice, …) that
  exit 99 — this Mac has a live CUPS, a test that forgets its fake must fail, never print.
* socket.create_connection is disabled (no printer probes leave the machine) and PRINTER_IP points
  at TEST-NET-1 (192.0.2.1).
Tests load this repository's app/scan_app.py; no deployment checkout is imported.
"""
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
import threading

import pytest

import station_support as sup

_HERE = os.path.dirname(os.path.abspath(__file__))
_OLD_HOME = os.environ.get("HOME")
_OLD_DEVICE = os.environ.get("STATION_DEV")
os.environ["STATION_DEV"] = "airscan:w0:HP LaserJet MFP M437-M443"
_HOME = tempfile.mkdtemp(prefix="scanstation-home-")
os.environ["HOME"] = _HOME          # BEFORE importing the app: SCANS/JOBS/EVENTS derive from ~


def _find_app():
    for p in (os.path.join(_HERE, "..", "app", "scan_app.py"),):
        if p and os.path.exists(p):
            return os.path.abspath(p)
    return None


APP_PATH = _find_app()
APP_IMPORT_ERROR = None
if APP_PATH and "scan_app" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("scan_app", APP_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["scan_app"] = _mod
    try:
        _spec.loader.exec_module(_mod)
    except Exception as e:          # a broken app must FAIL the suite, not skip it
        del sys.modules["scan_app"]
        APP_IMPORT_ERROR = e


def pytest_unconfigure(config):
    if _OLD_HOME is not None:
        os.environ["HOME"] = _OLD_HOME
    if _OLD_DEVICE is None:
        os.environ.pop("STATION_DEV", None)
    else:
        os.environ["STATION_DEV"] = _OLD_DEVICE
    shutil.rmtree(_HOME, ignore_errors=True)


def _app_or_none():
    return sys.modules.get("scan_app")


@pytest.fixture(scope="session")
def m():
    if APP_IMPORT_ERROR is not None:
        pytest.fail("scan_app.py failed to import: %r" % (APP_IMPORT_ERROR,))
    mod = _app_or_none()
    if mod is None:
        pytest.fail("app/scan_app.py not found")
    assert os.path.realpath(mod.SCANS).startswith(os.path.realpath(_HOME)), \
        "SCANS must live under the temp HOME, got %s" % mod.SCANS
    return mod


@pytest.fixture(scope="session")
def guard_bin(tmp_path_factory):
    return sup.write_guard_bin(str(tmp_path_factory.mktemp("guard-bin")))


def _wipe_dir(d):
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        return
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass


def _reset_app_state(mod):
    _wipe_dir(mod.SCANS)
    _wipe_dir(mod.JOBS)
    with mod._jobs_lock:
        mod._jobs.clear()
    mod.scanner.clear()
    mod.scanner.update(pages=0, job=None)


@pytest.fixture(autouse=True)
def _station_sandbox(monkeypatch, guard_bin):
    monkeypatch.setenv("PATH", guard_bin + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("STATION_DOCX_CONVERTER", raising=False)
    monkeypatch.delenv("STATION_SOFFICE", raising=False)

    def _no_network(*a, **k):
        raise OSError("network disabled in tests (%r)" % (a[:1],))
    monkeypatch.setattr(socket, "create_connection", _no_network)
    mod = _app_or_none()
    if mod is None:
        yield
        return
    monkeypatch.setattr(mod, "STATION_PASSWORD", "")
    monkeypatch.setattr(mod, "PRINTER_IP", "192.0.2.1")
    monkeypatch.setattr(mod, "DEV", "airscan:w0:HP LaserJet MFP M437-M443")
    if isinstance(getattr(mod, "_pstate", None), dict):   # printer-status cache: "never probed"
        monkeypatch.setattr(mod, "_pstate", {"online": None, "ip": "", "t": 0.0, "probing": False})
    _reset_app_state(mod)
    yield
    locked = not sup.wait_until(lambda: not mod.scan_lock.locked(), timeout=5)
    if locked:
        mod.scan_lock.release()
    if isinstance(getattr(mod, "_pstate", None), dict):   # let a /status-triggered probe finish
        sup.wait_until(lambda: not mod._pstate.get("probing"), timeout=5)
    _reset_app_state(mod)
    if locked:
        pytest.fail("scan_lock was still held 5 s after the test ended")


@pytest.fixture()
def client(m):
    return m.app.test_client()


@pytest.fixture()
def bindir(tmp_path, monkeypatch):
    """A per-test bin dir placed on PATH ahead of the guard stubs."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ["PATH"])
    return d


def _img2pdf_wrapper_dir(base):
    real = shutil.which("img2pdf")
    if real:
        return os.path.dirname(real)
    try:
        import img2pdf  # noqa: F401
        import PIL
    except ImportError:
        return None
    paths = [os.path.dirname(img2pdf.__file__), os.path.dirname(os.path.dirname(PIL.__file__))]
    paths += [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    d = os.path.join(base, "img2pdf-bin")
    os.makedirs(d, exist_ok=True)
    sup.write_exe(os.path.join(d, "img2pdf"),
                  "#!/bin/sh\nPYTHONPATH='%s' exec '%s' -m img2pdf \"$@\"\n"
                  % (os.pathsep.join(dict.fromkeys(paths)), sys.executable))
    return d


@pytest.fixture(scope="session")
def _img2pdf_dir(tmp_path_factory):
    return _img2pdf_wrapper_dir(str(tmp_path_factory.mktemp("img2pdf")))


@pytest.fixture()
def img2pdf(_img2pdf_dir, monkeypatch):
    """Puts a working `img2pdf` on PATH (real one, or `python -m img2pdf` from PYTHONPATH)."""
    if not _img2pdf_dir:
        pytest.skip("img2pdf not available (install it or add the pylib dirs to PYTHONPATH)")
    monkeypatch.setenv("PATH", _img2pdf_dir + os.pathsep + os.environ["PATH"])
    return _img2pdf_dir


@pytest.fixture()
def poppler():
    if not (shutil.which("pdfinfo") and shutil.which("pdftoppm")):
        pytest.skip("poppler-utils (pdfinfo/pdftoppm) not installed")


@pytest.fixture(scope="session")
def _fake_bin(tmp_path_factory):
    return sup.install_fakes(str(tmp_path_factory.mktemp("fake-bin")))


@pytest.fixture()
def fake_scanner(_fake_bin, tmp_path, monkeypatch):
    """The fake scanimage (station_support.FAKE_SCANIMAGE) on PATH; per-test state dir via env."""
    monkeypatch.setenv("PATH", _fake_bin + os.pathsep + os.environ["PATH"])
    st = tmp_path / "fake-scan-state"
    st.mkdir()
    return sup.FakeScanner(st, monkeypatch)


@pytest.fixture()
def fake_cups(_fake_bin, tmp_path, monkeypatch):
    """Recording lp / lpstat / cancel on PATH; per-test record dir via env."""
    monkeypatch.setenv("PATH", _fake_bin + os.pathsep + os.environ["PATH"])
    return sup.FakeCups(tmp_path / "cups-rec", monkeypatch)


@pytest.fixture()
def fast_time(m, monkeypatch):
    ft = sup.FastTime()
    monkeypatch.setattr(m, "time", ft)
    return ft


@pytest.fixture()
def job_history(m, monkeypatch):
    """Records every job_update(jid, **kw) call (then applies it for real)."""
    hist = []
    real = m.job_update

    def rec(jid, **kw):
        hist.append((jid, dict(kw)))
        return real(jid, **kw)
    monkeypatch.setattr(m, "job_update", rec)
    return hist


@pytest.fixture()
def threads_before():
    return set(threading.enumerate())
