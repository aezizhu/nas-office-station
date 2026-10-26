"""Text darkening (postprocess_page enhance) must not shift colour: red stamps and blue signatures keep
their hue (per-channel ratio shift < 0.02), grey text gets darker, paper stays white."""
import numpy as np
import pytest
from PIL import Image, ImageDraw

DPI = 200
W, H = round(210 / 25.4 * DPI), round(297 / 25.4 * DPI)


@pytest.fixture()
def page(tmp_path):
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for y in range(300, 1600, 50):
        d.rectangle([200, y, 1400, y + 16], fill=(127, 127, 127))       # greyish text
    d.ellipse([900, 1750, 1350, 2150], outline=(205, 45, 45), width=22)  # red stamp
    d.rectangle([300, 1850, 700, 1900], fill=(45, 70, 200))              # blue signature
    p = str(tmp_path / "c.png")
    im.save(p, dpi=(DPI, DPI))
    return p


def sample(path):
    a = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    return {"纸底": a[100:200, 100:200].reshape(-1, 3).mean(axis=0),
            "字": a[300:316, 300:1300].reshape(-1, 3).mean(axis=0),
            "红章": a[1760:1772, 1100:1150].reshape(-1, 3).mean(axis=0),
            "蓝签": a[1860:1890, 350:650].reshape(-1, 3).mean(axis=0)}


def hue(c):
    c = np.array(c, dtype=float)
    mx = c.max()
    return c / mx if mx > 0 else c


def test_enhance_keeps_colours(m, page):
    before = sample(page)
    rot, enhanced, problems = m.postprocess_page(page, DPI, False, True)
    assert (rot, problems) == (0, [])
    assert enhanced is True
    after = sample(page)
    for k in ("纸底", "字", "红章", "蓝签"):
        shift = float(np.abs(hue(before[k]) - hue(after[k])).max())
        assert shift < 0.02, "%s hue shifted by %.4f: %s → %s" % (k, shift, before[k], after[k])
    assert after["字"].mean() < before["字"].mean() - 20, "grey text was not darkened"
    assert after["纸底"].min() >= 254, "paper is no longer white"
    for k in ("红章", "蓝签"):                        # saturated colours are left (almost) untouched
        assert np.abs(after[k] - before[k]).max() <= 3, (k, before[k], after[k])
    with Image.open(page) as im:
        assert round(im.info["dpi"][0]) == DPI
