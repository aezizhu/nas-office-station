"""Helpers shared by the scan+print station tests (imported by conftest.py and the test modules).

Nothing here talks to real hardware or the network: fake binaries are written into per-test
temp dirs, and the app's `time` / `subprocess` module globals are swapped for proxies only
inside the test that asked for it (monkeypatch restores them)."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time as _time

# ---------------------------------------------------------------- time / subprocess proxies


class FastTime:
    """Stand-in for the app's `time` module: every sleep is capped, everything else is real.
    Used for run_scan (busy-retry sleeps 6 s, tick thread 2 s, scanimage poll 1 s)."""

    def __init__(self, cap=0.05):
        self._cap = cap

    def __getattr__(self, name):
        return getattr(_time, name)

    def sleep(self, s):
        _time.sleep(min(max(s, 0), self._cap))


class FakeClock:
    """Deterministic clock: sleep() advances time() instantly. For watch_print_job/reap_orphans."""

    def __init__(self, start=None):
        self.t = _time.time() if start is None else start
        self.sleeps = []

    def __getattr__(self, name):
        return getattr(_time, name)

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class SubprocessProxy:
    """Stand-in for the app's `subprocess` module. `hook(cmd, real_run, *a, **kw)` decides what
    `subprocess.run` returns; Popen/TimeoutExpired/CompletedProcess/... are the real ones."""

    def __init__(self, hook):
        self._hook = hook

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, cmd, *a, **kw):
        return self._hook(cmd, subprocess.run, *a, **kw)


def completed(stdout="", stderr="", returncode=0, args=None):
    return subprocess.CompletedProcess(args or [], returncode, stdout, stderr)


# ---------------------------------------------------------------- threads


def wait_threads_back(before, timeout=5.0):
    """Wait until every thread started after `before` (a set from threading.enumerate()) ended.
    Returns the list of stragglers (empty = good)."""
    deadline = _time.time() + timeout
    while True:
        extra = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        if not extra or _time.time() > deadline:
            return extra
        _time.sleep(0.02)


def wait_until(pred, timeout=5.0, step=0.02):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if pred():
            return True
        _time.sleep(step)
    return bool(pred())


def pid_gone(pid, timeout=3.0):
    """True once `pid` no longer exists (zombies reparented to init/launchd get reaped quickly)."""
    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False
    return wait_until(gone, timeout)


# ---------------------------------------------------------------- images / PDFs


def save_png(im, path, dpi=None):
    if dpi:
        im.save(path, dpi=(dpi, dpi))
    else:
        im.save(path)
    return str(path)


def pdf_bytes(sizes_pt, marks=True):
    """A PDF whose page k has size sizes_pt[k] (points) and a black bar at a k-dependent x."""
    from PIL import Image, ImageDraw
    ims = []
    for k, (w, h) in enumerate(sizes_pt):
        im = Image.new("L", (int(round(w)), int(round(h))), 255)
        if marks:
            ImageDraw.Draw(im).rectangle([20 + k * 30, 60, 40 + k * 30, int(h) - 60], fill=0)
        ims.append(im)
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:], resolution=72)
    return buf.getvalue()


A4_PT = (595.28, 841.89)
A3_PT = (841.89, 1190.55)


def pdf_page_sizes(path):
    """[(w_pt, h_pt), ...] via pikepdf, else pdfinfo. None if neither is available."""
    try:
        import pikepdf
        with pikepdf.open(path) as pdf:
            out = []
            for p in pdf.pages:
                box = [float(v) for v in p.mediabox]
                rot = int(p.obj.get("/Rotate", 0)) % 180
                w, h = box[2] - box[0], box[3] - box[1]
                out.append((h, w) if rot else (w, h))
            return out
    except ImportError:
        pass
    if shutil.which("pdfinfo"):
        r = subprocess.run(["pdfinfo", "-f", "1", "-l", "9999", str(path)],
                           capture_output=True, text=True, timeout=30)
        return [(float(a), float(b)) for a, b in
                re.findall(r"Page\s+\d+\s+size:\s+([\d.]+) x ([\d.]+)", r.stdout)]
    return None


def mm_from_pt(v):
    return v / 72.0 * 25.4


# ---------------------------------------------------------------- fake binaries

GUARD_NAMES = ("lp", "lpr", "lpstat", "cancel", "cupsenable", "cupsdisable", "lpadmin", "ipptool",
               "scanimage", "unoconv", "soffice", "ocrmypdf")

GUARD_SCRIPT = """#!/bin/sh
echo "TEST GUARD: the real '$(basename "$0")' must never run inside the test suite" >&2
exit 99
"""


def write_exe(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return str(path)


def write_guard_bin(d):
    """Stubs that shadow real CUPS/scanner/office binaries (this Mac has a live CUPS with
    /usr/bin/lp — a test that forgot its fake must fail, never print on a real printer)."""
    os.makedirs(d, exist_ok=True)
    for n in GUARD_NAMES:
        write_exe(os.path.join(d, n), GUARD_SCRIPT)
    return d


FAKE_SCANIMAGE = r'''#!__PY__
# Fake scanimage for the station tests. Controlled by env:
#   FAKE_SCAN_STATE  dir for calls.jsonl + invocation counter (required)
#   FAKE_SCAN_MODE   comma list, one entry per invocation (last repeats):
#                    ok | busy | nopaper | midfail | stall | endless | openfail_smfp
#   FAKE_SCAN_PAGES  pages in batch mode (default 3), FAKE_SCAN_DELAY seconds between pages
#   FAKE_SCAN_BLANK  comma list of 1-based page numbers that are blank paper
#   FAKE_SCAN_REAL_DPI  DPI actually delivered (pixels + PNG metadata), default = --resolution
#   FAKE_SCAN_TRUNC  in stall mode also leave a half-written extra page
#   FAKE_SCAN_ZERO   in busy mode leave a 0-byte first page (like a failed flatbed scan)
#   FAKE_SCAN_NO_SMFP  `-L` lists no M437 line (forces the WSD path)
#   FAKE_SCAN_PAPER_W / _H  paper size in mm (default A4 portrait 210×297), one value or a comma list
#                    per page. smfp ADF: centred on 150 mm (+ FAKE_SCAN_PAPER_X), where the real feeder carries it, drawn the way
#                    the real M437 delivers it (measured 2026-10-19 from raw -l 0 -x 297 -y 432 frames): ~2 mm of
#                    pure-white device strip at both window sides and at the leading edge, a static grey backing
#                    beside the paper (no change along the feed), paper with fibre texture, pure white after the
#                    paper ends. Paper wider/longer than the window is cut by it.
#   FAKE_SCAN_PAPER_X  lateral offset of the paper centre in mm (ADF), one value or a comma list per page
#   FAKE_SCAN_EDGE_INK comma list per page of edge marks touching the paper edge: R = red 3 mm slice on the right
#                    edge (a cross-page seal), L = same on the left, T = dark mark on the leading edge, - = none
import io, json, os, sys, time
argv = sys.argv[1:]
state = os.environ["FAKE_SCAN_STATE"]
def log(rec):
    with open(os.path.join(state, "calls.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
if "-L" in argv:
    log({"argv": argv, "list": True})
    if os.environ.get("FAKE_SCAN_NO_SMFP"):
        print("device `airscan:w0:Some Other MFP' is a WSD Some Other MFP")
    else:
        print("device `airscan:w0:HP M437' is a WSD HP M437 ip=127.0.0.1")
    sys.exit(0)
def opt(name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
cnt = os.path.join(state, "count")
n_call = (int(open(cnt).read()) if os.path.exists(cnt) else 0) + 1
open(cnt, "w").write(str(n_call))
modes = os.environ.get("FAKE_SCAN_MODE", "ok").split(",")
mode = modes[min(n_call, len(modes)) - 1]
batch, out = opt("--batch"), opt("-o")
tdir = os.path.dirname(batch or out)
log({"argv": argv, "call": n_call, "mode": mode, "pid": os.getpid(), "pgid": os.getpgid(0),
     "existing": sorted(x for x in os.listdir(tdir) if x.endswith(".png"))})
dev = opt("-d") or ""
if mode == "openfail_smfp":
    if dev.startswith("smfp"):
        sys.stderr.write("scanimage: open of device %s failed: Invalid argument\n" % dev)
        sys.exit(1)
    mode = "ok"
if mode == "busy":
    if os.environ.get("FAKE_SCAN_ZERO"):
        open(batch % 1 if batch else out, "wb").close()
    sys.stderr.write("scanimage: sane_start: Device busy\n")
    sys.exit(1)
if mode == "nopaper":
    sys.stderr.write("scanimage: sane_start: Document feeder out of documents\n")
    sys.exit(7)
from PIL import Image, ImageDraw
res = float(opt("--resolution") or 200)
real = float(os.environ.get("FAKE_SCAN_REAL_DPI") or res)
xmm, ymm = float(opt("-x") or 210), float(opt("-y") or 297)
src = opt("--doc-source") or opt("--source") or "Flatbed"
adf = src.startswith("ADF")
gray = (opt("--mode") or "").startswith("Gray")
blank = {int(x) for x in os.environ.get("FAKE_SCAN_BLANK", "").split(",") if x}
npages = int(os.environ.get("FAKE_SCAN_PAGES") or 3)
delay = float(os.environ.get("FAKE_SCAN_DELAY") or 0)
def per_page(k, name, default):                       # "210" or "210,250,210" (last repeats)
    vals = [v for v in (os.environ.get(name) or "").split(",") if v]
    return vals[min(k, len(vals)) - 1] if vals else default
smfp_adf = adf and dev.startswith("smfp")
def page_png(k):
    W, H = round(xmm / 25.4 * real), round(ymm / 25.4 * real)
    mm = real / 25.4
    paper_w = float(per_page(k, "FAKE_SCAN_PAPER_W", 210.0))
    paper_h = float(per_page(k, "FAKE_SCAN_PAPER_H", 297.0 if smfp_adf else 1e9))
    if smfp_adf:
        import numpy as np
        lmm = float(opt("-l") or 0)
        off = float(per_page(k, "FAKE_SCAN_PAPER_X", 0.0))
        a = np.empty((H, W), np.int16)
        a[:] = 252 + (np.arange(W) // 7) % 2                     # static backing: same in every row
        px0 = round((150.0 + off - paper_w / 2 - lmm) * mm)
        px1 = round((150.0 + off + paper_w / 2 - lmm) * mm)
        ph = min(H, round(paper_h * mm))
        cx0, cx1 = max(0, px0), min(W, px1)
        rng = np.random.default_rng(k)
        tile = rng.choice(np.array([-1, 0, 0, 0, 1], np.int16), size=(64, 64))
        if cx1 > cx0 and ph > 0:
            reps = (ph // 64 + 1, (cx1 - cx0) // 64 + 1)
            a[:ph, cx0:cx1] = 251 + np.tile(tile, reps)[:ph, :cx1 - cx0]
            if k not in blank:
                for y in range(round(20 * mm), ph - round(20 * mm), max(4, round(8 * mm))):
                    xa, xb = max(cx0, px0 + (px1 - px0) // 8), min(cx1, px0 + (px1 - px0) * 7 // 8)
                    a[y:y + max(2, round(2 * mm)), xa:xb] = 100
        a[ph:, :] = 255                                          # device white after the paper ends
        pad, fade = round(1.5 * mm), max(1, round(0.5 * mm))
        a[:, :pad] = 255                                         # device white at both window sides...
        a[:, W - pad:] = 255
        a[:, pad:pad + fade] = np.maximum(a[:, pad:pad + fade], 254)          # ...fading into the image
        a[:, W - pad - fade:W - pad] = np.maximum(a[:, W - pad - fade:W - pad], 254)
        rgb = np.repeat(a.clip(0, 255).astype(np.uint8)[:, :, None], 3, axis=2)
        marks = str(per_page(k, "FAKE_SCAN_EDGE_INK", "-"))
        y0, y1 = round(100 * mm), round(140 * mm)
        w3 = max(2, round(3 * mm))
        if "R" in marks and px1 <= W:
            rgb[y0:y1, max(0, px1 - w3):px1] = (205, 45, 45)
        if "L" in marks and px0 >= 0:
            rgb[y0:y1, px0:px0 + w3] = (205, 45, 45)
        if "T" in marks:
            rgb[:round(5 * mm), max(cx0, (cx0 + cx1) // 2 - w3):(cx0 + cx1) // 2 + w3] = (60, 60, 60)
        rgb[:pad, :] = 255                                       # device white at the leading edge
        im = Image.fromarray(rgb if not gray else rgb[:, :, 0], "RGB" if not gray else "L")
    else:
        im = Image.new("L" if gray else "RGB", (W, H), 255 if gray else (255, 255, 255))
        d = ImageDraw.Draw(im)
        if adf:                                                  # WSD fallback: paper centred, white outside
            pw = min(W, round(paper_w * mm))
            x0 = (W - pw) // 2
            ph = min(H, round(paper_h * mm))
            y0 = 0
        else:                                                    # flatbed: paper in the corner, lid beyond it
            full = (xmm >= 296 and ymm >= 419) or not dev.startswith("smfp")   # smfp windows carry 10 mm slack
            pw = W if full else min(W, round((xmm - 10) * mm))
            ph = H if full else min(H, round((ymm - 10) * mm))
            x0 = y0 = 0
            if not full:
                d.rectangle([0, 0, W - 1, H - 1], fill=250 if gray else (250, 250, 250))
        d.rectangle([x0, y0, x0 + pw - 1, y0 + ph - 1], fill=244 if gray else (246, 242, 246))
        if k not in blank:
            for y in range(round(20 * mm), ph - round(20 * mm), max(4, round(8 * mm))):
                d.rectangle([x0 + pw // 8, y, x0 + pw * 7 // 8, y + max(2, round(2 * mm))],
                            fill=100 if gray else (100, 100, 100))
        if adf:
            im = im.crop((0, 0, W, ph))                          # WSD ADF: the frame ends with the paper
    buf = io.BytesIO()
    im.save(buf, "PNG", dpi=(real, real))
    return buf.getvalue()
def path(k):
    return batch % k if batch else out
def write(k, data=None):
    data = data or page_png(k)
    with open(path(k), "wb") as f:
        f.write(data)
if mode == "endless":
    k = 0
    while True:
        k += 1
        write(k)
        time.sleep(delay or 0.2)
count = npages if batch else 1
for k in range(1, count + 1):
    if delay and k > 1:
        time.sleep(delay)
    write(k)
if mode == "midfail":
    sys.stderr.write("scanimage: sane_read: Error during device I/O\n")
    sys.exit(1)
if mode == "stall":
    if os.environ.get("FAKE_SCAN_TRUNC") and batch:
        data = page_png(count + 1)
        write(count + 1, data[: len(data) // 2])
    time.sleep(60)
    sys.exit(0)
if batch:                                   # real scanimage: rc=7 when the feeder runs empty
    sys.stderr.write("scanimage: sane_start: Document feeder out of documents\n")
    sys.exit(7)
sys.exit(0)
'''


class FakeScanner:
    """Controller for the fake scanimage installed on PATH by the `fake_scanner` fixture."""

    ENV = ("FAKE_SCAN_MODE", "FAKE_SCAN_PAGES", "FAKE_SCAN_DELAY", "FAKE_SCAN_BLANK",
           "FAKE_SCAN_REAL_DPI", "FAKE_SCAN_TRUNC", "FAKE_SCAN_ZERO", "FAKE_SCAN_NO_SMFP",
           "FAKE_SCAN_PAPER_W", "FAKE_SCAN_PAPER_H", "FAKE_SCAN_PAPER_X", "FAKE_SCAN_EDGE_INK")

    def __init__(self, state, monkeypatch):
        self.state = str(state)
        self._mp = monkeypatch
        monkeypatch.setenv("FAKE_SCAN_STATE", self.state)
        for k in self.ENV:
            monkeypatch.delenv(k, raising=False)

    def set(self, **kw):
        for k, v in kw.items():
            name = "FAKE_SCAN_" + k.upper()
            assert name in self.ENV, name
            if v is None or v is False:
                self._mp.delenv(name, raising=False)
            else:
                self._mp.setenv(name, "1" if v is True else str(v))
        return self

    def calls(self):
        p = os.path.join(self.state, "calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def scan_calls(self):
        return [c for c in self.calls() if not c.get("list")]


def argval(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


# CUPS fakes are plain /bin/sh (a Python start-up per `lp`/`lpstat` call made the print tests the
# slowest part of the suite). Each call is recorded as one argument per line + a \036 record separator.
FAKE_LP = r"""#!/bin/sh
{ for a in "$@"; do printf '%s\n' "$a"; done; printf '\036\n'; } >> "$FAKE_CUPS_DIR/lp.args"
if [ -n "$FAKE_LP_FAIL" ]; then echo "lp: Error - The printer or class does not exist." >&2; exit 1; fi
if [ -z "$FAKE_LP_NOID" ]; then echo "request id is ${FAKE_LP_ID:-M437-999} (1 file(s))"; fi
exit 0
"""

FAKE_LPSTAT = r"""#!/bin/sh
echo "$*" >> "$FAKE_CUPS_DIR/lpstat.log"
case " $* " in
  *" -v "*) echo "device for M437: ${FAKE_QUEUE_URI:-socket://1.2.3.4:9100}" ;;
  *" -p "*) printf '%s\n' "${FAKE_LPSTAT_P:-printer M437 is idle.  enabled since Tue Sep 22 02:59:49 2026}" ;;
esac
exit 0
"""

FAKE_CANCEL = r"""#!/bin/sh
{ for a in "$@"; do printf '%s\n' "$a"; done; printf '\036\n'; } >> "$FAKE_CUPS_DIR/cancel.args"
if [ -n "$FAKE_CANCEL_FAIL" ]; then
  echo "cancel: cancel-job failed: Job #999 is already completed - can't cancel." >&2; exit 1
fi
exit 0
"""


def install_fakes(d):
    """Write the fake scanimage/lp/lpstat/cancel ONCE per session and exec each once: the first exec of a
    brand-new file on macOS can take ~0.5 s (security scan), which per-test copies paid every time."""
    os.makedirs(d, exist_ok=True)
    write_exe(os.path.join(d, "scanimage"), FAKE_SCANIMAGE.replace("__PY__", sys.executable))
    for name, src in (("lp", FAKE_LP), ("lpstat", FAKE_LPSTAT), ("cancel", FAKE_CANCEL)):
        write_exe(os.path.join(d, name), src)
    warm = os.path.join(d, ".warm")
    os.makedirs(warm, exist_ok=True)
    env = dict(os.environ, FAKE_CUPS_DIR=warm, FAKE_SCAN_STATE=warm)
    for cmd in (["scanimage", "-L"], ["lp", "x"], ["lpstat", "-v", "M437"], ["cancel", "x"]):
        subprocess.run([os.path.join(d, cmd[0])] + cmd[1:], env=env, capture_output=True, timeout=60)
    return d


class FakeCups:
    """Controller for the lp / lpstat / cancel recorders (see `fake_cups` fixture)."""

    def __init__(self, recdir, monkeypatch):
        self.rec = str(recdir)
        os.makedirs(self.rec, exist_ok=True)
        monkeypatch.setenv("FAKE_CUPS_DIR", self.rec)
        for k in ("FAKE_LP_FAIL", "FAKE_LP_NOID", "FAKE_LP_ID", "FAKE_QUEUE_URI",
                  "FAKE_LPSTAT_P", "FAKE_CANCEL_FAIL"):
            monkeypatch.delenv(k, raising=False)
        self._mp = monkeypatch

    def env(self, **kw):
        for k, v in kw.items():
            if v is None:
                self._mp.delenv(k, raising=False)
            else:
                self._mp.setenv(k, str(v))

    def _records(self, name):
        p = os.path.join(self.rec, name)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            raw = f.read()
        return [rec.strip("\n").split("\n") if rec.strip("\n") else [] for rec in raw.split("\036\n")[:-1]]

    def lp_calls(self):
        return self._records("lp.args")

    def cancel_calls(self):
        return self._records("cancel.args")

    def lpstat_calls(self):
        p = os.path.join(self.rec, "lpstat.log")
        return open(p).read().splitlines() if os.path.exists(p) else []


def lp_options(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-o" and i + 1 < len(argv)]
