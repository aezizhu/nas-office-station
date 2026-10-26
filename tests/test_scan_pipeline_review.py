"""Office scan station: plain scanner output, read-only edge checks and persistent review state.

Since 2026-10-19 a scan is the scanner's own output: the station requests the right window, writes the real DPI
and builds a lossless PDF. It never crops, rotates, darkens or drops pages unless asked. Measured on the real
M437 feeder (raw frames, same sheets through HP's A4 window and the full-width window): paper rides centred on
~150 mm (HP assumes 148), the firmware whites out ~2 mm at both window sides and at the leading edge, the driver
pads white after the paper ends. The synthetic frames below model exactly that.
"""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

STATION = Path(__file__).resolve().parents[1] / "app"


def _put_img2pdf_on_path(tmp_path, monkeypatch):
    """The station builds PDFs with the img2pdf CLI: use the real one, else `python -m img2pdf` if importable."""
    if shutil.which("img2pdf"):
        return
    try:
        import img2pdf  # noqa: F401
    except ImportError:
        return
    shim = tmp_path / "bin" / "img2pdf"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\nexec "%s" -m img2pdf "$@"\n' % sys.executable)
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim.parent) + os.pathsep + os.environ["PATH"])


def need_img2pdf():
    if not shutil.which("img2pdf"):
        pytest.skip("img2pdf is not installed; the station needs it to build PDFs")


@pytest.fixture
def station(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _put_img2pdf_on_path(tmp_path, monkeypatch)
    monkeypatch.syspath_prepend(str(STATION))
    spec = importlib.util.spec_from_file_location("pipeline_station", STATION / "scan_app.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    monkeypatch.setattr(app, "smfp_device", lambda: "smfp:net;test")
    monkeypatch.setattr(app, "printer_status", lambda: {"online": True})
    return app


def backing(H, W, dpi, rng):
    """The real backing beside the sheet: 252/253 per column, flipping like a slow two-state chain. Measured at
    300 dpi: mean row-to-row change 0.048, 0.095 over 2 rows, 0.142 over 3 - so it looks busier at lower dpi."""
    step = 300 / dpi
    n = int(np.ceil(H * step)) + 1
    up, rows = np.zeros(W, bool), np.empty((n, W), bool)
    for r in range(n):
        u = rng.random(W)
        up = np.where(up, u >= 0.080, u < 0.034)
        rows[r] = up
    return 252 + rows[np.minimum(np.round(np.arange(H) * step).astype(int), n - 1)].astype(np.int16)


def device_frame(window, dpi, paper_w=210.0, paper_h=297.0, offset=0.0, seal=None, shade=False, pink=False):
    """What the M437 feeder returns for window [l, t, x, y] (mm): the flickering backing beside the paper, a textured
    sheet centred on 150 mm (+offset), ~2 mm firmware white at both window sides and the leading edge, white filler
    after the paper, or a ~1.6 mm white band at the end when the sheet fills the window's length.
    seal='R' draws a 3 mm red cross-page-seal slice on the paper's right edge; shade puts a light grey band
    (230-254) across the page centre; pink makes a carbonless-form sheet (green channel ~194, as measured)."""
    l, _, x, y = window
    k = dpi / 25.4
    W, H = round(x * k), round(y * k)
    rng = np.random.default_rng(1)
    a = backing(H, W, dpi, rng)
    px0, px1 = round((150 + offset - paper_w / 2 - l) * k), round((150 + offset + paper_w / 2 - l) * k)
    ph = min(H, round(paper_h * k))
    c0, c1 = max(0, px0), min(W, px1)
    tile = np.round(rng.normal(0, 0.75, (64, 64))).astype(np.int16)     # paper texture ~0.85 (real 0.43-0.93)
    if c1 > c0:
        a[:ph, c0:c1] = 251 + np.tile(tile, (ph // 64 + 1, (c1 - c0) // 64 + 1))[:ph, :c1 - c0]
        for yy in range(round(30 * k), ph - round(30 * k), round(10 * k)):
            a[yy:yy + 3, c0 + (c1 - c0) // 6:c1 - (c1 - c0) // 6] = 90
    if shade and c1 > c0:
        cx, sh = (c0 + c1) // 2, round(12 * k)
        a[round(60 * k):round(180 * k), cx - sh:cx + sh] = np.clip(
            np.round(rng.normal(242, 6, (round(180 * k) - round(60 * k), 2 * sh))), 230, 254).astype(np.int16)
    chans = [a.copy(), a.copy(), a.copy()]                  # R, G, B
    if pink and c1 > c0:
        chans[0][:ph, c0:c1], chans[1][:ph, c0:c1], chans[2][:ph, c0:c1] = 253, a[:ph, c0:c1] - 57, 221
    strip, fade = round(1.5 * k), max(1, round(0.5 * k))
    for ch in chans:
        ch[ph:] = 255
        if ph >= H:
            ch[H - round(1.6 * k):] = 255                   # the window ended on the sheet
        ch[:, :strip] = 255                                 # firmware white at both window sides, all channels,
        ch[:, W - strip:] = 255
        ch[:, strip:strip + fade] = np.maximum(ch[:, strip:strip + fade], 254)   # fading into the image
        ch[:, W - strip - fade:W - strip] = np.maximum(ch[:, W - strip - fade:W - strip], 254)
    rgb = np.stack([ch.clip(0, 255).astype(np.uint8) for ch in chans], axis=2)
    if seal == "R" and 0 < px1 <= W:
        rgb[round(100 * k):round(140 * k), max(0, px1 - round(3 * k)):px1] = (205, 45, 45)
    rgb[:strip] = 255
    return Image.fromarray(rgb, "RGB")


def scan(app, monkeypatch, size="A4", dpi=100, frame=device_frame, paper_white=False, **paper):
    """Run the real run_scan with scanimage replaced by the device model above; returns (job view, argv window)."""
    need_img2pdf()
    seen = {}

    def acquire(cmd, workdir):
        win = [int(cmd[cmd.index(f) + 1]) for f in ("-l", "-t", "-x", "-y")]
        seen["window"] = win
        frame(win, dpi, **paper).save(Path(workdir) / "p001.png", dpi=(dpi, dpi))
        return 7, ""

    monkeypatch.setattr(app, "_run_scanimage", acquire)
    jid = app.new_job("scan")
    app.run_scan(jid, "ADF", str(dpi), "Color", size, False, False, False, False, paper_white)
    return app.job_view(jid, "scan"), seen["window"]


def page_mm(app, name):
    from pypdf import PdfReader
    page = PdfReader(Path(app.SCANS) / name).pages[0]
    return [round(float(v) * 25.4 / 72, 1) for v in (page.mediabox.width, page.mediabox.height)]


def test_a4_feeder_scan_is_the_scanners_output_through_the_calibrated_window(station, monkeypatch):
    view, window = scan(station, monkeypatch, size="A4")
    assert window == [40, 0, 220, 300]
    assert view["state"] == "done", view
    assert page_mm(station, view["file"]) == pytest.approx([220, 300], abs=0.6)    # not cropped, not snapped
    assert "page 220 x 300 mm, both side edges complete" in view["msg"]
    assert station._file_rows()[0]["quality"] == {"state": "done", "issues": []}


@pytest.mark.parametrize("dpi", [100, 150, 200, 300])
def test_centred_sheet_is_clean_at_every_offered_resolution(station, monkeypatch, dpi):
    # the backing looks busier at lower dpi (longer line pitch); a fixed 300 dpi texture threshold called it paper
    view, _ = scan(station, monkeypatch, size="A4", dpi=dpi)
    assert view["state"] == "done", view
    assert "both side edges complete" in view["msg"]


@pytest.mark.parametrize("dpi", [100, 150, 300])
def test_sheet_in_the_blind_strip_is_flagged_at_every_resolution(station, monkeypatch, dpi):
    view, _ = scan(station, monkeypatch, size="A4", dpi=dpi, offset=4)          # paper edge 1 mm under the strip
    assert view["state"] == "review", view
    assert "right edge" in view["msg"] and "edges complete" not in view["msg"]


def test_sheet_longer_than_the_chosen_size_is_not_passed(station, monkeypatch):
    # A4 sheet with Letter chosen: the window ends on the sheet and its last 15 mm were never scanned
    view, window = scan(station, monkeypatch, size="Letter", dpi=150)
    assert window == [37, 0, 226, 282]
    assert view["state"] == "review", view
    assert "The sheet is longer than the scan area; its trailing edge may be missing" in view["msg"] and "edges complete" not in view["msg"]


def test_sheet_wider_than_the_chosen_size_is_named_and_the_advice_given_once(station, monkeypatch):
    # 2026-10-19 paper test: office forms fed sideways through the A4 window lost ~38 mm on each side
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, paper_w=297, paper_h=210)
    assert view["state"] == "review", view
    assert "The sheet is wider than the selected size, often because it was fed sideways; both side edges are incomplete" in view["msg"]
    assert view["msg"].count("Next:") == 1 and "feed A4 short edge first" in view["msg"]


def test_shading_in_the_page_centre_does_not_hide_a_sheet_under_a_strip(station, monkeypatch):
    # a shaded header or form field in the centre must not raise the paper threshold (delta review, 2026-10-19)
    view, _ = scan(station, monkeypatch, size="A4", dpi=300, offset=4, shade=True)
    assert view["state"] == "review", view
    assert "right edge" in view["msg"] and "edges complete" not in view["msg"]


@pytest.mark.parametrize("offset,state", [(0, "done"), (4, "review")])
def test_coloured_paper_is_checked_like_white_paper(station, monkeypatch, offset, state):
    # pink carbonless forms read ~194 in green; a white-only mask once ignored them ("edge is indistinct")
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, offset=offset, pink=True)
    assert view["state"] == state, view
    assert "edge is indistinct" not in view["msg"]


def test_a_page_the_edge_check_could_not_read_is_not_passed(station, monkeypatch):
    def broken(*a, **k):
        raise MemoryError("edge check")
    monkeypatch.setattr(station, "edge_check", broken)
    view, _ = scan(station, monkeypatch, size="A4")
    assert view["state"] == "review", view
    assert "The edge check failed" in view["msg"] and "edges complete" not in view["msg"]


def test_full_capture_is_the_whole_feed_path(station, monkeypatch):
    view, window = scan(station, monkeypatch, size="full", paper_w=297, paper_h=210)    # A4 fed sideways
    assert window == [0, 0, 297, 432]
    assert view["state"] == "done", view                     # inherent loss at the long edges is a note
    assert "on each long edge are unseen" in view["msg"] and "edges complete" not in view["msg"]
    assert page_mm(station, view["file"]) == pytest.approx([297, 432], abs=0.6)


@pytest.mark.parametrize("offset", [-2, 0, 2])
def test_cross_page_seal_on_the_paper_edge_survives_the_a4_window(station, monkeypatch, offset):
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, offset=offset, seal="R")
    assert view["state"] == "done", view
    from pypdf import PdfReader
    img = next(iter(PdfReader(Path(station.SCANS) / view["file"]).pages[0].images)).image.convert("RGB")
    a = np.asarray(img, dtype=np.int16)
    cols = np.flatnonzero(((a[..., 0] - np.maximum(a[..., 1], a[..., 2])) > 60).any(axis=0))
    assert len(cols) and (cols[-1] - cols[0] + 1) / (150 / 25.4) >= 2.9


def test_ink_against_a_blind_strip_asks_for_a_check(station, monkeypatch):
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, offset=6, seal="R")    # sheet 6 mm off centre
    assert view["state"] == "review", view
    assert "right edge contains content or enters an unseen scanner strip" in view["msg"] and "edges complete" not in view["msg"]
    row = station._file_rows()[0]
    assert row["quality"]["state"] == "review" and row["quality"]["issues"]


def test_scan_route_requests_no_processing_and_refuses_auto_crop(station, monkeypatch):
    calls = []
    monkeypatch.setattr(station, "run_scan", lambda jid, *args: calls.append(args))
    client = station.app.test_client()
    form = {"source": "ADF", "res": "300", "mode": "Color", "size": "A4", "build": station.BUILD_ID}
    r = client.post("/scan", data=form)
    assert r.status_code == 202
    for _ in range(100):
        if calls:
            break
        __import__("time").sleep(0.02)
    assert calls == [("ADF", "300", "Color", "A4", False, False, False, False, False)]   # nothing asked for
    while station.scan_lock.locked():
        __import__("time").sleep(0.02)
    r = client.post("/scan", data=dict(form, size="auto"))
    assert r.status_code == 400 and not r.get_json().get("stale")
    r = client.post("/scan", data=dict(form, paper_white="1"))         # the page's tick box (on by default there)
    assert r.status_code == 202
    for _ in range(100):
        if len(calls) == 2:
            break
        __import__("time").sleep(0.02)
    assert calls[1] == ("ADF", "300", "Color", "A4", False, False, False, False, True)


def test_a_tab_from_before_the_update_is_told_to_refresh(station, monkeypatch):
    # tabs opened before 2026-10-19 post darkening + rotation (checked by default there) and no build fingerprint
    monkeypatch.setattr(station, "run_scan", lambda *a: pytest.fail("must not scan"))
    old_tab = {"source": "ADF", "res": "300", "mode": "Color", "size": "full", "enh": "1", "deskew": "1"}
    for form in (old_tab, dict(old_tab, build="0123456789ab")):
        r = station.app.test_client().post("/scan", data=form)
        assert r.status_code == 400 and r.get_json() == {"error": "The station was updated. Refresh this page before continuing",
                                                         "stale": True}
    assert not station.scan_lock.locked()


def test_quality_is_persisted_before_pdf_is_published(station, tmp_path, monkeypatch):
    need_img2pdf()
    source = tmp_path / "p.png"
    Image.new("RGB", (100, 100), "white").save(source, dpi=(100, 100))
    output = Path(station.SCANS) / "scan-20261019-120000-1p.pdf"
    quality = {"state": "review", "issues": ["Paper sizeReview required"]}
    replace = station.os.replace

    def check_replace(src, dst):
        if str(dst) == str(output):
            assert station._read_scan_quality(output) == quality
        return replace(src, dst)

    monkeypatch.setattr(station.os, "replace", check_replace)
    station._publish_pdf([str(source)], str(output), quality=quality)
    assert output.exists()


def test_review_state_survives_a_jobs_reset(station, monkeypatch):
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, offset=6, seal="R")
    station._jobs.clear()
    rows = station.app.test_client().get("/files").get_json()["files"]
    assert rows[0]["name"] == view["file"] and rows[0]["quality"]["state"] == "review"


def test_quality_write_failure_keeps_the_pdf_and_reads_as_review(station, monkeypatch):
    def failed(*args):
        raise OSError("metadata write failed")
    monkeypatch.setattr(station, "_write_scan_quality", failed)
    view, _ = scan(station, monkeypatch, size="A4")
    assert (Path(station.SCANS) / view["file"]).is_file()
    assert station._file_rows()[0]["quality"]["state"] == "review"


def test_orphan_recovery_remains_flagged(station):
    need_img2pdf()
    work = Path(station.SCANS) / ".work-20261019-123000-test"
    work.mkdir()
    Image.new("RGB", (100, 100), "white").save(work / "p001.png", dpi=(100, 100))
    station.recover_orphan_workdirs()
    row = station._file_rows()[0]
    assert row["quality"]["state"] == "partial" and row["quality"]["issues"]


def test_copy_keeps_quality_if_original_expires_during_preview(station, monkeypatch):
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, offset=6, seal="R")
    source = Path(station.SCANS) / view["file"]

    def finish(directory):
        source.unlink()
        source.with_name(source.name + ".quality.json").unlink()
        return 1, "220×300 mm"
    monkeypatch.setattr(station, "_finish_print_doc", finish)
    response = station.app.test_client().post("/copy", data={"name": view["file"]})
    assert response.status_code == 200
    assert response.get_json()["quality"]["state"] == "review"


def test_deleting_a_scan_removes_its_e_mail_copy_and_check_record(station, monkeypatch):
    view, _ = scan(station, monkeypatch, size="A4", dpi=150)
    name = view["file"]
    assert (Path(station.SCANS) / (name + ".quality.json")).exists()
    r = station.app.test_client().post("/delete", data={"name": name})
    assert r.status_code == 200
    left = sorted(p.name for p in Path(station.SCANS).iterdir() if p.name != "events.log")
    assert left == [], left


def test_page_hints_get_the_real_room_beside_each_size(station):
    boot = __import__("json").loads(station._boot_json().replace("\\u003c", "<"))
    room = boot["adf_side_room"]
    assert room["A4"] == room["A5"] == room["Letter"] == 5.0 and room["A3"] == 0.0
    assert set(room) == set(station.SCAN_SIZES) - {"full"}


# ---------------------------------------------------------------- paper white (owner decision 2026-10-19 18:40 SGT)
def paper_page(path, paper=(255, 251, 255), noise=0.75, size=(800, 1100), dpi=300, mode="RGB"):
    """A page as the M437 delivers it: paper with HP's faint pink cast and texture, black text, a red seal disc and
    a blue signature stroke."""
    rng = np.random.default_rng(3)
    h, w = size[1], size[0]
    a = np.empty((h, w, 3), np.float64)
    a[:] = paper
    a += rng.normal(0, noise, (h, w, 1)) * (np.array(paper) < 255)
    a[200:206, 100:700] = (60, 60, 62)                               # a text line
    yy, xx = np.mgrid[:h, :w]
    seal = (yy - 600) ** 2 + (xx - 400) ** 2 < 90 ** 2
    a[seal] = (205, 45, 45)
    a[900:904, 150:500] = (40, 60, 170)                              # signature stroke
    im = Image.fromarray(np.clip(np.round(a), 0, 255).astype(np.uint8), "RGB")
    if mode == "L":
        im = im.convert("L")
    im.save(path, dpi=(dpi, dpi))
    return seal


def test_paper_white_turns_hp_pink_paper_white_and_leaves_marks_alone(station, tmp_path):
    p = tmp_path / "p001.png"
    seal = paper_page(p)
    before = np.asarray(Image.open(p)).astype(int)
    info = station.whiten_paper(str(p), 300)
    with Image.open(p) as im:
        after = np.asarray(im).astype(int)
        assert round(im.info["dpi"][0]) == 300
    assert "gain" in info, info
    paper = np.ones(after.shape[:2], bool)
    paper[190:216, :] = paper[880:914, :] = False
    paper[seal] = False
    paper[480:720, 280:520] = False
    assert (after[paper] == 255).all(axis=1).mean() > 0.99                 # pure white paper
    assert np.abs(after[seal] - before[seal]).max() <= 3                   # red seal unchanged to the eye
    assert np.abs(after[200:206, 100:700] - before[200:206, 100:700]).max() <= 2   # black text
    assert np.abs(after[900:904, 150:500] - before[900:904, 150:500]).max() <= 3   # blue signature


def test_paper_white_keeps_coloured_paper_and_dark_pages_as_scanned(station, tmp_path):
    for paper, reason in (((250, 192, 218), "coloured paper"), ((90, 90, 95), "no paper to measure")):
        p = tmp_path / ("p-%d.png" % paper[1])
        paper_page(p, paper=paper)
        raw = p.read_bytes()
        info = station.whiten_paper(str(p), 300)
        assert info["kept"] == reason, info
        assert p.read_bytes() == raw                                       # untouched file


def test_paper_white_works_on_grey_scans(station, tmp_path):
    p = tmp_path / "g.png"
    paper_page(p, paper=(251, 251, 251), mode="L")
    info = station.whiten_paper(str(p), 300)
    with Image.open(p) as im:
        assert im.mode == "L"
        a = np.asarray(im)
    assert "gain" in info and (a[50:150, 50:750] == 255).mean() > 0.99


def test_paper_white_measures_the_paper_at_the_centre_of_feeder_pages(station, tmp_path):
    # a narrow slip in the full feed path: the backing beside it is brighter than the paper and must not be used
    p = tmp_path / "slip.png"
    a = np.full((1100, 2970, 3), (255, 253, 255), np.uint8)               # backing
    rng = np.random.default_rng(5)
    slip = np.clip(np.round(rng.normal(251.5, 0.75, (1100, 600))), 0, 255).astype(np.uint8)
    a[:, 1185:1785, 1] = slip                                             # 60 mm slip at 300 dpi-ish scale
    Image.fromarray(a, "RGB").save(p, dpi=(254, 254))
    station.whiten_paper(str(p), 254, feeder=True)
    out = np.asarray(Image.open(p))
    assert (out[:, 1200:1770] == 255).all(axis=2).mean() > 0.99


def test_default_scan_delivers_white_paper_and_keeps_the_original(station, monkeypatch):
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, paper_white=True)
    assert view["state"] == "done", view
    assert "paper background corrected (original scan saved separately)" in view["msg"] and "both side edges complete" in view["msg"]
    from pypdf import PdfReader
    img = next(iter(PdfReader(Path(station.SCANS) / view["file"]).pages[0].images)).image.convert("RGB")
    a = np.asarray(img)
    k = 150 / 25.4
    sheet = a[round(40 * k):round(280 * k), round(20 * k):round(30 * k)]   # plain paper, left margin of the sheet
    assert (sheet == 255).all(axis=2).mean() > 0.98
    row = station._file_rows()[0]
    assert row["raw"] is True                                             # "Original scan" link: the untouched frames
    record = __import__("json").loads((Path(station.SCANS) / (".raw-" + view["file"][:-4]) / "record.json").read_text())
    assert record["params"]["paper_white"] is True
    assert "gain" in record["pages"]["p001.png"]["paper_white"]


def test_edge_check_still_judges_the_untouched_frame(station, monkeypatch):
    view, _ = scan(station, monkeypatch, size="A4", dpi=150, offset=4, paper_white=True)
    assert view["state"] == "review" and "right edge" in view["msg"], view
