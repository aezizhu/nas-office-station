"""ADF widened window (2026-08-25 骑缝章 incident): window 222 mm wide, paper 210 mm, fed with an
offset of 0/±2/±4 mm; the last 3 mm at the paper's right edge carry a red stamp. Output must be
210×297 (A4) with the stamp still there. ±4 mm is the measured 2026-08-26 feed skew — centring would
eat the stamp, so the crop must be anchored on the trimmed margins. Both outside looks: device white
and dark backing plate."""
import numpy as np
import pytest
from PIL import Image

DPI = 200


def mm(v):
    return round(v / 25.4 * DPI)


def page(path, offset_mm, outside, seed=0):
    W, H = mm(222), mm(297)
    pw = mm(210)
    rng = np.random.default_rng(seed)
    a = np.full((H, W, 3), 255.0 if outside == "white" else 40.0, np.float32)
    x0 = mm(6 + offset_mm)
    paper = np.full((H, pw, 3), 253.6, np.float32) + rng.normal(0, 9, (H, pw, 3))
    a[:, x0:x0 + pw] = np.clip(paper, 0, 255)
    a[mm(120):mm(180), x0 + pw - mm(3):x0 + pw] = (205, 45, 45)     # red stamp on the last 3 mm
    a[mm(60):mm(70), x0 + mm(30):x0 + pw - mm(30)] = 40               # a line of black text
    Image.fromarray(a.astype("uint8"), "RGB").save(path)
    return str(path)


@pytest.mark.parametrize("outside", ["white", "dark"])
@pytest.mark.parametrize("off", [0, 2, -2, 4, -4])
def test_widened_window_keeps_edge_stamp(m, tmp_path, outside, off):
    p = page(tmp_path / "p.png", off, outside, seed=off + 10)
    label = m.normalize_pages([p], DPI)
    with Image.open(p) as im:
        w, h = im.size
        a = np.asarray(im.convert("YCbCr")).astype(float)
    red = (a[:, -mm(3):, 2] > 150).mean()          # share of red pixels in the rightmost 3 mm
    assert abs(w - mm(210)) <= 2 and abs(h - mm(297)) <= 2, (w, h)
    assert label == "A4"
    assert red > 0.15, "edge stamp was cut off (red share %.2f)" % red
