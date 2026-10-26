"""OSD input size vs reliability (don't shrink the OSD thumbnail again: at 1000 px a real sideways
page was silently NOT detected). Uses the production _osd_once on the real 2339×3307 sample
SCAN_SAMPLES/p-001.png when present; the cap guard always runs."""
import os
import shutil

import pytest
from PIL import Image

SAMPLE = os.path.join(os.environ.get("SCAN_SAMPLES", ""), "p-001.png")


def test_osd_cap_not_shrunk_below_reliable_size(m):
    assert m.OSD_CAP >= 1800, "OSD thumbnails below 1800 px were measured to miss sideways pages"


@pytest.mark.skipif(not os.environ.get("SCAN_SAMPLES") or not os.path.exists(SAMPLE), reason="set SCAN_SAMPLES to an approved directory containing p-001.png")
@pytest.mark.parametrize("cap", [1800, 2200, 2600, None])
def test_real_page_detected_at_reliable_sizes(m, cap):
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    im = Image.open(SAMPLE)
    im.load()
    deg, conf = m._osd_once(im, cap)
    assert deg == 90 and conf >= 3.0, "cap=%s → %d° conf %.2f" % (cap, deg, conf)


@pytest.mark.skipif(not os.environ.get("SCAN_SAMPLES") or not os.path.exists(SAMPLE), reason="set SCAN_SAMPLES to an approved directory containing p-001.png")
def test_real_page_via_osd_degrees(m):
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    im = Image.open(SAMPLE)
    im.load()
    assert m._osd_degrees(im) == 90
