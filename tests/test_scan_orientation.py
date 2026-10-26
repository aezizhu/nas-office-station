"""Optional page rotation for the office scan station (off by default since 2026-10-19).

decide_orientations (scan_app) runs only when the user asks for rotation: blank pages are skipped, sure OSD answers
are used, an unsure page follows the sure pages of the same shape in the batch, and only then does
scan_orientation.ocr_vote compare recognised text. ocr_vote never contradicts a weak-but-stated OSD direction:
an English-only vote once turned an upright Chinese page sideways because of a sideways English stamp.
"""
import importlib.util
from pathlib import Path
import subprocess

import pytest
from PIL import Image

STATION = Path(__file__).resolve().parents[1] / "app"


def _load(name, filename, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.syspath_prepend(str(STATION))
    spec = importlib.util.spec_from_file_location(name, STATION / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def orientation(tmp_path, monkeypatch):
    return _load("scan_orientation_under_test", "scan_orientation.py", tmp_path, monkeypatch)


@pytest.fixture
def station(tmp_path, monkeypatch):
    return _load("orientation_station", "scan_app.py", tmp_path, monkeypatch)


HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def tsv(*words):
    return "\n".join([HEADER] + ["5\t1\t1\t1\t1\t%d\t0\t0\t10\t10\t%s\t%s" % (i, c, t) for i, (c, t) in enumerate(words)])


def test_a_token_starting_with_a_quote_does_not_hide_the_rest(orientation):
    # csv's default quoting swallowed every row after a token like '"Agreement' (one field to the next quote)
    assert orientation._evidence(tsv((95, '"Agreement'), (95, "between"), (95, "parties"))) == pytest.approx(
        (9 + 7 + 7) / 2 * 0.95, rel=0.01)


def test_chinese_characters_count_as_evidence(orientation):
    assert orientation._evidence(tsv((90, "承诺函"), (90, "签字"))) == pytest.approx(5 * 0.9)
    assert orientation._evidence(tsv((50, "承诺函"))) == 0                     # low confidence does not count


def _fake_tesseract(orientation, monkeypatch, per_rotation):
    """subprocess.run stand-in: the n-th call answers for rotation (0, 90, 180, 270)[n]."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, per_rotation[len(calls) - 1], "")
    monkeypatch.setattr(orientation.subprocess, "run", run)
    return calls


def test_clear_text_evidence_decides(orientation, monkeypatch):
    _fake_tesseract(orientation, monkeypatch, [tsv(), tsv((95, "承诺方在此承诺自收到价款之日起")), tsv(), tsv()])
    rot, detail = orientation.ocr_vote(Image.new("L", (300, 400), 255), osd_hint=(90, 0.4), langs="chi_sim+eng")
    assert rot == 90, detail


def test_never_contradicts_a_stated_osd_direction(orientation, monkeypatch):
    # upright Chinese page whose only confident words are a sideways English stamp: OSD weakly says 0
    _fake_tesseract(orientation, monkeypatch, [tsv(), tsv((95, "RECEIVED"), (95, "CERTIFIED"), (95, "COPY")), tsv(), tsv()])
    rot, detail = orientation.ocr_vote(Image.new("L", (300, 400), 255), osd_hint=(0, 2.3), langs="eng")
    assert rot is None and "contradicts osd" in detail


def test_too_little_evidence_is_unknown_not_a_guess(orientation, monkeypatch):
    _fake_tesseract(orientation, monkeypatch, [tsv((95, "Zhu")), tsv(), tsv(), tsv()])
    rot, detail = orientation.ocr_vote(Image.new("L", (300, 400), 255), langs="eng")
    assert rot is None and "not clear" in detail


def _pages(tmp_path, specs):
    paths = []
    for i, (w, h, ink) in enumerate(specs, 1):
        im = Image.new("RGB", (w, h), "white")
        if ink:
            for y in range(40, h - 40, 30):
                im.paste((40, 40, 40), (30, y, w - 30, y + 4))
        p = tmp_path / ("p%03d.png" % i)
        im.save(p, dpi=(100, 100))
        paths.append(str(p))
    return paths


def test_unsure_page_follows_sure_pages_of_the_same_shape(station, tmp_path, monkeypatch):
    """2026-10-19 contract: page 1 sure (rotate 270), sparse signature page 2 unsure -> also 270."""
    pages = _pages(tmp_path, [(400, 300, True), (400, 300, True)])
    answers = iter([(270, 13.7), (90, 1.2)])
    monkeypatch.setattr(station, "_osd_guess", lambda im, *a: next(answers))
    monkeypatch.setattr(station, "ocr_vote", lambda *a, **k: pytest.fail("batch direction should decide"))
    assert station.decide_orientations(pages) == [(270, "", ""), (270, "Rotated to match other pages in this batch", "")]


def test_blank_pages_are_not_examined(station, tmp_path, monkeypatch):
    pages = _pages(tmp_path, [(300, 400, False)])
    monkeypatch.setattr(station, "_osd_guess", lambda im, *a: pytest.fail("blank page must not run OSD"))
    assert station.decide_orientations(pages) == [(0, "", "")]


def test_a_page_sure_enough_of_its_own_keeps_out_of_the_batch_direction(station, tmp_path, monkeypatch):
    pages = _pages(tmp_path, [(300, 400, True), (300, 400, True)])
    answers = iter([(0, 15.0), (180, 2.5)])
    monkeypatch.setattr(station, "_osd_guess", lambda im, *a: next(answers))
    monkeypatch.setattr(station, "ocr_vote", lambda im, hint: (None, "not clear"))
    assert station.decide_orientations(pages) == [(0, "", ""), (0, "Orientation uncertain; kept the original", "")]


def test_osd_failure_is_reported_not_swallowed(station, tmp_path, monkeypatch):
    pages = _pages(tmp_path, [(300, 400, True)])

    def boom(im, *a):
        raise RuntimeError("tesseract missing")
    monkeypatch.setattr(station, "_osd_guess", boom)
    (rot, note, err), = station.decide_orientations(pages)
    assert rot == 0 and "tesseract missing" in err
