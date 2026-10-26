"""ADF production path `normalize_pages`, on the geometry the smfp ADF really delivers
(review C07 — the old tests simulated the retired WSD 297×420 window with paper at the origin, which
the smfp path can never produce):

* window = run_scan's rule: width min(254, paper_w + 12) × requested height, centred on the feed-path
  centre ADF_CENTER_MM = 148 (driver default A4 window -l 43 -x 210 → paper at 43..253 mm);
  size full/auto uses the A4-portrait basis → 222×297 at -l 37;
* the paper is centred at 148 mm of the feed path, so it sits centred in the window (6 mm each side);
* paper has grain noise (std 9); outside the paper at the sides: device white (pure 255) or the dark
  backing plate (~40); below a short sheet: device white tail.

Content survival is checked with noise-free marker pixels at both paper edges (value exactly 100) and on
the footer line (value 101): their counts after normalize_pages must equal the counts before — nothing
cropped. (A sheet measured wider than A4 but snapped to A4 may lose its over-width at the edges.)"""
import numpy as np
import pytest
from PIL import Image, ImageDraw

DPI = 200
MARK = 100
FOOT = 101
STD = {"A4": (210, 297), "A5": (148, 210), "B5": (176, 250), "Letter": (216, 279), "Legal": (216, 356)}


def px(v_mm):
    return round(v_mm / 25.4 * DPI)


CENTER = 148.0                                      # feed-path centre (A4 at 43..253 mm)


def window(size):
    """(width_mm, height_mm, left_offset_mm) exactly as run_scan asks smfp for it."""
    aw, ah = (210.0, 297.0) if size in ("full", "auto") else STD[size]
    ww = min(254.0, aw + 12.0)
    return ww, ah, max(0, int(round(CENTER - ww / 2)))


def test_geometry_model_matches_app(m):
    # normalize_pages now serves only the WSD fallback (smfp scans the whole 297 mm feed path, see
    # test_adf_fullwidth.py); its anchor maths still uses these two constants
    assert m.ADF_CENTER_MM == CENTER and m.ADF_EDGE_MARGIN == 6.0
    assert window("full") == (222.0, 297.0, 37) and window("A5") == (160.0, 210.0, 68)


def adf_scan(path, paper_w, paper_h, size="full", outside="white", seed=0, blank=False):
    """outside: 'white' (device white, pure 255), 'dark' (backing plate ~40) or 'noisy' (white WITH
    paper-like grain — then the window edges look like paper even though the sheet fits)."""
    ww, wh, left = window(size)
    W, H = px(ww), px(wh)
    rng = np.random.default_rng(seed)
    a = np.full((H, W), 255.0, np.float32)
    x0 = CENTER - paper_w / 2 - left                 # paper centred in the feed path
    X0, X1, Y1 = max(0, px(x0)), min(W, px(x0 + paper_w)), min(H, px(paper_h))
    if outside == "dark":
        a[:Y1, :] = np.clip(40 + rng.normal(0, 4, (Y1, W)), 0, 255)
    elif outside == "noisy":
        a[:Y1, :] = np.clip(250 + rng.normal(0, 5, (Y1, W)), 0, 255)
    a[:Y1, X0:X1] = np.clip(253.6 + rng.normal(0, 9, (Y1, X1 - X0)), 0, 255)
    im = Image.fromarray(a.astype("uint8"), "L")
    d = ImageDraw.Draw(im)
    if not blank:
        for y in range(Y1 // 4, Y1 // 4 + min(400, Y1 // 2), 40):
            d.rectangle([X0 + (X1 - X0) // 8, y, X0 + (X1 - X0) * 7 // 8, y + 12], fill=40)
    edge = px(2)
    d.rectangle([X0 + 1, Y1 // 3, X0 + edge, Y1 // 3 + px(20)], fill=MARK)            # left paper edge
    d.rectangle([X1 - 1 - edge, Y1 // 2, X1 - 2, Y1 // 2 + px(20)], fill=MARK)        # right paper edge (stamp)
    d.rectangle([X0 + px(20), Y1 - px(4), X1 - px(20), Y1 - px(3)], fill=FOOT)        # footer line
    im.save(path)
    return str(path)


def marks(path):
    a = np.asarray(Image.open(path).convert("L"))
    return int((a == MARK).sum()), int((a == FOOT).sum())


def run(tmp_path, specs, **kw):
    ps = []
    for i, sp in enumerate(specs):
        opts = dict(kw, **(sp[2] if len(sp) > 2 else {}))
        ps.append(adf_scan(tmp_path / ("p%d.png" % i), sp[0], sp[1], seed=i, **opts))
    before = [marks(p) for p in ps]
    return ps, before


def mm_size(p):
    with Image.open(p) as im:
        assert im.info.get("dpi") is not None
        return tuple(round(v / DPI * 25.4) for v in im.size)


def check_nothing_cropped(ps, before, over_wide=False):
    """Footer must survive exactly (F06). Edge marks too — unless the sheet is wider than the A4 it was
    snapped to, then at most its over-width (< 1.5 mm total) may go, never the whole 2 mm mark."""
    for p, (edge, foot) in zip(ps, before):
        e2, f2 = marks(p)
        assert foot > 0 and f2 == foot, "footer cropped (%d → %d px)" % (foot, f2)
        if over_wide:
            assert e2 >= edge * 0.5, "paper edge cropped far beyond the over-width (%d → %d px)" % (edge, e2)
        else:
            assert e2 == edge, "content at the paper edge was cropped (%d → %d px)" % (edge, e2)


@pytest.mark.parametrize("outside", ["white", "dark"])
def test_a4_portrait_full_window(m, tmp_path, outside):
    ps, before = run(tmp_path, [(210, 297)], outside=outside)
    truncated = []
    assert m.normalize_pages(ps, DPI, truncated) == "A4"
    assert mm_size(ps[0]) == (210, 297) and truncated == []
    check_nothing_cropped(ps, before)


def test_three_a4_pages(m, tmp_path):
    ps, before = run(tmp_path, [(210, 297)] * 3)
    truncated = []
    assert m.normalize_pages(ps, DPI, truncated) == "A4"
    assert [mm_size(p) for p in ps] == [(210, 297)] * 3 and truncated == []
    check_nothing_cropped(ps, before)


@pytest.mark.parametrize("outside", ["white", "dark"])
def test_a4_landscape_in_full_window_is_flagged(m, tmp_path, outside):
    """297 mm wide paper in the 222 mm window: the sides are physically cut off → must be reported."""
    ps, before = run(tmp_path, [(297, 210)], outside=outside)
    truncated = []
    label = m.normalize_pages(ps, DPI, truncated)
    assert truncated == [ps[0]]                         # page PATHS: run_scan numbers them after blank removal
    assert label == "222×210mm"                       # the measured size, no fake standard label
    assert mm_size(ps[0]) == (222, 210)                # device-white tail trimmed, height kept


@pytest.mark.parametrize("outside,label,width", [("white", "175×210mm", 175), ("dark", "222×210mm", 222)])
def test_a5_in_full_window_kept_honestly(m, tmp_path, outside, label, width):
    """A5 fed with size=full: not snapped to a standard size it isn't; the whole sheet is kept."""
    ps, before = run(tmp_path, [(148, 210)], outside=outside)
    truncated = []
    assert m.normalize_pages(ps, DPI, truncated) == label
    assert mm_size(ps[0]) == (width, 210) and truncated == []
    check_nothing_cropped(ps, before)


def test_a5_with_size_a5_and_blank_page(m, tmp_path):
    ps, before = run(tmp_path, [(148, 210), (148, 210, {"blank": True})], size="A5")
    truncated = []
    assert m.normalize_pages(ps, DPI, truncated) == "A5"
    assert [mm_size(p) for p in ps] == [(148, 210)] * 2 and truncated == []
    check_nothing_cropped(ps, before)


def test_mixed_stack_per_page(m, tmp_path):
    """invoice (A6-ish landscape) + A4 + A4 fed sideways + B5 fed sideways (250 mm: fits the 254 mm
    feeder, wider than the 222 mm window) + A3 portrait, all in the full window."""
    ps, before = run(tmp_path, [(148, 105), (210, 297), (297, 210), (250, 176), (297, 420)])
    truncated = []
    label = m.normalize_pages(ps, DPI, truncated)
    # flagged = wider than the window AND not snappable to any standard size
    assert truncated == [ps[2], ps[3]]
    assert [mm_size(p) for p in ps] == [(175, 105), (210, 297), (222, 210), (222, 176), (210, 297)]
    assert label == "175×105mm and 4 different sizes"
    check_nothing_cropped(ps[:2], before[:2])


def test_a3_in_full_window_snaps_to_a4_without_warning(m, tmp_path):
    """Documents the trade-off of the 'wider AND unsnappable' rule: a 297 mm A3 sheet filling the 222 mm
    window snaps to A4 via widened_size and is NOT reported as cut (the /scan route already refuses ADF+A3;
    with size=full nothing can tell it apart from an A4 on a noisy-white background)."""
    ps, before = run(tmp_path, [(297, 420)])
    truncated = []
    assert m.normalize_pages(ps, DPI, truncated) == "A4"
    assert truncated == [] and mm_size(ps[0]) == (210, 297)


def test_a4_with_noisy_white_outside_is_not_flagged(m, tmp_path):
    """If the area outside the sheet is grainy white, both window edges look like paper ('wider'), but
    the page still snaps to A4 — that must not trigger the 'cut off' warning on every page."""
    ps, before = run(tmp_path, [(210, 297), (210, 297)], outside="noisy")
    truncated = []
    assert m.normalize_pages(ps, DPI, truncated) == "A4"
    assert truncated == [] and [mm_size(p) for p in ps] == [(210, 297)] * 2


def test_truncated_argument_is_optional(m, tmp_path):
    ps, _ = run(tmp_path, [(297, 210)])
    assert m.normalize_pages(ps, DPI) == "222×210mm"   # old call signature still works


# ---------------------------------------------------------------- dark-edged pages (C03)

@pytest.mark.parametrize("outside", ["white", "dark"])
def test_dark_cover_page_with_dark_border_loses_nothing(m, tmp_path, outside):
    # regression: the window used to be centred on 148.5 (-l 38) while the sheet is centred on 148, so
    # with no detectable edge (dark cover on the dark plate) the centred fallback crop ate 0.5 mm
    """A dark cover (paper ~70) with a darker 3 mm border column at both paper edges: the dark
    border must not be mistaken for the backing plate and trimmed away."""
    ww, wh, left = window("full")
    W, H = px(ww), px(wh)
    rng = np.random.default_rng(5)
    a = np.full((H, W), 255.0 if outside == "white" else 40.0, np.float32)
    if outside == "dark":
        a += rng.normal(0, 4, (H, W))
    x0 = CENTER - 105 - left
    X0, X1 = px(x0), px(x0 + 210)
    a[:, X0:X1] = np.clip(70 + rng.normal(0, 6, (H, X1 - X0)), 0, 255)
    b = px(3)
    a[:, X0:X0 + b] = MARK - 80                         # dark border columns (value 20)
    a[:, X1 - b:X1] = MARK - 80
    p = str(tmp_path / "cover.png")
    Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "L").save(p)
    border_before = int((np.asarray(Image.open(p)) == MARK - 80).sum())
    truncated = []
    label = m.normalize_pages([p], DPI, truncated)
    border_after = int((np.asarray(Image.open(p)) == MARK - 80).sum())
    assert border_after == border_before, "dark border trimmed (%d → %d px)" % (border_before, border_after)
    assert label == "A4" and mm_size(p) == (210, 297) and truncated == []


# ---------------------------------------------------------------- bottom-trimmed A4 (F06)

REVIEW_CASES = [(210.0, 283), (210.0, 284.5), (210.0, 285), (210.3, 285), (211, 284.5)]


@pytest.mark.parametrize("paper_w,paper_h", REVIEW_CASES)
def test_bottom_trimmed_a4_never_cropped_to_letter(m, tmp_path, paper_w, paper_h):
    """An A4 whose last few mm were lost in the device-white tail measures ~285 mm: it must never be
    'snapped' to Letter (279 mm) — that crops the footer."""
    ps, before = run(tmp_path, [(paper_w, paper_h)])
    label = m.normalize_pages(ps, DPI, [])
    assert "Letter" not in label
    w, h = mm_size(ps[0])
    assert h >= round(paper_h) - 1, "page shorter than the measured sheet (%s)" % label
    check_nothing_cropped(ps, before, over_wide=paper_w > 210.2)


@pytest.mark.parametrize("paper_w,paper_h", REVIEW_CASES)
def test_bottom_trimmed_a4_is_padded_back_to_a4(m, tmp_path, paper_w, paper_h):
    # regression: measured sizes are whole pixels (210.0 mm → 210.06 mm, 283 mm → 282.96 mm), which
    # snap_up_size used to reject on either side of its bounds, leaving a non-standard 210×28x mm page
    ps, before = run(tmp_path, [(paper_w, paper_h)])
    assert m.normalize_pages(ps, DPI, []) == "A4"
    assert mm_size(ps[0]) == (210, 297)
    check_nothing_cropped(ps, before, over_wide=paper_w > 210.2)


# ---------------------------------------------------------------- legacy: WSD fallback geometry

def test_legacy_wsd_fallback_window_equals_paper(m, tmp_path):
    """When smfp can't be opened run_scan falls back to WSD, whose ADF window is NOT widened
    (-l 0 -x <paper width>): the sheet fills the window edge to edge. Size must still come out right."""
    W, H = px(210), px(297)
    rng = np.random.default_rng(3)
    a = np.clip(253.6 + rng.normal(0, 9, (H, W)), 0, 255)
    p = str(tmp_path / "wsd.png")
    Image.fromarray(a.astype("uint8"), "L").save(p)
    assert m.normalize_pages([p], DPI) == "A4" and mm_size(p) == (210, 297)


def test_legacy_wsd_fallback_passes_no_truncation_list(m, tmp_path):
    """normalize_pages can't know the window wasn't widened, so on the WSD fallback run_scan passes
    truncated=None (see test_run_scan_e2e::test_wsd_fallback_adf_a4_is_not_flagged_truncated).
    With None it must still normalise the page and never fail."""
    W, H = px(210), px(297)
    rng = np.random.default_rng(3)
    p = str(tmp_path / "wsd.png")
    Image.fromarray(np.clip(253.6 + rng.normal(0, 9, (H, W)), 0, 255).astype("uint8"), "L").save(p)
    assert m.normalize_pages([p], DPI, None) == "A4" and mm_size(p) == (210, 297)
