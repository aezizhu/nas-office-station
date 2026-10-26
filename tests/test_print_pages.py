"""Print page: per-page preview + page selection + duplex/copies options, through the production
routes /upload → /preview → /print. lp/lpstat are recording fakes on PATH — they prove what the
station SENDS to CUPS, not that the paper really came out duplex (check that at the printer)."""
import io
import os
import shutil
import subprocess

import pytest

import station_support as sup

NPAGES = 12


@pytest.fixture()
def token(client, poppler):
    r = client.post("/upload", data={"file": (io.BytesIO(sup.pdf_bytes([sup.A4_PT] * NPAGES)), "doc.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["pages"] == NPAGES and j["docsize"] == "A4" and j["name"] == "doc.pdf"
    return j["token"]


@pytest.fixture()
def printer(m, fake_cups, monkeypatch):
    """Online printer + recording lp; the job watcher is replaced by a recorder."""
    monkeypatch.setattr(m, "printer_online", lambda: (True, "1.2.3.4"))
    watched = []
    monkeypatch.setattr(m, "watch_print_job", lambda *a: watched.append(a))
    fake_cups.watched = watched
    return fake_cups


# ---------- page-range parsing

@pytest.mark.parametrize("spec,want", [
    ("", list(range(1, 13))), ("  ", list(range(1, 13))), ("全部", list(range(1, 13))),
    ("all", list(range(1, 13))), ("ALL", list(range(1, 13))),
    ("3", [3]), ("1-3,5", [1, 2, 3, 5]), ("1-3，5、8～10", [1, 2, 3, 5, 8, 9, 10]),
    ("第2页到第4页", [2, 3, 4]), ("5-3", [3, 4, 5]), ("1 3 5", [1, 3, 5]),
    ("2 - 4 , 4-6", [2, 3, 4, 5, 6]), ("12,1", [1, 12]),
    ("１-３，５", [1, 2, 3, 5]), ("１２", [12]), ("2至4；6", [2, 3, 4, 6]), ("1–2", [1, 2]),
])
def test_parse_ok(m, spec, want):
    assert m.parse_page_ranges(spec, 12) == want


@pytest.mark.parametrize("spec", ["0", "13", "1-13", "abc", "1-2-3", "1.5", "-3", "3-", "1,,x", "１３"])
def test_parse_rejects(m, spec):
    with pytest.raises(ValueError):
        m.parse_page_ranges(spec, 12)


def test_long_legitimate_selection_is_accepted(m):
    """Every other page of a 200-page document in canonical form is 344 chars — must not hit the cap
    (it used to be 300 characters)."""
    odd = list(range(1, 201, 2))
    spec = m.compress_ranges(odd)
    assert 300 < len(spec) < 4000
    assert m.parse_page_ranges(spec, 200) == odd
    spaced = "， ".join(str(p) for p in odd)            # the same with Chinese commas and spaces
    assert m.parse_page_ranges(spaced, 200) == odd


def test_length_cap_is_4000_characters(m):
    ok = "1," * 2000                                     # exactly 4000 characters
    assert len(ok) == 4000 and m.parse_page_ranges(ok, 12) == [1]
    with pytest.raises(ValueError, match="too long"):
        m.parse_page_ranges(ok + "1", 12)


def test_compress_roundtrip(m):
    assert m.compress_ranges([1, 2, 3, 5, 8, 9, 10]) == "1-3,5,8-10"
    assert m.compress_ranges([7]) == "7"
    assert m.compress_ranges([]) == ""
    assert m.parse_page_ranges(m.compress_ranges([2, 4, 5, 6, 12]), 12) == [2, 4, 5, 6, 12]


# ---------- preview: every page exists and preview k really is page k

def test_every_page_previewable_and_correct(m, client, token, tmp_path):
    pdf = os.path.join(m.JOBS, token, "doc.pdf")
    seen = set()
    for k in (1, 8, 9, NPAGES):                         # 9 and 12 were invisible in the 8-page era
        r = client.get("/preview/%s?p=%d" % (token, k - 1))
        assert r.status_code == 200 and r.data[:8] == b"\x89PNG\r\n\x1a\n"
        ref = str(tmp_path / ("ref%d" % k))
        subprocess.run(["pdftoppm", "-png", "-r", "60", "-f", str(k), "-l", str(k), "-singlefile", pdf, ref],
                       check=True)
        assert r.data == open(ref + ".png", "rb").read(), "preview %d is not page %d" % (k, k)
        seen.add(r.data)
    assert len(seen) == 4
    assert client.get("/preview/%s?p=%d" % (token, NPAGES)).status_code == 404   # out of range
    assert client.get("/preview/%s?p=-1" % token).status_code == 404
    assert client.get("/preview/nothex").status_code == 400
    assert client.get("/preview/%s" % ("0" * 32)).status_code == 404


def test_hi_res_preview_120dpi_cached_separately(m, client, token, tmp_path, monkeypatch):
    d = os.path.join(m.JOBS, token)
    lo = client.get("/preview/%s?p=2" % token)
    hi = client.get("/preview/%s?p=2&hi=1" % token)
    assert lo.status_code == hi.status_code == 200
    assert os.path.exists(os.path.join(d, "pg-3.png")) and os.path.exists(os.path.join(d, "pg-3-hi.png"))
    ref = str(tmp_path / "ref")
    subprocess.run(["pdftoppm", "-png", "-r", "120", "-f", "3", "-l", "3", "-singlefile",
                    os.path.join(d, "doc.pdf"), ref], check=True)
    assert hi.data == open(ref + ".png", "rb").read()           # page 3 at 120 dpi
    assert hi.data != lo.data and len(hi.data) > len(lo.data)
    calls = []

    def hook(cmd, real_run, *a, **kw):
        calls.append(cmd[0])
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(m, "subprocess", sup.SubprocessProxy(hook))
    again = client.get("/preview/%s?p=2&hi=1" % token)
    assert again.data == hi.data and "pdftoppm" not in calls     # served from the pg-3-hi.png cache


def _vanish_after_render(m, monkeypatch):
    real = m.render_preview

    def render_then_vanish(d, n, hi=False):
        out = real(d, n, hi)
        shutil.rmtree(d)
        return out
    monkeypatch.setattr(m, "render_preview", render_then_vanish)


def _vanish_before_render(m, monkeypatch):
    real = m.render_preview

    def vanish_then_render(d, n, hi=False):
        shutil.rmtree(d)
        return real(d, n, hi)
    monkeypatch.setattr(m, "render_preview", vanish_then_render)


def _vanish_before_page_count(m, monkeypatch):
    real = m.job_page_count

    def vanish_then_count(d):
        shutil.rmtree(d)
        return real(d)
    monkeypatch.setattr(m, "job_page_count", vanish_then_count)


@pytest.mark.parametrize("vanish", [_vanish_after_render, _vanish_before_render, _vanish_before_page_count],
                         ids=["after-render", "before-render", "before-page-count"])
def test_preview_404_when_job_dir_vanishes_mid_request(m, client, token, monkeypatch, vanish):
    # regression: before-render / before-page-count used to surface as CalledProcessError/RuntimeError → 500
    vanish(m, monkeypatch)
    r = client.get("/preview/%s?p=4" % token)
    assert r.status_code == 404


def test_preview_real_failure_is_still_500(m, client, token, monkeypatch):
    """Only a vanished job is a 404 — a genuine render failure must stay visible as 500 and be logged."""
    def broken(d, n, hi=False):
        raise RuntimeError("pdftoppm crashed")
    monkeypatch.setattr(m, "render_preview", broken)
    r = client.get("/preview/%s?p=1" % token)
    assert r.status_code == 500
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tpreview-fail\t" in f.read()


# ---------- /print: options as sent to CUPS

def test_print_all_pages_has_no_range(m, client, token, printer):
    r = client.post("/print", data={"token": token, "paper": "A4", "sides": "None", "copies": "1", "pages": ""})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True, j
    (argv,) = printer.lp_calls()
    o = sup.lp_options(argv)
    assert not any(x.startswith("page-ranges") for x in o)
    assert "sides=one-sided" in o and "Duplex=None" in o and "collate=true" not in o
    assert "print-scaling=auto" in o and not any("fit-to-page" in x for x in argv)
    assert j["desc"] == "1 copies / A4 / Single-sided / All 12 pages" and j["sheets"] == 12


def test_print_selected_pages_duplex_copies(m, client, token, printer):
    r = client.post("/print", data={"token": token, "paper": "A4", "sides": "DuplexNoTumble",
                                    "copies": "2", "pages": "1-3，5"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True, j
    (argv,) = printer.lp_calls()
    o = sup.lp_options(argv)
    assert "page-ranges=1-3,5" in o
    assert "Duplex=DuplexNoTumble" in o and "sides=two-sided-long-edge" in o
    assert "collate=true" in o                            # multi-copy duplex must collate
    assert argv[argv.index("-n") + 1] == "2" and argv[-1].endswith("doc.pdf")
    assert j["desc"] == "2 copies / A4 / Double-sided, long edge (LEF) / Pages 1-3,5 (12 pages total, 4 selected)"
    assert j["sheets"] == 4                               # ceil(4/2) sheets × 2 copies
    v = m.job_view(j["job"], "print")
    assert v["state"] == "running" and v["msg"] == "Sent to the printer (%s); printing..." % j["desc"]
    assert printer.watched == [(j["job"], "M437-999", j["desc"], 4)]
    with open(m.EVENTS, encoding="utf-8") as f:
        log = [line for line in f.read().splitlines() if "\tprint-submit\t" in line][-1]
    assert "page-ranges=1-3,5" in log and "sides=two-sided-long-edge" in log and "M437-999" in log


@pytest.mark.parametrize("pages", ["13", "0", "2-99", "x"])
def test_print_bad_pages_never_reaches_printer(m, client, token, printer, pages):
    r = client.post("/print", data={"token": token, "paper": "A4", "sides": "None", "copies": "1", "pages": pages})
    assert r.status_code == 400 and "error" in r.get_json()
    assert printer.lp_calls() == [] and printer.watched == []
