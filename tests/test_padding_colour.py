"""Padding / rotation fill on COLOUR scans must be white. Regression: Image.new(RGB, size, 255) and
rotate(fillcolor=255) paint pure red (255, 0, 0) on RGB images — L-mode tests never saw it."""
import numpy as np
import pytest
from PIL import Image, ImageDraw

DPI = 100


def px(v_mm):
    return round(v_mm / 25.4 * DPI)


def colour_page(path, w_mm, h_mm, seed=0):
    rng = np.random.default_rng(seed)
    W, H = px(w_mm), px(h_mm)
    a = np.full((H, W, 3), 250.0, np.float32) + rng.normal(0, 6, (H, W, 3))   # paper with grain
    im = Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "RGB")
    d = ImageDraw.Draw(im)
    for y in range(px(20), H - px(20), px(10)):
        d.rectangle([px(20), y, W - px(20), y + px(2)], fill=(60, 60, 60))
    im.save(path, dpi=(DPI, DPI))
    return str(path)


def test_normalize_pages_pads_colour_page_with_white(m, tmp_path):
    # 205×290 mm (margins trimmed by the device) snaps UP to A4 → bottom/right strips are padding
    p = colour_page(tmp_path / "p.png", 205, 290)
    label = m.normalize_pages([p], DPI)
    assert label == "A4"
    with Image.open(p) as im:
        assert im.mode == "RGB"
        assert im.size == (px(210), px(297))
        a = np.asarray(im)
    pad_bottom = a[px(291):, :px(200)]
    pad_right = a[:px(280), px(206):]
    for name, region in (("bottom", pad_bottom), ("right", pad_right)):
        assert region.size, name
        assert (region == 255).all(), "%s padding is not white: unique colours %s" % (
            name, np.unique(region.reshape(-1, 3), axis=0)[:5].tolist())


@pytest.mark.parametrize("mode", ["RGB", "L"])
def test_fit_page_rotation_and_pad_fill_is_white(m, tmp_path, mode):
    W, H = px(180), px(250)
    im = Image.new(mode, (W, H), (240, 240, 240) if mode == "RGB" else 240)
    ImageDraw.Draw(im).rectangle([px(20), px(40), px(160), px(60)], fill=(0, 0, 0) if mode == "RGB" else 0)
    p = str(tmp_path / "f.png")
    im.save(p)
    info = {"ang": 5.0, "cx": W / 2, "cy": H / 2, "ax": None, "ay": None}
    rotated = m.fit_page(p, DPI, 210, 297, info)      # target bigger than the image → pad too
    assert rotated is True
    with Image.open(p) as out:
        assert out.size == (px(210), px(297))
        assert out.mode == mode
        a = np.asarray(out.convert("RGB"))
    white = np.array([255, 255, 255])
    assert (a[1, 1] == white).all(), "rotation fill corner is %s" % a[1, 1].tolist()   # rotated-in corner
    assert (a[-2, -2] == white).all(), "padding is %s" % a[-2, -2].tolist()            # canvas padding
    red = (a[:, :, 0] > 200) & (a[:, :, 1] < 60) & (a[:, :, 2] < 60)
    assert not red.any(), "found %d pure-red fill pixels" % int(red.sum())
