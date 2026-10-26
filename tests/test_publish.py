"""_as_jpeg (C04: 300 dpi colour A4 as lossless PNG made 10-15 MB/page PDFs) and _publish_pdf.
Since 2026-10-19 the main PDF is lossless (the scanner's output unchanged) and the JPEG version is a .small- copy."""
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw

import station_support as sup


def page(tmp_path, name, mode, dpi=(200, 200), size=(400, 560)):
    color = {"RGB": (250, 250, 250), "L": 250, "RGBA": (250, 250, 250, 255), "P": 0, "1": 1,
             "LA": (250, 255), "CMYK": (0, 0, 0, 0)}[mode]
    im = Image.new(mode, size, color)
    if mode in ("RGB", "L"):
        d = ImageDraw.Draw(im)
        d.rectangle([50, 50, 350, 60], fill=(205, 45, 45) if mode == "RGB" else 30)   # thin red stroke
    p = str(tmp_path / name)
    kw = {"dpi": dpi} if dpi else {}
    im.save(p, **kw)
    return p


@pytest.mark.parametrize("mode", ["RGB", "L"])
def test_rgb_and_gray_become_jpeg_444_q90_same_dpi(m, tmp_path, mode):
    p = page(tmp_path, "p001.png", mode, dpi=(300, 300))
    (out,) = m._as_jpeg([p])
    assert out == str(tmp_path / "p001.jpg")
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.mode == mode
        assert tuple(round(v) for v in im.info["dpi"]) == (300, 300)   # page geometry unchanged
        assert im.size == (400, 560)
        if mode == "RGB":
            from PIL import JpegImagePlugin
            assert JpegImagePlugin.get_sampling(im) == 0                # 4:4:4 — red strokes stay sharp
            a = np.asarray(im).astype(int)
            assert np.abs(a[55, 200] - [205, 45, 45]).max() <= 12       # stamp colour survives
    assert os.path.exists(p)                                            # source PNG not consumed


def test_quality_is_90(m, tmp_path, monkeypatch):
    seen = {}
    real_save = Image.Image.save

    def spy(self, fp, format=None, **kw):
        if format == "JPEG":
            seen.update(kw)
        return real_save(self, fp, format, **kw)
    monkeypatch.setattr(Image.Image, "save", spy)
    m._as_jpeg([page(tmp_path, "q.png", "RGB")])
    assert seen["quality"] == 90 and seen["subsampling"] == 0


def test_missing_dpi_defaults_to_300(m, tmp_path):
    (out,) = m._as_jpeg([page(tmp_path, "nodpi.png", "RGB", dpi=None)])
    with Image.open(out) as im:
        assert tuple(round(v) for v in im.info["dpi"]) == (300, 300)


@pytest.mark.parametrize("mode", ["RGBA", "P", "1", "LA"])
def test_other_modes_stay_png(m, tmp_path, mode):
    p = page(tmp_path, "x.png", mode)
    assert m._as_jpeg([p]) == [p]
    assert not os.path.exists(str(tmp_path / "x.jpg"))


def test_conversion_failure_falls_back_to_png(m, tmp_path):
    good = page(tmp_path, "p001.png", "RGB")
    bad = str(tmp_path / "p002.png")
    open(bad, "wb").write(b"\x89PNG\r\n\x1a\nbroken")
    assert m._as_jpeg([good, bad]) == [str(tmp_path / "p001.jpg"), bad]   # order kept, nothing dropped


def _filters(pikepdf, path):
    with pikepdf.open(path) as pdf:
        out = []
        for pg in pdf.pages:
            imgs = pg.get_images() if hasattr(pg, "get_images") else pg.images
            out += [str(img.get("/Filter")) for img in imgs.values()]
    return out


def test_publish_keeps_scanner_pages_lossless_plus_small_jpeg_copy(m, tmp_path, img2pdf):
    """Since 2026-10-19 the PDF is the scanner's output unchanged (PNG embedded losslessly); the JPEG q90 version
    (C04: lossless pages are too big to e-mail) is a separate .small- copy next to it."""
    pikepdf = pytest.importorskip("pikepdf")
    rng = np.random.default_rng(1)                   # paper texture, like a real scan (flat pages compress as PNG)
    ps = []
    for i, mode in enumerate(["RGB", "L", "RGBA"], 1):
        a = np.clip(245 + rng.normal(0, 6, (1169, 827, 4)), 0, 255).astype("uint8")
        im = Image.fromarray(a[..., :3], "RGB") if mode == "RGB" else (
            Image.fromarray(a[..., 0], "L") if mode == "L" else Image.fromarray(a, "RGBA"))
        ps.append(str(tmp_path / ("p%03d.png" % i)))
        im.save(ps[-1], dpi=(100, 100))
    out = os.path.join(m.SCANS, "scan-20260923-101010-3p.pdf")
    assert m._publish_pdf(ps, out) is None
    assert sorted(os.listdir(m.SCANS)) == [".small-scan-20260923-101010-3p.pdf",
                                           "scan-20260923-101010-3p.pdf"]           # no .part left
    assert _filters(pikepdf, out) == ["/FlateDecode"] * 3                          # lossless
    small = os.path.join(m.SCANS, ".small-scan-20260923-101010-3p.pdf")
    assert _filters(pikepdf, small) == ["/DCTDecode", "/DCTDecode", "/FlateDecode"]   # RGBA stays lossless
    for path in (out, small):
        for w, h in sup.pdf_page_sizes(path):
            assert abs(sup.mm_from_pt(w) - 210) < 1 and abs(sup.mm_from_pt(h) - 297) < 1


def test_jpeg_pdf_is_much_smaller_than_png_pdf(m, tmp_path, img2pdf):
    """The point of C04: a noisy colour page must shrink a lot."""
    rng = np.random.default_rng(0)
    a = np.clip(245 + rng.normal(0, 8, (1169, 827, 3)), 0, 255).astype("uint8")
    p = str(tmp_path / "noisy.png")
    Image.fromarray(a, "RGB").save(p, dpi=(100, 100))
    png_size = os.path.getsize(p)
    out = os.path.join(m.SCANS, "scan-20260923-101010-1p.pdf")
    m._publish_pdf([p], out)
    small = os.path.join(m.SCANS, ".small-scan-20260923-101010-1p.pdf")   # the e-mail copy carries the JPEG
    assert os.path.getsize(small) < png_size / 2 and os.path.getsize(small) < os.path.getsize(out) / 2
