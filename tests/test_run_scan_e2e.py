"""run_scan end-to-end with a FAKE scanimage on PATH (see station_support.FAKE_SCANIMAGE) and the
real img2pdf. Every path must end with: the right job state + plain-language message, the PDF only
under its final scan-*.pdf name (never visible while being written), no .part-*/.work-* left, the
progress thread stopped, the scanner record reset."""
import glob
import os
import re
import subprocess
import threading

import pytest

import station_support as sup

BUSY = "The scanner is busy. Wait 10 seconds and retry. If it remains busy, restart the printer"
IO_ERR = "Scanner communication stopped. Check printer power and its network cable, then retry"
HIDDEN = "Processing failed. Retry, or contact the administrator with the error time if it persists"


@pytest.fixture()
def env(m, fake_scanner, img2pdf, fast_time, job_history, threads_before, monkeypatch):
    """Everything a run_scan test needs. .visible records what a user could see at the moment
    img2pdf finished writing (the PDF must not be listed yet)."""
    e = type("Env", (), {})()
    e.scanner, e.history, e.before = fake_scanner, job_history, threads_before
    e.visible = []
    e.img2pdf_hook = None

    def hook(cmd, real_run, *a, **kw):
        if cmd and cmd[0] == "img2pdf" and e.img2pdf_hook:
            return e.img2pdf_hook(cmd, real_run, *a, **kw)
        r = real_run(cmd, *a, **kw)
        if cmd and cmd[0] == "img2pdf":
            e.visible.append(snapshot(m))
        return r
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    return e


def snapshot(m):
    return {"final": sorted(os.path.basename(p) for p in glob.glob(os.path.join(m.SCANS, "scan-*.pdf"))),
            "rows": m._file_rows(),
            "parts": sorted(os.path.basename(p) for p in glob.glob(os.path.join(m.SCANS, ".part-*")))}


def run(m, **kw):
    args = dict(source="Flatbed", res="100", mode="Color", size="A5", drop_blank=False,
                do_ocr=False, enhance=True, deskew=False)
    args.update(kw)
    jid = m.new_job("scan")
    m.run_scan(jid, **args)
    return jid, m.job_view(jid, "scan")


def events(m):
    if not os.path.exists(m.EVENTS):
        return []
    with open(m.EVENTS, encoding="utf-8") as f:
        return [line.split("\t") for line in f.read().splitlines()]


def event_kinds(m):
    return [e[1] for e in events(m)]


def finished_cleanly(m, env, view, jid):
    assert glob.glob(os.path.join(m.SCANS, ".work-*")) == [], "work dir left behind"
    assert glob.glob(os.path.join(m.SCANS, ".part-*")) == [], ".part file left behind"
    entries = set(os.listdir(m.SCANS)) - {"events.log"}
    # the published PDF, its check record, its e-mail copy and (only when processing ran) the kept originals
    f = view["file"]
    allowed = {f, f + ".quality.json", ".small-" + f, ".raw-" + f[:-4]} if f else set()
    assert f in entries if f else True, entries
    assert entries <= allowed and (not f or {f, f + ".quality.json"} <= entries), entries
    assert m.scanner.get("job") == jid and m.scanner.get("pages") == 0     # job id kept for the owner
    assert m.scanner.get("workdir") is None and m.scanner.get("phase") == ""
    stragglers = sup.wait_threads_back(env.before, timeout=3)
    assert not stragglers, "threads still alive: %s" % stragglers
    assert threading.active_count() <= len(env.before)
    for snap in env.visible:                                # at img2pdf time nobody could see the PDF
        assert snap["final"] == [] and snap["rows"] == [], snap
        assert 1 <= len(snap["parts"]) <= 2, snap               # the PDF and (then) its e-mail copy, both hidden
        assert all(x.startswith((".part-scan-", ".part-small-scan-")) for x in snap["parts"]), snap


def pdf_sizes_mm(m, name):
    sizes = sup.pdf_page_sizes(os.path.join(m.SCANS, name))
    if sizes is None:
        pytest.skip("neither pikepdf nor pdfinfo available to inspect the PDF")
    return [(sup.mm_from_pt(w), sup.mm_from_pt(h)) for w, h in sizes]


def assert_pages(m, name, n, w_mm, h_mm, tol=1.5):
    sizes = pdf_sizes_mm(m, name)
    assert len(sizes) == n, sizes
    for w, h in sizes:
        assert abs(w - w_mm) <= tol and abs(h - h_mm) <= tol, (name, sizes)


def msgs(env, jid):
    return [kw["msg"] for j, kw in env.history if j == jid and "msg" in kw]


def size_label(m, name):
    """The ' · 51 KB' / ' · 2.3 MB' suffix run_scan appends, computed from the published file."""
    mb = os.path.getsize(os.path.join(m.SCANS, name)) / 1024 / 1024
    return "%.1f MB" % mb if mb >= 1 else "%d KB" % max(1, mb * 1024)


def pdf_image_filters(m, name):
    pikepdf = pytest.importorskip("pikepdf")
    out = []
    with pikepdf.open(os.path.join(m.SCANS, name)) as pdf:
        for page in pdf.pages:
            imgs = page.get_images() if hasattr(page, "get_images") else page.images
            for _, img in imgs.items():
                out.append(str(img.get("/Filter")))
    return out


# ---------------------------------------------------------------- happy paths

def test_flatbed_a5_done(m, env, monkeypatch):
    phases = {}
    real_rs, real_pp = m._run_scanimage, m.postprocess_page

    def rs(cmd, workdir):
        phases.setdefault("scan", m.scanner.get("phase"))
        return real_rs(cmd, workdir)

    def pp(*a):
        phases.setdefault("post", m.scanner.get("phase"))
        return real_pp(*a)
    monkeypatch.setattr(m, "_run_scanimage", rs)
    monkeypatch.setattr(m, "postprocess_page", pp)

    jid, v = run(m)
    assert v["state"] == "done", v
    assert re.fullmatch(r"scan-\d{8}-\d{6}-1p\.pdf", v["file"])
    assert v["msg"] == "Complete: %s (1 pages / Flatbed / A5 · %s; text enhanced; deleted automatically after 15 minutes)" % (
        v["file"], size_label(m, v["file"]))
    assert_pages(m, v["file"], 1, 148, 210)
    assert pdf_image_filters(m, v["file"]) == ["/FlateDecode"]    # the scanner's PNG, lossless
    # (this flat synthetic page compresses better as PNG, so no e-mail copy is offered for it)
    assert not os.path.exists(os.path.join(m.SCANS, ".small-" + v["file"]))
    assert phases == {"scan": "scanning", "post": "processing"}
    calls = env.scanner.scan_calls()
    assert len(calls) == 1
    a = calls[0]["argv"]
    assert sup.argval(a, "-d") == "smfp:net;127.0.0.1" and sup.argval(a, "--doc-source") == "Flatbed"
    assert sup.argval(a, "--resolution") == "100" and sup.argval(a, "--mode") == "Color - 16 Million Colors"
    assert [sup.argval(a, k) for k in ("-l", "-t", "-x", "-y")] == ["0", "0", "148", "210"]   # HP's own A5 window
    assert sup.argval(a, "-o").endswith("/p001.png") and "/.work-" in sup.argval(a, "-o")
    seq = msgs(env, jid)
    for want in ("Preparing the scanner...", "Scanning...", "Processing pages... 1/1", "Generating PDF..."):
        assert want in seq, seq
    assert event_kinds(m)[0] == "scan-start" and event_kinds(m)[-1] == "scan-done"
    assert len(env.visible) == 2                     # img2pdf ran twice: the lossless PDF, then its e-mail copy
    finished_cleanly(m, env, v, jid)


def test_adf_full_three_pages_with_progress(m, env):
    env.scanner.set(pages=3, delay=0.4)
    jid, v = run(m, source="ADF", size="full")
    assert v["state"] == "done", v                    # rc=7 at the end of the batch is normal
    assert v["file"].endswith("-3p.pdf")
    assert "3 pages / Feeder / Full area (page 297 x 432 mm, both side edges complete) · %s;" % size_label(m, v["file"]) in v["msg"]
    assert "Review required" not in v["msg"]
    assert_pages(m, v["file"], 3, 297, 432)            # the whole feed path, exactly as the scanner sent it
    a = env.scanner.scan_calls()[0]["argv"]
    assert sup.argval(a, "--doc-source") == "ADF Simplex"
    assert [sup.argval(a, k) for k in ("-l", "-t", "-x", "-y")] == ["0", "0", "297", "432"]
    assert sup.argval(a, "--batch").endswith("/p%03d.png")
    assert any(re.fullmatch(r"Scanning\.\.\. [12] pages scanned", s) for s in msgs(env, jid)), msgs(env, jid)
    finished_cleanly(m, env, v, jid)


def test_adf_duplex_gray_drops_blank_pages(m, env):
    env.scanner.set(pages=4, blank="2,4")
    jid, v = run(m, source="ADF Duplex", mode="Gray", size="A4", drop_blank=True, enhance=False)
    assert v["state"] == "done", v
    assert v["file"].endswith("-2p.pdf") and "removed 2 blank pages" in v["msg"]
    assert_pages(m, v["file"], 2, 220, 300)            # calibrated A4 window, no crop
    assert pdf_image_filters(m, v["file"]) == ["/FlateDecode"] * 2                # lossless greyscale
    a = env.scanner.scan_calls()[0]["argv"]
    assert sup.argval(a, "--doc-source") == "ADF Duplex - Long-Edge Binding"
    assert sup.argval(a, "--mode") == "Grayscale - 256 Levels"
    finished_cleanly(m, env, v, jid)


def test_no_smfp_line_uses_wsd(m, env):
    env.scanner.set(no_smfp=True)
    jid, v = run(m, res="150", enhance=False)
    assert v["state"] == "done", v
    assert "dpi" not in v["msg"]                       # 150 ≤ 200: no downgrade note
    a = env.scanner.scan_calls()[0]["argv"]
    assert sup.argval(a, "-d") == m.DEV and sup.argval(a, "--source") == "Flatbed"
    assert sup.argval(a, "--resolution") == "150"
    assert "scan-smfp-unavailable" in event_kinds(m)
    finished_cleanly(m, env, v, jid)


def test_adf_sideways_a4_is_captured_whole(m, env):
    """2026-10-19 incident: A4 fed long edge first lost ~79 mm in the old 222 mm window. "Full area" scans the whole
    297 mm feed path: the sheet is on the page complete, nothing is cropped."""
    env.scanner.set(pages=2, paper_w=297, paper_h=210)
    jid, v = run(m, source="ADF", size="full", enhance=False)
    assert v["state"] == "done", v
    assert_pages(m, v["file"], 2, 297, 432)
    assert "Pages 1-2: The sheet fills the feeder width (sideways A4 or A3). A few millimeters on each long edge are unseen; feed A4 short edge first to include both sides" in v["msg"], v["msg"]
    assert "Review required" not in v["msg"]
    finished_cleanly(m, env, v, jid)


def _red_columns(m, name, dpi):
    pikepdf = pytest.importorskip("pikepdf")
    import numpy as np
    with pikepdf.open(os.path.join(m.SCANS, name)) as pdf:
        page = pdf.pages[0]
        images = page.get_images(recursive=False) if hasattr(page, "get_images") else page.images
        img = pikepdf.PdfImage(next(iter(images.values()))).as_pil_image().convert("RGB")
    a = np.asarray(img, dtype=np.int16)
    return np.flatnonzero(((a[..., 0] - np.maximum(a[..., 1], a[..., 2])) > 60).any(axis=0)), img.width


@pytest.mark.parametrize("size,offset", [("full", -20), ("full", 0), ("full", 20),
                                         ("A4", -2), ("A4", 0), ("A4", 2)])
def test_seal_on_the_paper_edge_is_kept_whole(m, env, size, offset):
    """Cross-page seal (骑缝章) slice, 3 mm wide, on the paper's right edge. The calibrated A4 window keeps it whole
    for the measured feed drift (+-2 mm); Full area keeps it wherever the sheet sits. Nothing is cropped either way."""
    env.scanner.set(pages=1, paper_x=offset, edge_ink="R")
    jid, v = run(m, source="ADF", size=size, enhance=False, res="150")
    assert v["state"] == "done", v
    cols, width = _red_columns(m, v["file"], 150)
    k = 150 / 25.4
    assert len(cols) and (cols[-1] - cols[0] + 1) / k >= 3.0 - 0.3, cols[:1]
    finished_cleanly(m, env, v, jid)


def test_sheet_drifting_into_the_blind_strip_is_flagged(m, env):
    """The scanner whites out ~2 mm at each window side. A sheet 5 mm off centre in the A4 window puts its seal
    slice into that strip: the scan must say so (needs a check), never pass as a clean done."""
    env.scanner.set(pages=1, paper_x=5, edge_ink="R")
    jid, v = run(m, source="ADF", size="A4", enhance=False, res="150")
    assert v["state"] == "review", v
    assert "The right edge contains content or enters an unseen scanner strip" in v["msg"], v["msg"]
    finished_cleanly(m, env, v, jid)


@pytest.mark.parametrize("drop_blank,want", [(False, "Pages 3"), (True, "Pages 2")])
def test_edge_ink_review_names_final_pdf_pages(m, env, drop_blank, want):
    """Page 3 has ink touching the leading edge (the device whites out ~2 mm there): that page needs a
    check. Page 1 is blank; with blank pages dropped the warning must name the page as numbered in the PDF."""
    env.scanner.set(pages=4, blank="1", edge_ink="-,-,T,-")
    jid, v = run(m, source="ADF", size="full", enhance=False, drop_blank=drop_blank)
    assert v["state"] == "review", v
    assert "%s: The leading edge contains content or enters an unseen scanner strip" % want in v["msg"], v["msg"]
    quality = m._file_rows()[0]["quality"]
    assert quality["state"] == "review" and any(i.startswith(want) for i in quality["issues"]), quality
    finished_cleanly(m, env, v, jid)


def test_issue_on_a_dropped_blank_page_is_not_reported(m, env):
    env.scanner.set(pages=3, blank="2", paper_h="297,500,297")      # page 2: blank, longer than the window
    jid, v = run(m, source="ADF", size="full", enhance=False, drop_blank=True)
    assert v["state"] == "done" and "removed 1 blank pages" in v["msg"], v
    assert "trailing" not in v["msg"]
    finished_cleanly(m, env, v, jid)


def test_partial_result_keeps_label_and_warnings(m, env):
    env.scanner.set(mode="midfail", pages=2, edge_ink="-,T")
    jid, v = run(m, source="ADF", size="full", enhance=False)
    assert v["state"] == "partial", v
    # page 2 has ink at the leading edge, so the label makes no "edges complete" claim
    assert "(Feeder / Full area (page 297 x 432 mm) · " in v["msg"] and "edges complete" not in v["msg"], v["msg"]
    assert ("but scanning stopped: %s. Check whether pages remain unscanned. Also: Pages 2: The leading edge contains content or enters an unseen scanner strip"
            % IO_ERR) in v["msg"], v["msg"]
    finished_cleanly(m, env, v, jid)


def test_owner_is_not_told_occupied_before_the_lock_is_released(m, env, client, monkeypatch):
    seen = {}
    real = m.run_scan

    def run_then_look(jid, *args):
        real(jid, *args)                                  # finished, lock still held by _scan_worker
        owner = client.get("/status", query_string={"scan": jid}).get_json()
        other = client.get("/status").get_json()
        seen.update(owner=owner, other=other)
    monkeypatch.setattr(m, "run_scan", run_then_look)
    monkeypatch.setattr(m, "printer_online", lambda: (False, "192.0.2.1"))
    monkeypatch.setattr(m, "ADF_WAIT_SEC", 0)
    env.scanner.set(mode="nopaper")
    r = client.post("/scan", data={"source": "ADF", "res": "100", "mode": "Color", "size": "A4", "build": m.BUILD_ID})
    jid = r.get_json()["job"]
    assert sup.wait_until(lambda: "owner" in seen and not m.scan_lock.locked(), 10)
    assert seen["owner"]["scanner"]["busy"] is True and seen["owner"]["scanner"]["mine"] is True
    assert seen["owner"]["scan"]["state"] == "error" == m.job_view(jid, "scan")["state"]
    assert seen["other"]["scanner"]["mine"] is False and seen["other"]["scan"] is None


@pytest.mark.parametrize("scanner_mode", [dict(no_smfp=True), dict(mode="openfail_smfp")],
                         ids=["no-smfp-device", "smfp-open-failed"])
def test_wsd_fallback_adf_a4_still_done(m, env, scanner_mode):
    # WSD fallback keeps the old window = paper width path (normalize_pages); no false warnings there
    env.scanner.set(pages=2, **scanner_mode)
    jid, v = run(m, source="ADF", size="A4", enhance=False)
    assert v["state"] == "done", v
    assert "Review required" not in v["msg"] and "scan area" not in v["msg"], v["msg"]
    assert sup.argval(env.scanner.scan_calls()[-1]["argv"], "-d") == m.DEV
    finished_cleanly(m, env, v, jid)


@pytest.mark.parametrize("size,win", [
    ("full", ["0", "0", "297", "432"]),
    ("A4", ["40", "0", "220", "300"]),
    ("A5", ["71", "0", "158", "213"]),
    ("B5", ["57", "0", "186", "253"]),
    ("Letter", ["37", "0", "226", "282"]),
    ("Legal", ["37", "0", "226", "359"]),
    ("A3", ["0", "0", "297", "423"]),
])
def test_adf_window_per_size(m, env, size, win):
    """Measured 2026-10-19: this feeder carries paper centred on ~150 mm (HP assumes 148) and the scanner whites out
    ~2 mm at each window side, so a chosen size gets a window centred on 150 mm with 5 mm extra per side and 3 mm
    extra length. The page is exactly what the scanner returned for that window."""
    w, h = {"full": (210, 297)}.get(size) or next((sw, sh) for n, sw, sh in m.STD_SIZES if n == size)
    env.scanner.set(pages=1, paper_w=w, paper_h=h)                  # a sheet of the chosen size
    jid, v = run(m, source="ADF", size=size, enhance=False)
    a = env.scanner.scan_calls()[0]["argv"]
    assert [sup.argval(a, k) for k in ("-l", "-t", "-x", "-y")] == win
    assert v["state"] == "done", v
    assert_pages(m, v["file"], 1, float(win[2]), float(win[3]))


# ---------------------------------------------------------------- scanner failures

def test_busy_three_times_is_an_error(m, env):
    env.scanner.set(mode="busy", zero=True)
    jid, v = run(m)
    assert v == {"state": "error", "msg": "Error: " + BUSY, "file": ""}
    calls = env.scanner.scan_calls()
    assert len(calls) == 3
    assert [c["existing"] for c in calls[1:]] == [[], []], "0-byte page from the failed try not removed"
    seq = msgs(env, jid)
    assert "Scanner busy; retrying in 6 seconds (1/3)..." in seq and "Scanner busy; retrying in 6 seconds (2/3)..." in seq
    assert "scan-fail" in event_kinds(m)
    finished_cleanly(m, env, v, jid)


def test_busy_then_ok_retries(m, env):
    env.scanner.set(mode="busy,ok", zero=True)
    jid, v = run(m, enhance=False)
    assert v["state"] == "done" and v["file"].endswith("-1p.pdf"), v
    calls = env.scanner.scan_calls()
    assert len(calls) == 2 and calls[1]["existing"] == []
    assert_pages(m, v["file"], 1, 148, 210)
    finished_cleanly(m, env, v, jid)


NO_PAPER = "Error: The feeder did not detect paper. Insert the stack face up, short edge first, until the printer detects it, then select Retry. For paper on the glass, select Use flatbed"


def test_adf_no_paper(m, env, monkeypatch):
    monkeypatch.setattr(m, "ADF_WAIT_SEC", 0)
    env.scanner.set(mode="nopaper")
    jid, v = run(m, source="ADF", size="A4")
    assert v == {"state": "error", "msg": NO_PAPER, "file": ""}
    assert len(env.scanner.scan_calls()) == 1
    finished_cleanly(m, env, v, jid)


def test_adf_waits_for_paper_then_scans(m, env, monkeypatch):
    """25 of 285 feeder scans began with an empty feeder and were redone by hand seconds later. Now the
    station keeps asking (the device answers 'no documents' in ~0.2 s) and starts as soon as paper is in."""
    monkeypatch.setattr(m, "ADF_WAIT_SEC", 30)
    monkeypatch.setattr(m, "ADF_WAIT_POLL", 0.01)
    env.scanner.set(mode="nopaper,nopaper,ok", pages=2)
    jid, v = run(m, source="ADF", size="full", enhance=False)
    assert v["state"] == "done" and v["file"].endswith("-2p.pdf"), v
    assert len(env.scanner.scan_calls()) == 3
    assert any(s.startswith("Waiting for feeder paper") for s in msgs(env, jid)), msgs(env, jid)
    assert "scan-wait-paper" in event_kinds(m)
    finished_cleanly(m, env, v, jid)


def test_adf_wait_for_paper_gives_up_with_both_ways_out(m, env, monkeypatch):
    monkeypatch.setattr(m, "ADF_WAIT_SEC", 0.4)
    monkeypatch.setattr(m, "ADF_WAIT_POLL", 0.05)
    env.scanner.set(mode="nopaper")
    jid, v = run(m, source="ADF", size="A4")
    assert v == {"state": "error", "msg": NO_PAPER, "file": ""}      # Retry + Use flatbed (UI)
    assert len(env.scanner.scan_calls()) >= 2                          # it kept asking before giving up
    finished_cleanly(m, env, v, jid)


def test_mid_batch_io_error_is_partial(m, env):
    env.scanner.set(mode="midfail", pages=2)
    jid, v = run(m, source="ADF", size="A4", enhance=False)
    assert v["state"] == "partial", v
    assert v["file"].endswith("-2p.pdf")
    assert v["msg"] == ("Partial result: scanned 2 pages into %s (Feeder / A4 (page 220 x 300 mm, both side edges complete) · %s), "
                        "but scanning stopped: %s. Check whether pages remain unscanned" % (v["file"], size_label(m, v["file"]), IO_ERR))
    assert_pages(m, v["file"], 2, 220, 300)            # calibrated A4 window, plain output
    assert "scan-partial" in event_kinds(m)
    finished_cleanly(m, env, v, jid)


def test_stall_salvages_readable_pages(m, env, monkeypatch):
    monkeypatch.setattr(m, "SCAN_STALL_SEC", 1.5)
    env.scanner.set(mode="stall", pages=2, trunc=True)
    jid, v = run(m, source="ADF", size="A4")
    assert v["state"] == "partial", v
    assert v["file"].endswith("-2p.pdf")                # the half-written 3rd page is dropped
    assert v["msg"].startswith("Error: No new page arrived for %d seconds, so scanning stopped" % m.SCAN_STALL_SEC)
    assert v["msg"].endswith("; saved 2 completed pages as %s" % v["file"])
    assert_pages(m, v["file"], 2, 220, 300)            # calibrated A4 window, plain output
    pid = env.scanner.scan_calls()[0]["pid"]
    assert sup.pid_gone(pid), "stalled scanimage was not killed"
    assert "scan-timeout" in event_kinds(m)
    finished_cleanly(m, env, v, jid)


def test_stall_without_pages_is_an_error(m, env, monkeypatch):
    monkeypatch.setattr(m, "SCAN_STALL_SEC", 1)
    env.scanner.set(mode="stall", pages=0)
    jid, v = run(m, source="ADF", size="A4")
    assert v["state"] == "error" and v["file"] == "", v
    assert v["msg"] == "Error: No new page arrived for 1 seconds, so scanning stopped. Check the panel for a jam or sleep mode"
    finished_cleanly(m, env, v, jid)


# ---------------------------------------------------------------- resolution honesty

def test_smfp_open_failure_falls_back_to_wsd_and_says_so(m, env):
    env.scanner.set(mode="openfail_smfp")
    jid, v = run(m, res="300", enhance=False)
    assert v["state"] == "done", v
    assert "; scanner returned 200 dpi (requested 300)" in v["msg"]
    calls = env.scanner.scan_calls()
    assert [sup.argval(c["argv"], "-d") for c in calls] == ["smfp:net;127.0.0.1", m.DEV]
    assert sup.argval(calls[1]["argv"], "--resolution") == "200"          # WSD is clamped to 200
    assert_pages(m, v["file"], 1, 148, 210)            # geometry from the REAL dpi, not the request
    kinds = event_kinds(m)
    assert "scan-smfp-fallback" in kinds and "scan-res-downgrade" in kinds
    finished_cleanly(m, env, v, jid)


def test_device_delivering_lower_dpi_is_reported(m, env):
    env.scanner.set(real_dpi=150)
    jid, v = run(m, res="300", enhance=False)
    assert v["state"] == "done", v
    assert "; scanner returned 150 dpi (requested 300)" in v["msg"]
    assert_pages(m, v["file"], 1, 148, 210)
    finished_cleanly(m, env, v, jid)


def test_stall_after_wsd_fallback_keeps_real_dpi(m, env, monkeypatch):
    # regression: salvage used to stamp the REQUESTED 300 dpi on 200 dpi WSD pages → 2/3-size PDF
    monkeypatch.setattr(m, "SCAN_STALL_SEC", 1.5)
    env.scanner.set(mode="openfail_smfp,stall")
    jid, v = run(m, res="300", enhance=False)
    assert v["state"] == "partial", v
    assert_pages(m, v["file"], 1, 148, 210)
    assert "scan-res-downgrade" in event_kinds(m)
    finished_cleanly(m, env, v, jid)


def _png_with_dpi(path, dpi):
    from PIL import Image
    im = Image.new("RGB", (50, 70), "white")
    if dpi:
        im.save(path, dpi=(dpi, dpi))
    else:
        im.save(path)
    return str(path)


@pytest.mark.parametrize("meta,res,want", [
    (300, "300", ("300", "")),
    (301, "300", ("300", "")),                                  # PNG pixels-per-metre rounding
    (200, "300", ("200", "; scanner returned 200 dpi (requested 300)")),
    (150, "600", ("150", "; scanner returned 150 dpi (requested 600)")),
    (None, "300", ("300", "")),                                 # no metadata: trust the request
])
def test_device_dpi(m, tmp_path, meta, res, want):
    p = _png_with_dpi(tmp_path / "p.png", meta)
    assert m._device_dpi([p], res) == want


def test_device_dpi_unreadable_page(m, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\nbroken")
    assert m._device_dpi([str(bad)], "300") == ("300", "")


# ---------------------------------------------------------------- processing failures

def test_postprocess_failure_is_reported_in_done_message(m, env, monkeypatch):
    real = m._enhance_inplace
    n = {"i": 0}

    def flaky(im):
        n["i"] += 1
        if n["i"] == 2:
            raise MemoryError("page 2 too big")
        return real(im)
    monkeypatch.setattr(m, "_enhance_inplace", flaky)
    env.scanner.set(pages=3)
    jid, v = run(m, source="ADF", size="A4")
    assert v["state"] == "done", v
    assert "; text enhanced" in v["msg"]
    assert "; orientation or enhancement failed on 1 pages (originals retained)" in v["msg"]
    fails = [e for e in events(m) if e[1] == "scan-postprocess-fail"]
    assert len(fails) == 1 and fails[0][2].startswith("p2 Text enhancement failed")
    assert_pages(m, v["file"], 3, 220, 300)            # calibrated A4 window, plain output
    finished_cleanly(m, env, v, jid)


def test_deskew_failure_on_every_page(m, env, monkeypatch):
    def boom(im, *a):
        raise RuntimeError("osd broke")
    monkeypatch.setattr(m, "_osd_guess", boom)
    env.scanner.set(pages=2)
    jid, v = run(m, source="ADF", size="A4", enhance=False, deskew=True)
    assert v["state"] == "done", v
    assert "; orientation or enhancement failed on 2 pages (originals retained)" in v["msg"]
    assert "rotated" not in v["msg"].split("⚠️")[0]        # nothing claimed as rotated
    finished_cleanly(m, env, v, jid)


def test_postprocess_crash_counts_as_problem(m, env, monkeypatch):
    def crash(*a):
        raise OSError("disk hiccup")
    monkeypatch.setattr(m, "postprocess_page", crash)
    jid, v = run(m)
    assert v["state"] == "done", v
    assert "; orientation or enhancement failed on 1 pages (originals retained)" in v["msg"] and "text enhanced" not in v["msg"]
    finished_cleanly(m, env, v, jid)


def test_img2pdf_timeout_is_not_reported_as_scanner_stall(m, env):
    def slow(cmd, real_run, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, 300)
    env.img2pdf_hook = slow
    jid, v = run(m, enhance=False)
    assert v["state"] == "error" and v["file"] == "", v
    assert "PDF generation timed out" in v["msg"] and "No new page" not in v["msg"]
    assert v["msg"].startswith("Error: ")
    assert "scan-error" in event_kinds(m) and "scan-timeout" not in event_kinds(m)
    finished_cleanly(m, env, v, jid)


def test_img2pdf_failure_hides_command_line(m, env):
    def fail(cmd, real_run, *a, **kw):
        raise subprocess.CalledProcessError(1, cmd)
    env.img2pdf_hook = fail
    jid, v = run(m, enhance=False)
    assert v == {"state": "error", "msg": "Error: " + HIDDEN, "file": ""}
    finished_cleanly(m, env, v, jid)


def test_unexpected_exception_ends_cleanly(m, env, monkeypatch):
    def boom(*a):
        raise RuntimeError("boom in dpi")
    monkeypatch.setattr(m, "_device_dpi", boom)
    jid, v = run(m, source="ADF", size="A4")
    assert v["state"] == "error" and v["file"] == ""
    assert v["msg"] == "Error: boom in dpi (If this persists, send this message to the administrator)"
    assert "scan-error" in event_kinds(m)
    finished_cleanly(m, env, v, jid)


# ---------------------------------------------------------------- OCR wiring

@pytest.mark.parametrize("ocr_result,tail", [
    (None, "; searchable text added"),
    ("OCR timed out", "; OCR failed (OCR timed out); the PDF is still usable"),
])
def test_ocr_result_in_message_and_pdf_hidden_during_ocr(m, env, monkeypatch, ocr_result, tail):
    during = []

    def fake_ocr(path):
        during.append((os.path.basename(path), snapshot(m)))
        return ocr_result
    monkeypatch.setattr(m, "ocr_pdf", fake_ocr)
    jid, v = run(m, enhance=False, do_ocr=True)
    assert v["state"] == "done", v
    assert v["msg"].endswith(tail + "; deleted automatically after 15 minutes)")
    assert "Recognizing text... (1 pages, about 5 seconds)" in msgs(env, jid)
    (name, snap), = during
    assert name == ".part-" + v["file"]                 # OCR works on the hidden .part file
    assert snap["final"] == [] and snap["rows"] == []
    finished_cleanly(m, env, v, jid)


# ---------------------------------------------------------------- through the HTTP route

def test_scan_route_end_to_end(m, env, client, monkeypatch):
    monkeypatch.setattr(m, "printer_online", lambda: (False, "192.0.2.1"))
    r = client.post("/scan", data={"source": "Flatbed", "res": "100", "mode": "Color", "build": m.BUILD_ID,
                                   "size": "A5", "enh": "1"})
    assert r.status_code == 202
    jid = r.get_json()["job"]
    assert sup.wait_until(lambda: client.get("/status", query_string={"scan": jid}).get_json()
                          ["scan"]["state"] != "running", timeout=30)
    s = client.get("/status", query_string={"scan": jid}).get_json()
    assert s["scan"]["state"] == "done", s
    assert sup.wait_until(lambda: not m.scan_lock.locked(), 5)
    rows = client.get("/files").get_json()["files"]
    assert [row["name"] for row in rows] == [s["scan"]["file"]]
    pdf = client.get("/f/" + s["scan"]["file"])
    assert pdf.status_code == 200 and pdf.data[:5] == b"%PDF-"
    finished_cleanly(m, env, s["scan"], jid)
