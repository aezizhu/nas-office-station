"""Last-resort page orientation by comparing recognised text in the four rotations.

Only used for a page whose Tesseract OSD was not confident and that got no direction
from the other pages of the same batch (scan_app.decide_orientations). It never
overrides a weak-but-stated OSD direction, and it says "don't know" (None) rather
than guessing. Recognised text stays in memory and is never logged.

Why the rules (measured 2026-10-19 on real and synthetic pages):
* English-only OCR turned an upright Chinese page sideways because its only Latin
  text was a sideways rubber stamp -> Chinese characters count as evidence too, and
  a disagreement with OSD's own weak vote means "don't know".
* csv.DictReader with default quoting swallowed every row after a token that starts
  with a double quote -> tab-split with QUOTE_NONE.
"""
import csv
import io
import math
import os
import re
import subprocess
import tempfile
import time

from PIL import Image

PREVIEW_SIDE = 2000        # OCR preview long side; sparse pages are decided by a few lines
CALL_TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
MIN_CONF = 80.0            # per-token confidence to count as evidence
MIN_SCORE = 8.0            # e.g. 8 Chinese characters, or 16 Latin letters
HINT_CONF = 1.0            # an OSD vote at least this confident must not be contradicted

_CJK = re.compile(r"[㐀-䶿一-鿿]")
_LATIN = re.compile(r"[A-Za-z]{3,}")
_langs_cache = {}


def available_langs():
    """`chi_sim+eng` when the Chinese model is installed (the station container has it), else `eng`."""
    if "v" not in _langs_cache:
        try:
            out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True,
                                 timeout=15).stdout
            have = {line.strip() for line in out.splitlines()}
            _langs_cache["v"] = "chi_sim+eng" if "chi_sim" in have else "eng"
        except (OSError, subprocess.SubprocessError):
            _langs_cache["v"] = "eng"
    return _langs_cache["v"]


def _evidence(tsv):
    """Score = confident Chinese characters + half the letters of confident Latin words."""
    reader = csv.reader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if not header or not {"level", "conf", "text"}.issubset(header):
        raise ValueError("Invalid OCR output")
    il, ic, it = header.index("level"), header.index("conf"), header.index("text")
    score = 0.0
    for row in reader:
        if len(row) <= max(il, ic, it) or row[il] != "5":
            continue
        try:
            conf = float(row[ic])
        except ValueError:
            continue
        if not math.isfinite(conf) or conf < MIN_CONF:
            continue
        text = row[it].strip()
        cjk = len(_CJK.findall(text))
        latin = sum(len(w) for w in _LATIN.findall(text))
        score += (cjk + latin / 2.0) * conf / 100.0
    return score


def _preview(im):
    longest = max(im.size)
    if longest > PREVIEW_SIDE:
        scale = PREVIEW_SIDE / longest
        size = tuple(max(1, round(v * scale)) for v in im.size)
        return im.resize(size, Image.Resampling.LANCZOS, reducing_gap=3).convert("L")
    return im.convert("L")


def ocr_vote(im, osd_hint=None, langs=None):
    """Return (rotation or None, detail). rotation = clockwise degrees to turn the page upright.

    osd_hint = (degrees, confidence) from Tesseract OSD; if its confidence is at least
    HINT_CONF and the OCR winner disagrees, the answer is None (do not guess).
    detail is a short reason for the event log (numbers only, never recognised text).
    """
    langs = langs or available_langs()
    deadline = time.monotonic() + TOTAL_TIMEOUT
    scores = {}
    try:
        with tempfile.TemporaryDirectory(prefix="scan-orientation-") as directory:
            path = os.path.join(directory, "candidate.png")
            with _preview(im) as base:
                for rotation in (0, 90, 180, 270):
                    remaining = deadline - time.monotonic()
                    if remaining <= 1:
                        return None, "ocr time budget used up"
                    candidate = base.rotate(-rotation, expand=True)
                    try:
                        candidate.save(path, compress_level=1)
                    finally:
                        candidate.close()
                    proc = subprocess.run(
                        ["tesseract", path, "stdout", "--psm", "11", "-l", langs, "tsv"],
                        capture_output=True, text=True, timeout=min(CALL_TIMEOUT, remaining),
                        env={**os.environ, "OMP_THREAD_LIMIT": "1"})
                    if proc.returncode:
                        return None, "tesseract exit %d" % proc.returncode
                    scores[rotation] = _evidence(proc.stdout)
    except subprocess.TimeoutExpired:
        return None, "ocr timeout"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, "ocr failed: %s" % type(exc).__name__
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, s1), (_, s2) = ranked[0], ranked[1]
    detail = "ocr %s best=%d score=%.1f runner=%.1f" % (langs, best, s1, s2)
    if s1 < MIN_SCORE or s1 < 2 * s2 + 2:
        return None, detail + " (not clear)"
    if osd_hint and osd_hint[1] >= HINT_CONF and osd_hint[0] % 360 != best:
        return None, detail + " (contradicts osd %d/%.1f)" % (osd_hint[0], osd_hint[1])
    return best, detail
