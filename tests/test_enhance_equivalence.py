"""The strip-wise enhancer (_enhance_inplace, via postprocess_page(do_orient=False)) must be
PIXEL-IDENTICAL to the old full-frame maths it replaced for memory reasons (6.1 GB peak → OOM).

Oracle = the enhance branch of postprocess_page at git 24483cc
(.claude/skills/raspi5-office-scan-print-station/scan_app.py), copied verbatim below, using the
app's own _lum_curve (unchanged). Covered: RGB with and without the white-balance branch firing,
WB-only (curve says "already fine"), nothing-to-do, RGBA, L; heights that are not multiples of
256 or 8, and extra-small strip sizes so many strip boundaries are crossed."""
import numpy as np
import pytest
from PIL import Image, ImageDraw


def old_enhance(m, im):
    """Verbatim copy of the 24483cc enhance branch. Returns (image, enhanced)."""
    enhanced = False
    try:
        if im.mode in ("RGB", "RGBA"):
            arr = np.asarray(im.convert("RGB"), dtype=np.float32)
            wp = np.percentile(arr[::8, ::8].reshape(-1, 3), 90, axis=0)
            if wp.min() > 180 and (wp.max() - wp.min()) > 1.5:
                scale = np.minimum(wp.max() / np.maximum(wp, 1.0), 1.08)
                arr = np.clip(arr * scale, 0, 255)
                im = Image.fromarray(arr.astype("uint8"), "RGB")
                enhanced = True
            ycc = im.convert("YCbCr")
            y, cb, cr = ycc.split()
            ya = np.asarray(y, dtype=np.float32)
            r = m._lum_curve(ya[::4, ::4])
            if r is not None:
                black, white = r
                cba = np.asarray(cb, dtype=np.float32)
                cra = np.asarray(cr, dtype=np.float32)
                chroma = np.maximum(np.abs(cba - 128.0), np.abs(cra - 128.0))
                w = np.clip((18.0 - chroma) / 10.0, 0.0, 1.0)
                curved = np.clip((ya - black) * (255.0 / (white - black)), 0, 255)
                ya = ya * (1.0 - w) + curved * w
                im = Image.merge("YCbCr", (Image.fromarray(ya.astype("uint8"), "L"), cb, cr)
                                 ).convert("RGB")
                enhanced = True
        else:
            a = np.asarray(im.convert("L"), dtype=np.float32)
            r = m._lum_curve(a[::4, ::4])
            if r is not None:
                black, white = r
                im = Image.fromarray(
                    np.clip((a - black) * (255.0 / (white - black)), 0, 255).astype("uint8"), "L")
                enhanced = True
    except Exception:
        pass
    return im, enhanced


def make(kind, W, H, seed):
    """Synthetic scans that drive each branch. Returns a PIL image."""
    rng = np.random.default_rng(seed)
    paper = {"rgb_wb": (247, 238, 246),      # magenta-ish paper → white balance fires
             "rgb_neutral": (236, 236, 236), # neutral grey paper → only the luminance curve
             "rgb_wb_only": (250, 244, 250), # WB fires, curve then says "already fine"
             "rgb_nothing": (255, 255, 255), # white paper, black text → nothing to do
             "rgba_wb": (247, 238, 246), "rgba_neutral": (236, 236, 236),
             "l_curve": 232, "l_nothing": 255}[kind]
    gray = kind.startswith("l_")
    ink = 0 if kind in ("rgb_wb_only", "rgb_nothing", "l_nothing") else 115
    if gray:
        a = np.full((H, W), float(paper), np.float32)
    else:
        a = np.empty((H, W, 3), np.float32)
        a[...] = paper
    a += rng.normal(0, 3 if ink else 0.0, a.shape)
    im = Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "L" if gray else "RGB")
    d = ImageDraw.Draw(im)
    for y in range(3, H - 3, 9):                           # text lines
        d.rectangle([W // 10, y, W - W // 10, y + 2], fill=ink if gray else (ink, ink, ink))
    if not gray and W > 40 and H > 40:
        d.ellipse([W // 2, H // 3, W // 2 + 30, H // 3 + 30], outline=(205, 45, 45), width=4)  # stamp
        d.rectangle([5, H - 20, 40, H - 12], fill=(45, 70, 200))                                # signature
    if kind.startswith("rgba"):
        alpha = Image.fromarray(rng.integers(0, 256, (H, W), dtype=np.uint8), "L")
        im = im.convert("RGBA")
        im.putalpha(alpha)
    return im


KINDS = ["rgb_wb", "rgb_neutral", "rgb_wb_only", "rgb_nothing", "rgba_wb", "rgba_neutral",
         "l_curve", "l_nothing"]
SIZES = [(301, 700), (257, 263), (96, 517), (64, 13), (120, 512)]   # 700%8=4, 263%8=7, 517%256=5


@pytest.mark.parametrize("size", SIZES, ids=["%dx%d" % s for s in SIZES])
@pytest.mark.parametrize("kind", KINDS)
def test_postprocess_matches_old_full_frame(m, tmp_path, kind, size):
    W, H = size
    src = make(kind, W, H, seed=W * 1000 + H)
    p = str(tmp_path / "p.png")
    src.save(p)
    raw = open(p, "rb").read()

    want, want_enh = old_enhance(m, Image.open(p).copy())
    rot, got_enh, problems = m.postprocess_page(p, 150, False, True)
    assert (rot, problems) == (0, [])
    assert got_enh == want_enh, "enhanced flag differs from the old code"
    if not want_enh:
        assert open(p, "rb").read() == raw, "page rewritten although nothing was enhanced"
        return
    got = Image.open(p)
    assert got.mode == want.mode and got.size == want.size
    diff = np.asarray(got).astype(int) - np.asarray(want).astype(int)
    assert not diff.any(), "%d pixels differ (max %d)" % (int((diff != 0).sum()), int(abs(diff).max()))


def test_branches_are_really_exercised(m):
    """Guard for the test itself: the fixtures must hit WB / curve / neither as intended."""
    def flags(kind):
        im = make(kind, 301, 700, 1)
        arr = np.asarray(im.convert("RGB"), dtype=np.float32) if im.mode != "L" else None
        wb = False
        if arr is not None:
            wp = np.percentile(arr[::8, ::8].reshape(-1, 3), 90, axis=0)
            wb = bool(wp.min() > 180 and (wp.max() - wp.min()) > 1.5)
        return wb, old_enhance(m, im)[1]
    assert flags("rgb_wb") == (True, True)
    assert flags("rgb_neutral") == (False, True)
    assert flags("rgb_nothing") == (False, False)
    assert flags("rgba_wb") == (True, True)
    assert flags("l_curve")[1] is True and flags("l_nothing")[1] is False
    im = make("rgb_wb_only", 301, 700, 1)                  # WB fires but the curve declines
    arr = np.asarray(im, dtype=np.float32)
    wp = np.percentile(arr[::8, ::8].reshape(-1, 3), 90, axis=0)
    scale = np.minimum(wp.max() / np.maximum(wp, 1.0), 1.08)
    wbim = Image.fromarray(np.clip(arr * scale, 0, 255).astype("uint8"), "RGB")
    y = np.asarray(wbim.convert("YCbCr"))[:, :, 0].astype(np.float32)
    assert wp.min() > 180 and wp.max() - wp.min() > 1.5
    assert m._lum_curve(y[::4, ::4]) is None


@pytest.mark.parametrize("strip", [8, 16, 24, 256])
@pytest.mark.parametrize("kind", ["rgb_wb", "rgb_neutral", "rgb_wb_only", "l_curve"])
def test_enhance_inplace_any_strip_height(m, monkeypatch, kind, strip):
    """Many strip boundaries on a small page: the result may not depend on ENHANCE_STRIP."""
    monkeypatch.setattr(m, "ENHANCE_STRIP", strip)
    src = make(kind, 150, 203, seed=strip)
    want, want_enh = old_enhance(m, src.copy())
    im = src.copy()
    got_enh = m._enhance_inplace(im)
    assert got_enh == want_enh
    if want_enh:
        assert not (np.asarray(im).astype(int) - np.asarray(want).astype(int)).any()
