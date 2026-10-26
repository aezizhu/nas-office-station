"""Blank-page drop, plain-language errors (friendly), OCR end-to-end, event log basics."""
import os
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image, ImageDraw

import station_support as sup


def mk(path, blank, seed):
    im = Image.new("L", (1654, 2339), 255)
    if not blank:
        dr = ImageDraw.Draw(im)
        for y in range(300, 1500, 40):
            dr.rectangle([200, y, 1400, y + 12], fill=30)
    rng = np.random.default_rng(seed)
    a = np.asarray(im, dtype=np.float32) + rng.normal(0, 2, (2339, 1654))
    Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "L").save(path, dpi=(200, 200))
    return str(path)


# ---------------------------------------------------------------- blank pages

def test_drop_blank_pages_drops_only_blank(m, tmp_path):
    pages = [mk(tmp_path / ("x%d.png" % i), b, i)
             for i, b in enumerate([False, True, False, True, True])]
    kept, dropped = m.drop_blank_pages(pages, str(tmp_path))
    assert (len(kept), dropped) == (2, 3)
    assert kept == [pages[0], pages[2]]               # order preserved, content pages kept


def test_drop_blank_pages_all_blank_keeps_everything(m, tmp_path):
    pages = [mk(tmp_path / ("b%d.png" % i), True, i) for i in range(2)]
    kept, dropped = m.drop_blank_pages(pages, str(tmp_path))
    assert (kept, dropped) == (pages, 0)              # never hand out an empty PDF


def test_drop_blank_pages_unreadable_page_is_kept(m, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    good = mk(tmp_path / "g.png", False, 1)
    kept, dropped = m.drop_blank_pages([str(bad), good], str(tmp_path))
    assert (kept, dropped) == ([str(bad), good], 0)   # can't judge → keep, never delete by mistake


@pytest.mark.parametrize("bg,content", [(60, 120), (120, 60), (150, 190), (40, 40)])
def test_dark_pages_are_never_blank(m, tmp_path, bg, content):
    """C02: a dark cover/slide (paper white < 200) is 'content' — even a uniform one is never dropped."""
    im = Image.new("L", (800, 1100), bg)
    d = ImageDraw.Draw(im)
    for y in range(200, 900, 60):
        d.rectangle([100, y, 700, y + 10], fill=content)
    p = str(tmp_path / "dark.png")
    im.save(p)
    assert m.ink_ratio(p) == 1.0
    blank = mk(tmp_path / "white-blank.png", True, 9)
    kept, dropped = m.drop_blank_pages([p, blank], str(tmp_path))
    assert (kept, dropped) == ([p], 1)                # the white blank goes, the dark page stays


def test_white_page_ink_ratio_unchanged(m, tmp_path):
    assert m.ink_ratio(mk(tmp_path / "w.png", True, 1)) < m.BLANK_INK
    assert m.ink_ratio(mk(tmp_path / "t.png", False, 2)) > 0.01


# ---------------------------------------------------------------- friendly()

BUSY = "The scanner is busy. Wait 10 seconds and retry. If it remains busy, restart the printer"
NO_PAPER = "The feeder did not detect paper. Insert the stack face up, short edge first, until the printer detects it, then select Retry. For paper on the glass, select Use flatbed"
NO_SPACE = "Station storage is full. Contact the administrator"
PASSWORD = "This PDF is encrypted. Save an unencrypted copy before uploading"
IO_ERR = "Scanner communication stopped. Check printer power and its network cable, then retry"
HIDDEN = "Processing failed. Retry, or contact the administrator with the error time if it persists"
TAIL = " (If this persists, send this message to the administrator)"


@pytest.mark.parametrize("err,want", [
    ("scanimage: sane_start: Device busy", BUSY),
    ("Document feeder out of documents", NO_PAPER),
    ("scanimage: sane_start: No documents", NO_PAPER),
    ("No space left on device", NO_SPACE),
    ("Command Line Error: Incorrect password", PASSWORD),
    ("scanimage: sane_read: Error during device I/O", IO_ERR),
    ("Syntax Error: Couldn't find trailer dictionary", "Cannot open this file. It may be damaged or not a valid PDF"),
    ("cannot identify image file 'x'", "Cannot open this image. It may be damaged or use an unsupported format"),
    ("MemoryError", "This job exceeds station memory. Lower the resolution or split the job"),
    ("", "Something went wrong. Retry, or contact the administrator if it persists"),
    (None, "Something went wrong. Retry, or contact the administrator if it persists"),
    ("some weird internal thing", "some weird internal thing" + TAIL),
])
def test_friendly_mapping(m, err, want):
    assert m.friendly(err) == want


CANT_REACH = "Cannot reach the scanner. Check printer power and its network cable"


def test_friendly_open_of_device_invalid_argument_is_cant_reach(m):
    # F32: "open of device … Invalid argument" means the scanner can't be opened, not a bad setting
    assert m.friendly("scanimage: open of device smfp:net;192.0.2.50 failed: Invalid argument") == CANT_REACH
    assert m.friendly("scanimage: sane_start: Invalid argument") == "The scanner does not support these settings. Choose another resolution or size"


@pytest.mark.parametrize("err,want", [
    ("scanimage: sane_start: Scanner cover is open", "The scanner or feeder cover is open. Close it and retry"),
    ("printer cover open", "The printer cover is open. Close it and retry"),
    ("scanimage: sane_start: Operation not supported", "The scanner does not support this operation. Change the settings and retry"),
    ("scanimage: open of device x failed: Access to resource has been denied", CANT_REACH),
    ("scanimage: sane_start: Access to resource has been denied", "Scanner access was denied. Another computer may be using it; wait and retry"),
    ("scanimage: sane_read: Out of memory", "This job exceeds available memory. Lower the resolution or split the job"),
    ("[Errno 2] No such file or directory: 'soffice'",
     "A required program or file is missing. Contact the administrator with the time of the error"),
    (FileNotFoundError(2, "No such file or directory", "unoconv"),
     "A required program or file is missing. Contact the administrator with the time of the error"),
    ("cancel: cancel-job failed: Job #29 is already completed - can't cancel.", "This job has already been sent and cannot be cancelled"),
    ("cancel: cancel-job failed: Job #29 is finished and cancel is not possible.", "This job has already been sent and cannot be cancelled"),
])
def test_friendly_new_keys(m, err, want):
    out = m.friendly(err)
    assert out == want
    assert "soffice" not in out and "Errno" not in out


@pytest.mark.parametrize("err", [
    "Command '['img2pdf', '/root/scans/.work-x/p001.png']' returned non-zero exit status 1.",
    "failed to write /root/scans/.work-20260923-101010-abcdef/p001.png",
    "/usr/bin/ocrmypdf crashed",
])
def test_friendly_hides_command_lines_and_paths(m, err):
    out = m.friendly(err)
    assert out == HIDDEN
    assert "/root" not in out and "img2pdf" not in out


@pytest.mark.parametrize("err", [
    FileNotFoundError(2, "No such file or directory", "/root/scans/.work-20260923-101010-abcdef/p001.png"),
    'cannot open "/root/scans/x/p001.png"',
    "decoder failed (/root/scans/x/p001.png)",
    "workdir=/root/scans/.work-x/",
    "path:/root/scans/x/p001.png",
    "PermissionError-ish text: '/var/lib/cups/x/y'",
])
def test_friendly_hides_quoted_and_prefixed_paths(m, err):
    out = m.friendly(err)
    assert "/root" not in out and "/var/lib" not in out, out


def test_friendly_keeps_harmless_slashes(m):
    # a slash inside ordinary words is not a path: the message must stay readable, not be hidden
    assert m.friendly("A4/A3 mixed stack") == "A4/A3 mixed stack (If this persists, send this message to the administrator)"


def test_friendly_never_mentions_raspberry_pi(m):
    samples = ["device busy", "no documents", "jam", "cover open", "invalid argument", "no such device",
               "open of device", "error during device i/o", "incorrect password", "encrypt",
               "may not be a pdf", "syntax error", "cannot identify image", "connection", "timed out",
               "timeout", "no space", "cannot allocate memory", "memoryerror", "permission", "", "x",
               "Command 'x' returned non-zero exit status 1", " /a/b/c"]
    for s in samples:
        out = m.friendly(s)
        assert "树莓派" not in out and "Raspberry" not in out and "Pi " not in out, (s, out)


# ---------------------------------------------------------------- OCR end-to-end

def _big_font():
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=64)            # Pillow >= 10.1
    except TypeError:
        pass
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, 64)
    pytest.skip("no scalable font to draw OCR test text")


def test_ocr_end_to_end(m, tmp_path, img2pdf, monkeypatch):
    real = None
    for d in os.environ.get("PATH", "").split(os.pathsep):   # guard stub shadows ocrmypdf: find the real one
        c = os.path.join(d, "ocrmypdf")
        if os.path.isfile(c) and os.access(c, os.X_OK) and "guard-bin" not in d:
            real = d
            break
    if real is None or not shutil.which("pdftotext"):
        pytest.skip("ocrmypdf / pdftotext not installed")
    monkeypatch.setenv("PATH", real + os.pathsep + os.environ["PATH"])
    im = Image.new("L", (1654, 2339), 255)
    font = _big_font()
    for i, line in enumerate(["OFFICE SCAN STATION", "OCR REGRESSION TEST", "PAGE ONE OF ONE"]):
        ImageDraw.Draw(im).text((150, 300 + i * 120), line, fill=0, font=font)
    page = str(tmp_path / "t.png")
    im.save(page, dpi=(200, 200))
    pdf = str(tmp_path / "t.pdf")
    with open(pdf, "wb") as f:
        subprocess.run(["img2pdf", page], stdout=f, check=True)
    before = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True).stdout
    assert before.strip() == ""                               # image-only PDF: no text layer yet
    assert m.ocr_pdf(pdf) is None
    assert not os.path.exists(pdf + ".ocr.pdf")
    txt = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True).stdout
    assert len(sup.pdf_page_sizes(pdf) or [1]) == 1
    assert "STATION" in txt.upper(), "OCR produced no searchable text: %r" % txt[:200]


def test_ocr_failure_is_reported_not_swallowed(m, tmp_path, bindir):
    sup.write_exe(str(bindir / "ocrmypdf"), "#!/bin/sh\necho 'ocrmypdf: language chi_sim missing' >&2\nexit 3\n")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(sup.pdf_bytes([sup.A4_PT]))
    before = pdf.read_bytes()
    err = m.ocr_pdf(str(pdf))
    assert err and "chi_sim" in err
    assert pdf.read_bytes() == before                  # original untouched on failure
    assert not os.path.exists(str(pdf) + ".ocr.pdf")


# ---------------------------------------------------------------- event log

def test_log_event_writes_line(m):
    m.log_event("test", "单元测试写入\n第二行")
    with open(m.EVENTS, encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 1
    stamp, kind, msg = lines[0].split("\t")
    assert kind == "test" and msg == "单元测试写入 第二行"     # newlines flattened: one event = one line
    assert len(stamp) == 15 and stamp[8] == "-"
