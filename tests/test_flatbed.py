"""Flatbed production path `crop_pages` (not dead code): paper on the A3 bed must come out at the
exact standard size with DPI metadata. ADF cases live in test_adf.py / test_adf_edge.py.
Noise is seeded per case so a failure is reproducible."""
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw

DPI = 150


def mkpage(path, pw, ph, lid, paper, off, noise, rot=0, blank=False, bed=(297, 420), seed=0):
    W, H = round(bed[0] / 25.4 * DPI), round(bed[1] / 25.4 * DPI)
    pg = Image.new("L", (round(pw / 25.4 * DPI), round(ph / 25.4 * DPI)), paper)
    if not blank:
        d = ImageDraw.Draw(pg)
        for y in range(pg.size[1] // 4, pg.size[1] // 4 + min(250, pg.size[1] // 2), 30):
            d.rectangle([pg.size[0] // 8, y, pg.size[0] * 7 // 8, y + 8], fill=30)
    if rot:
        pg = pg.rotate(-rot, expand=True, fillcolor=lid, resample=Image.BICUBIC)
    b = Image.new("L", (W, H), lid)
    o = round(off / 25.4 * DPI)
    b.paste(pg, (o, o))
    rng = np.random.default_rng(seed)
    a = np.asarray(b, dtype=np.float32) + rng.normal(0, noise, (H, W))
    Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "L").save(path)
    return str(path)


# (name, [page spec (pw, ph, lid, paper, offset_mm, noise[, rot_deg])], expected (w_mm, h_mm))
CASES = [
    ("A4 掀盖黑底", [(210, 297, 30, 255, 0, 3)], (210, 297)),
    ("A5 掀盖黑底", [(148, 210, 30, 255, 0, 3)], (148, 210)),
    ("A6 小纸(旧版必失败)", [(105, 148, 30, 255, 0, 3)], (105, 148)),
    ("B5", [(176, 250, 30, 255, 0, 3)], (176, 250)),
    ("Letter", [(216, 279, 30, 255, 0, 3)], (216, 279)),
    ("A4 合盖纸暗3级", [(210, 297, 255, 252, 0, 1)], (210, 297)),
    ("A4 偏移10mm", [(210, 297, 30, 255, 10, 3)], (210, 297)),
    ("A4 歪5度", [(210, 297, 30, 255, 10, 3, 5)], (210, 297)),
    ("A4 噪声σ8", [(210, 297, 30, 255, 0, 8)], (210, 297)),
    ("A3 满幅", [(297, 420, 255, 250, 0, 1)], (297, 420)),
]


@pytest.mark.parametrize("name,specs,expect", CASES, ids=[c[0] for c in CASES])
def test_crop_pages_flatbed(m, tmp_path, name, specs, expect):
    ps = [mkpage(tmp_path / ("p%d.png" % i), *sp, seed=i) for i, sp in enumerate(specs)]
    label = m.crop_pages(ps, DPI)
    assert isinstance(label, str) and label
    for p in ps:
        with Image.open(p) as im:
            size_mm = (round(im.size[0] / DPI * 25.4), round(im.size[1] / DPI * 25.4))
            dpi = im.info.get("dpi")
        assert size_mm == expect, "%s: got %s, label %s" % (name, size_mm, label)
        assert dpi is not None and round(dpi[0]) == DPI, "DPI metadata missing/wrong: %r" % (dpi,)
        os.remove(p)


# F27: paper touching the bed corner and skewed — the closing step used to grow the mask past the
# image border, measuring the A4 4-6 mm too big → cropped as Legal.
SKEW_AT_CORNER = [
    ("A4 贴角歪4度", [(210, 297, 30, 255, 0, 3, 4)], (210, 297)),
    ("A4 偏3mm歪2度", [(210, 297, 30, 255, 3, 3, 2)], (210, 297)),
]


@pytest.mark.parametrize("name,specs,expect", SKEW_AT_CORNER, ids=[c[0] for c in SKEW_AT_CORNER])
def test_skewed_a4_at_bed_corner_is_a4_not_legal(m, tmp_path, name, specs, expect):
    ps = [mkpage(tmp_path / ("s%d.png" % i), *sp, seed=i + 1) for i, sp in enumerate(specs)]
    label = m.crop_pages(ps, DPI)
    assert label.startswith("A4") and "Legal" not in label, label
    with Image.open(ps[0]) as im:
        assert (round(im.size[0] / DPI * 25.4), round(im.size[1] / DPI * 25.4)) == expect
