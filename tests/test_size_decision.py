"""Size-snapping decision on page sizes users really produced: an A4 whose bottom margin was trimmed
must be restored to A4 (never cropped to Letter); a non-standard receipt keeps its measured size.

The decision chain must be the one normalize_pages uses, with the same parameters (README lesson #3:
a test that hard-coded slack=20 while production used 14 proved nothing)."""
import inspect
import re

import pytest

CASES = [
    (209.7, 284.5, "A4", "用户第4页:A4宽但短12mm(白边被裁)"),
    (258.6, 134.1, None, "用户第1页:长条收据(非标)"),
    (225.6, 139.1, None, "用户第2页:非标"),
    (210.0, 297.0, "A4", "标准A4"),
    (260.2, 209.3, None, "真实ADF页 260x209(非标)"),
    (297.0, 210.0, "A4", "A4横放"),
    (148.0, 210.0, "A5", "A5"),
    (205.0, 290.0, "A4", "略小于A4(白边被裁)"),
]

# F06 review cases: the device-white tail was trimmed, so the measured height is only a lower bound.
BOTTOM_TRIMMED = [
    (210.0, 283.0, "A4"), (210.0, 284.5, "A4"), (210.0, 285.0, "A4"),
    (210.3, 285.0, "A4"),       # pixel-column rounding: widths up to 1.5 mm over still pad back
    (211.0, 284.5, "A4"),
]


def decide(m, w, h, bottom_trimmed=False):
    """Mirror of normalize_pages: nearest(tol=6) → widened → snap_up(slack=14); when the bottom was
    trimmed, candidates shorter than the measured height - 0.5 mm are skipped."""
    cands = (m.nearest_size(w, h, tol=6.0), m.widened_size(w, h), m.snap_up_size(w, h, slack=14.0))
    return next((c for c in cands if c and (not bottom_trimmed or c[2] >= h - 0.5)), None)


def test_chain_matches_production(m):
    src = re.sub(r"\s+", " ", inspect.getsource(m.normalize_pages))
    assert ("cands = (nearest_size(w_mm, h_mm, tol=6.0), widened_size(w_mm, h_mm), "
            "snap_up_size(w_mm, h_mm, slack=14.0))") in src, \
        "normalize_pages changed its size decision — update decide() in this test to match"
    assert "next((c for c in cands if c and (not bottom_trimmed or c[2] >= h_mm - 0.5)), None)" in src


@pytest.mark.parametrize("w,h,expect,note", CASES, ids=[c[3] for c in CASES])
def test_size_decision(m, w, h, expect, note):
    r = decide(m, w, h)
    assert (r[0] if r else None) == expect, "%s: %.1f×%.1f → %r" % (note, w, h, r)


@pytest.mark.parametrize("w,h,expect", [
    (210.06, 285.0, ("A4", 210, 297)),        # one pixel column over A4 (210 mm = 1653.5 px @200dpi)
    (211.5, 296.0, ("A4", 210, 297)),         # -1.5 mm is the tolerance bound (inclusive)
    (211.6, 285.0, None),                     # beyond it: not A4 (and never Letter: that would crop)
    (205.0, 298.5, ("A4", 210, 297)),
    (205.0, 298.6, None),
])
def test_snap_up_tolerates_pixel_rounding(m, w, h, expect):
    assert m.snap_up_size(w, h) == expect


@pytest.mark.parametrize("w,h,expect", BOTTOM_TRIMMED)
def test_bottom_trimmed_never_snaps_shorter(m, w, h, expect):
    r = decide(m, w, h, bottom_trimmed=True)
    assert (r[0] if r else None) == expect
    assert r is None or r[2] >= h - 0.5                 # only padding, never cropping the footer
    unguarded = decide(m, w, h, bottom_trimmed=False)
    assert unguarded and unguarded[0] == "Letter"      # the F06 trap the guard exists for
