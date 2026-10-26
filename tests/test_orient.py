"""Orientation through the production function postprocess_page (what run_scan calls).
Real sideways contract pages in the explicitly supplied SCAN_SAMPLES directory are used when present (skip otherwise); the wiring
(rotation direction, failure reporting, the retry rule of _osd_degrees, the tesseract output parser)
is tested deterministically without samples."""
import glob
import os
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image, ImageDraw

import station_support as sup

SAMPLE_DIR = os.environ.get("SCAN_SAMPLES", "")
SAMPLES = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.png"))) if SAMPLE_DIR else []


def osd(p):
    r = subprocess.run(["tesseract", p, "-", "--psm", "0", "-l", "osd"],
                       capture_output=True, text=True, timeout=90)
    d = 0
    for line in r.stdout.splitlines():
        if line.startswith("Rotate:"):
            d = int(line.split(":")[1])
    return d


@pytest.mark.parametrize("sample", SAMPLES or [pytest.param(None, marks=pytest.mark.skip(
    reason="set SCAN_SAMPLES to an approved local sample directory"))])
def test_real_sideways_page_is_turned_upright(m, tmp_path, sample):
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    t = str(tmp_path / "s.png")
    shutil.copy(sample, t)
    b_size = Image.open(t).size
    b_text = float(np.percentile(np.asarray(Image.open(t).convert("L"), dtype=np.float32), 5))
    rot, enhanced, problems = m.postprocess_page(t, 200, True, True)
    assert problems == []
    assert osd(t) == 0, "page still needs rotating after postprocess_page (rot=%d)" % rot
    if rot in (90, 270):
        assert Image.open(t).size == b_size[::-1]
    if enhanced:
        a2 = np.asarray(Image.open(t).convert("L"), dtype=np.float32)
        assert float(np.percentile(a2, 5)) <= b_text


def _page(tmp_path, mode="RGB"):
    im = Image.new(mode, (300, 200), "white")
    ImageDraw.Draw(im).rectangle([10, 10, 60, 30], fill="black")   # marker in the top-left corner
    p = str(tmp_path / "o.png")
    im.save(p)
    return p


@pytest.mark.parametrize("deg,size,corner", [
    (90, (200, 300), "top-right"),      # "Rotate: 90" = turn 90° clockwise
    (180, (300, 200), "bottom-right"),
    (270, (200, 300), "bottom-left"),
])
def test_postprocess_rotates_the_right_way(m, tmp_path, monkeypatch, deg, size, corner):
    monkeypatch.setattr(m, "_osd_degrees", lambda im: deg)
    p = _page(tmp_path)
    rot, enhanced, problems = m.postprocess_page(p, 150, True, False)
    assert (rot, enhanced, problems) == (deg, False, [])
    with Image.open(p) as im:
        assert im.size == size
        assert round(im.info["dpi"][0]) == 150
        a = np.asarray(im.convert("L"))
    h, w = a.shape
    q = {"top-right": a[:h // 3, -w // 3:], "bottom-right": a[-h // 3:, -w // 3:],
         "bottom-left": a[-h // 3:, :w // 3]}[corner]
    assert (q < 50).any(), "marker not in the %s corner after rotating %d°" % (corner, deg)


def test_postprocess_ignores_odd_angles_and_upright(m, tmp_path, monkeypatch):
    p = _page(tmp_path)
    raw = open(p, "rb").read()
    for deg in (0, 45):
        monkeypatch.setattr(m, "_osd_degrees", lambda im, d=deg: d)
        assert m.postprocess_page(p, 150, True, False) == (0, False, [])
        assert open(p, "rb").read() == raw             # nothing to do → file not rewritten


def test_postprocess_reports_failures_instead_of_swallowing(m, tmp_path, monkeypatch):
    p = _page(tmp_path)

    def boom(im):
        raise RuntimeError("tesseract exploded")
    monkeypatch.setattr(m, "_osd_degrees", boom)
    monkeypatch.setattr(m, "_enhance_inplace", lambda im: (_ for _ in ()).throw(MemoryError("oom")))
    rot, enhanced, problems = m.postprocess_page(p, 150, True, True)
    assert (rot, enhanced) == (0, False)
    assert len(problems) == 2
    assert problems[0].startswith("Orientation correction failed") and "tesseract exploded" in problems[0]
    assert problems[1].startswith("Text enhancement failed")


# ---------------------------------------------------------------- _osd_degrees retry rule

@pytest.mark.parametrize("size,script,want,calls", [
    ((3000, 4000), [(0, 12.0)], 0, [2200]),                   # upright + confident: ONE call only
    ((3000, 4000), [(90, 16.1)], 90, [2200]),
    ((3000, 4000), [(0, 0.4), (90, 17.2)], 90, [2200, None]),   # thumbnail unsure → full-res retry
    ((3000, 4000), [(0, 0.4), (90, 2.0)], 0, [2200, None]),    # retry also unsure → leave it
    ((1500, 2000), [(90, 1.0)], 0, [2200]),                   # small page: nothing to retry at
    ((1500, 2000), [(180, 5.0)], 180, [2200]),
    ((1500, 2000), [(270, 3.0)], 270, [2200]),                # min_conf is inclusive
])
def test_osd_degrees_retries_only_when_unsure(m, monkeypatch, size, script, want, calls):
    seen = []

    def fake_once(im, cap):
        seen.append(cap)
        return script[len(seen) - 1]
    monkeypatch.setattr(m, "_osd_once", fake_once)
    assert m._osd_degrees(Image.new("L", size, 255)) == want
    assert seen == calls


# ---------------------------------------------------------------- _osd_once parser

OSD_OUT = ("Page number: 0\nOrientation in degrees: 270\nRotate: 90\n"
           "Orientation confidence: 16.15\nScript: Han\nScript confidence: 3.33\n")


@pytest.mark.parametrize("stdout,want", [
    (OSD_OUT, (90, 16.15)),
    ("Rotate: 0\nOrientation confidence: 20.0\n", (0, 20.0)),
    ("Rotate: 450\nOrientation confidence: 5\n", (90, 5.0)),
    ("Rotate: x\nOrientation confidence: 9\n", (0, 0.0)),
    ("Rotate: 180\nOrientation confidence: nan-ish\n", (180, 0.0)),
    ("", (0, 0.0)),
])
def test_osd_once_parses_tesseract(m, monkeypatch, stdout, want):
    seen = {}

    def hook(cmd, real_run, *a, **kw):
        assert cmd[0] == "tesseract" and cmd[-4:] == ["--psm", "0", "-l", "osd"]
        with Image.open(cmd[1]) as im:
            seen["size"], seen["mode"] = im.size, im.mode
        seen["tmp"] = cmd[1]
        return sup.completed(stdout=stdout)
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    assert m._osd_once(Image.new("RGB", (5000, 3000), "white"), 2200) == want
    assert max(seen["size"]) <= 2200 and seen["mode"] == "L"   # thumbnail, greyscale
    assert not os.path.exists(seen["tmp"])                     # temp file cleaned up


def test_osd_once_full_resolution_and_errors(m, monkeypatch):
    seen = {}

    def hook(cmd, real_run, *a, **kw):
        with Image.open(cmd[1]) as im:
            seen["size"] = im.size
        raise subprocess.TimeoutExpired(cmd, 120)
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    assert m._osd_once(Image.new("L", (3000, 4000), 255), None) == (0, 0.0)
    assert seen["size"] == (3000, 4000)
