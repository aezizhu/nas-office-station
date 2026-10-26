"""/print option set and error handling, without poppler: the print job dir (doc.pdf + pages file)
is created directly, lp/lpstat are recording fakes, printer_online is monkeypatched."""
import os
import uuid

import pytest

import station_support as sup


def make_doc(m, npages=12):
    token = uuid.uuid4().hex
    d = os.path.join(m.JOBS, token)
    os.makedirs(d)
    with open(os.path.join(d, "doc.pdf"), "wb") as f:
        f.write(sup.pdf_bytes([sup.A4_PT] * min(npages, 2)))   # lp is fake: content doesn't matter
    with open(os.path.join(d, "pages"), "w") as f:
        f.write(str(npages))
    return token


@pytest.fixture()
def cups(m, fake_cups, monkeypatch):
    fake_cups.online = [(True, "1.2.3.4")]
    fake_cups.probes = 0

    def online():
        fake_cups.probes += 1
        return fake_cups.online[0]
    monkeypatch.setattr(m, "printer_online", online)
    fake_cups.watched = []
    monkeypatch.setattr(m, "watch_print_job", lambda *a: fake_cups.watched.append(a))
    return fake_cups


def post(client, token, **form):
    data = {"token": token, "paper": "A4", "sides": "None", "copies": "1", "pages": ""}
    data.update(form)
    for k in [k for k, v in data.items() if v is None]:
        del data[k]
    r = client.post("/print", data=data)
    assert r.is_json, r.data[:200]
    return r.status_code, r.get_json()


def test_default_scaling_is_auto_and_no_legacy_options(m, client, cups):
    t = make_doc(m)
    code, j = post(client, t, scale=None)
    assert code == 200 and j["ok"] is True
    (argv,) = cups.lp_calls()
    o = sup.lp_options(argv)
    assert "print-scaling=auto" in o
    assert not any("fit-to-page" in a for a in argv)
    assert not any(x.startswith("Resolution") for x in o), "PPD has no Resolution option"
    assert argv[:4] == ["-d", m.QUEUE, "-n", "1"]
    assert "collate=true" not in o and not any(x.startswith("page-ranges") for x in o)


def test_scale_fit(m, client, cups):
    code, j = post(client, make_doc(m), scale="fit")
    assert code == 200
    o = sup.lp_options(cups.lp_calls()[0])
    assert "print-scaling=fit" in o and "print-scaling=auto" not in o
    assert not any("fit-to-page" in x for x in o)


@pytest.mark.parametrize("scale", ["fit-to-page", "none", "FIT", "", "auto "])
def test_bad_scale_400(m, client, cups, scale):
    code, j = post(client, make_doc(m), scale=scale)
    assert code == 400 and j["error"] == "Invalid print settings. Refresh the page and retry"
    assert cups.lp_calls() == []


@pytest.mark.parametrize("field,value", [("paper", "A0"), ("sides", "Duplex"), ("paper", "")])
def test_bad_paper_or_sides_400(m, client, cups, field, value):
    code, j = post(client, make_doc(m), **{field: value})
    assert code == 400 and cups.lp_calls() == []


@pytest.mark.parametrize("copies", ["0", "201", "1.5", "-1", "abc", "²", "２", "1e2", "0x2", "3 4"])
def test_bad_copies_400(m, client, cups, copies):
    code, j = post(client, make_doc(m), copies=copies)
    assert code == 400 and j["error"] == "Copies must be an integer from 1 to 200"
    assert cups.lp_calls() == []


@pytest.mark.parametrize("copies,n", [("1", "1"), ("", "1"), (" 7 ", "7"), ("007", "7"), ("200", "200")])
def test_copies_normalised(m, client, cups, copies, n):
    code, j = post(client, make_doc(m), copies=copies)
    assert code == 200
    argv = cups.lp_calls()[0]
    assert argv[argv.index("-n") + 1] == n
    assert ("collate=true" in sup.lp_options(argv)) == (n != "1")


@pytest.mark.parametrize("pages,sides,copies,total,sheets,ranges", [
    ("", "None", "1", 12, 12, None),
    ("1-3，5", "DuplexNoTumble", "2", 12, 4, "1-3,5"),
    ("1-5", "DuplexTumble", "3", 12, 9, "1-5"),
    ("1-5", "None", "2", 12, 10, "1-5"),
    ("7", "DuplexNoTumble", "1", 12, 1, "7"),
    ("", "DuplexNoTumble", "1", 7, 4, None),
    ("all", "DuplexTumble", "2", 3, 4, None),
    ("１-３", "None", "1", 5, 3, "1-3"),
])
def test_sheets_and_ranges(m, client, cups, pages, sides, copies, total, sheets, ranges):
    code, j = post(client, make_doc(m, total), pages=pages, sides=sides, copies=copies)
    assert code == 200, j
    assert j["sheets"] == sheets
    assert cups.watched[0][3] == sheets                     # the watcher's deadline scales with it
    o = sup.lp_options(cups.lp_calls()[0])
    got = [x.split("=", 1)[1] for x in o if x.startswith("page-ranges=")]
    assert got == ([ranges] if ranges else [])
    assert "sides=%s" % m.SIDES_IPP[sides] in o and "Duplex=%s" % sides in o


def test_long_every_other_page_selection_reaches_lp(m, client, cups):
    odd = list(range(1, 201, 2))
    spec = m.compress_ranges(odd)
    code, j = post(client, make_doc(m, 200), pages=spec, sides="DuplexNoTumble")
    assert code == 200, j
    assert "page-ranges=" + spec in sup.lp_options(cups.lp_calls()[0])
    assert j["sheets"] == 50 and "100 selected" in j["desc"]


def test_desc_in_response_and_job_message(m, client, cups):
    code, j = post(client, make_doc(m), paper="A3", sides="DuplexTumble", copies="3", pages="2,4-6")
    assert code == 200
    assert j["desc"] == "3 copies / A3 / Double-sided, short edge (SEF) / Pages 2,4-6 (12 pages total, 4 selected)"
    v = m.job_view(j["job"], "print")
    assert v == {"state": "running", "msg": "Sent to the printer (%s); printing..." % j["desc"], "file": ""}
    s = client.get("/status", query_string={"print": j["job"]}).get_json()
    assert s["print"] == v


def test_offline_printer_503_and_nothing_sent(m, client, cups):
    cups.online[0] = (False, "1.2.3.4")
    code, j = post(client, make_doc(m))
    assert code == 503 and "Cannot reach the printer (1.2.3.4)" in j["error"]
    assert cups.lp_calls() == [] and cups.watched == []
    assert m._pstate["online"] is False                    # /print refreshes the page-header cache


def test_disabled_queue_503_and_nothing_sent(m, client, cups):
    cups.env(FAKE_LPSTAT_P="printer M437 disabled since Tue Sep 23 10:00:00 2026 -\n\treason unknown")
    code, j = post(client, make_doc(m))
    assert code == 503 and "disabled" in j["error"]
    assert cups.lp_calls() == [] and cups.watched == []
    assert any(c.startswith("-p") for c in cups.lpstat_calls())


def test_lp_failure_502_plain_message(m, client, cups):
    cups.env(FAKE_LP_FAIL="1")
    code, j = post(client, make_doc(m))
    assert code == 502 and j["error"].startswith("Print submission failed: ")
    assert cups.watched == [] and m._jobs == {}


def test_lp_without_job_id_is_unknown_not_success(m, client, cups):
    cups.env(FAKE_LP_NOID="1")
    code, j = post(client, make_doc(m))
    assert code == 200 and j["ok"] is True
    v = m.job_view(j["job"], "print")
    assert v["state"] == "unknown" and "no job ID" in v["msg"] and j["desc"] in v["msg"]
    assert cups.watched == []                              # nothing to watch


def test_bad_and_expired_tokens(m, client, cups):
    code, j = post(client, "../../etc")
    assert code == 400 and "no longer available" in j["error"]
    code, j = post(client, "0" * 32)
    assert code == 410 and "expired" in j["error"]
    assert cups.lp_calls() == []


def test_print_touches_job_dir(m, client, cups):
    t = make_doc(m)
    d = os.path.join(m.JOBS, t)
    os.utime(d, (1, 1))
    post(client, t)
    assert os.path.getmtime(d) > 1000                     # cleaner counts from last use
