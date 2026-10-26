"""Text darkening through the production entry point postprocess_page(do_orient=False, do_enhance=True)
(the old enhance_text() is gone). Real scan samples in the explicitly supplied SCAN_SAMPLES directory (made with
`pdfimages -png sample.pdf /path/to/samples/p`) are used when present; the synthetic case always runs."""
import glob
import os
import shutil

import numpy as np
import pytest
from PIL import Image, ImageDraw

SAMPLE_DIR = os.environ.get("SCAN_SAMPLES", "")
SAMPLES = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.png"))) if SAMPLE_DIR else []


def stats(p):
    a = np.asarray(Image.open(p).convert("L"), dtype=np.float32)
    return {"纸底": float(np.percentile(a, 92)), "字芯": float(np.percentile(a, 2)),
            "动态范围": float(np.percentile(a, 92) - np.percentile(a, 2)),
            "灰底占比": float(((a > 200) & (a < 245)).mean() * 100)}


def check_darker(before, after, enhanced):
    if not enhanced:                                   # "already fine" is a legal answer
        assert after == before
        return
    assert after["字芯"] <= before["字芯"], (before, after)          # text core gets darker (or equal)
    assert after["纸底"] >= before["纸底"] - 0.5, (before, after)     # paper never gets darker
    assert after["动态范围"] >= before["动态范围"], (before, after)


def test_synthetic_grey_scan_gets_crisper(m, tmp_path):
    rng = np.random.default_rng(7)
    a = np.full((1200, 900), 226.0, np.float32) + rng.normal(0, 3, (1200, 900))
    im = Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "L")
    d = ImageDraw.Draw(im)
    for y in range(100, 1100, 30):
        d.rectangle([80, y, 820, y + 6], fill=118)     # washed-out grey text on grey paper
    p = str(tmp_path / "g.png")
    im.save(p, dpi=(200, 200))
    b = stats(p)
    rot, enhanced, problems = m.postprocess_page(p, 200, False, True)
    assert (rot, enhanced, problems) == (0, True, [])
    a2 = stats(p)
    check_darker(b, a2, True)
    assert a2["字芯"] < b["字芯"] - 20 and a2["纸底"] > b["纸底"] + 10
    assert a2["灰底占比"] < b["灰底占比"]              # grey background is lifted towards white


@pytest.mark.parametrize("sample", SAMPLES or [pytest.param(None, marks=pytest.mark.skip(
    reason="set SCAN_SAMPLES to an approved local sample directory"))])
def test_real_sample_enhance(m, tmp_path, sample):
    t = str(tmp_path / "s.png")
    shutil.copy(sample, t)
    b = stats(t)
    rot, enhanced, problems = m.postprocess_page(t, 200, False, True)
    assert rot == 0 and problems == []
    check_darker(b, stats(t), enhanced)


# ---------------------------------------------------------------- dark / mid-tone pages (C01)

def _dark_page(bg, content, coverage_rows, mode="L"):
    """bg background with bars of `content` covering `coverage_rows` of every 40 rows."""
    im = Image.new(mode, (800, 1100), bg if mode == "L" else (bg, bg, bg))
    d = ImageDraw.Draw(im)
    for y in range(100, 1000, 40):
        d.rectangle([80, y, 720, y + coverage_rows - 1], fill=content if mode == "L" else (content,) * 3)
    return im


@pytest.mark.parametrize("mode", ["L", "RGB"])
@pytest.mark.parametrize("bg,content", [(70, 30), (100, 40), (140, 60), (70, 110), (120, 150)])
def test_dark_page_untouched(m, tmp_path, mode, bg, content):
    """Dark cover / dark slide whose white point is below 160: the levels curve must not touch it."""
    p = str(tmp_path / "d.png")
    _dark_page(bg, content, 8, mode).save(p, dpi=(200, 200))
    raw = open(p, "rb").read()
    med = float(np.median(np.asarray(Image.open(p).convert("L"))))
    rot, enhanced, problems = m.postprocess_page(p, 200, False, True)
    assert (rot, enhanced, problems) == (0, False, [])
    assert open(p, "rb").read() == raw
    assert abs(float(np.median(np.asarray(Image.open(p).convert("L")))) - med) <= 2


@pytest.mark.parametrize("mode", ["L", "RGB"])
@pytest.mark.parametrize("bg,content", [(70, 240), (100, 180), (140, 220), (120, 255)])
def test_dark_slide_with_light_text_keeps_background(m, tmp_path, bg, content, mode):
    # regression (residual C01): light text covering >3% lifts P97 above 160; the median guard keeps
    # the dark background (it used to move 100 → 127)
    p = str(tmp_path / "s.png")
    _dark_page(bg, content, 8, mode).save(p, dpi=(200, 200))
    med = float(np.median(np.asarray(Image.open(p).convert("L"))))
    m.postprocess_page(p, 200, False, True)
    assert abs(float(np.median(np.asarray(Image.open(p).convert("L")))) - med) <= 2


@pytest.mark.parametrize("white,expect_none", [(150, True), (159, True), (161, False), (230, False)])
def test_lum_curve_white_point_threshold(m, white, expect_none):
    y = np.full((200, 200), float(white), np.float32)
    y[:40] = 20.0                                            # 20% dark content
    assert (m._lum_curve(y) is None) == expect_none


@pytest.mark.parametrize("dark_share,expect_none", [(0.45, False), (0.55, True)])
def test_lum_curve_median_threshold(m, dark_share, expect_none):
    """White point 240 either way; once more than half the page is dark (median < 160) → untouched."""
    y = np.full((200, 200), 240.0, np.float32)
    y[: int(200 * dark_share)] = 90.0
    assert (m._lum_curve(y) is None) == expect_none
