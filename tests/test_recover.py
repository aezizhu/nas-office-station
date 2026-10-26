"""recover_orphan_workdirs() (startup): pages left in SCANS/.work-* by a killed/restarted process
are published as a PDF instead of being silently deleted 15 minutes later."""
import io
import os

from PIL import Image

import station_support as sup

DPI = 100


def png(w_mm=148, h_mm=210):
    im = Image.new("RGB", (round(w_mm / 25.4 * DPI), round(h_mm / 25.4 * DPI)), (250, 250, 250))
    buf = io.BytesIO()
    im.save(buf, "PNG", dpi=(DPI, DPI))
    return buf.getvalue()


def workdir(m, name, pages):
    d = os.path.join(m.SCANS, name)
    os.makedirs(d)
    for fname, data in pages:
        with open(os.path.join(d, fname), "wb") as f:
            f.write(data)
    return d


def listing(m):
    """PDFs only; every recovered PDF must also carry its 'partial' check record (scan-*.pdf.quality.json)."""
    names = set(os.listdir(m.SCANS)) - {"events.log"}
    pdfs = sorted(n for n in names if n.endswith(".pdf") and not n.startswith("."))
    assert names - set(pdfs) <= {n + ".quality.json" for n in pdfs} | {".small-" + n for n in pdfs}, names
    return pdfs


def test_recovers_readable_pages_and_cleans_up(m, img2pdf):
    good = png()
    workdir(m, ".work-20260923-101112-abcdef", [("p001.png", good), ("p002.png", good),
                                                ("p003.png", good[: len(good) // 2]),
                                                ("scanimage.err", b"stall")])
    workdir(m, ".work-20260923-101200-123456", [])                                  # nothing scanned
    workdir(m, ".work-20260923-101300-zzzzzz", [("p001.png", b"")])                 # only a 0-byte page
    with open(os.path.join(m.SCANS, ".part-scan-20260923-090000-1p.pdf"), "wb") as f:
        f.write(b"%PDF-half")
    m.recover_orphan_workdirs()
    assert listing(m) == ["scan-20260923-101112-2p.pdf"]
    sizes = sup.pdf_page_sizes(os.path.join(m.SCANS, "scan-20260923-101112-2p.pdf"))
    assert sizes is None or (len(sizes) == 2 and abs(sup.mm_from_pt(sizes[0][0]) - 148) < 1.5)
    with open(m.EVENTS, encoding="utf-8") as f:
        log = f.read()
    assert "\tscan-recovered\tscan-20260923-101112-2p.pdf from .work-20260923-101112-abcdef" in log
    assert [r["name"] for r in m._file_rows()] == ["scan-20260923-101112-2p.pdf"]   # fresh 15-min clock
    assert m._file_rows()[0]["left_min"] == 14
    assert m._file_rows()[0]["quality"]["state"] == "partial"          # recovered = never shown as clean


def test_existing_pdf_is_not_overwritten(m, img2pdf):
    target = os.path.join(m.SCANS, "scan-20260923-101112-1p.pdf")
    with open(target, "wb") as f:
        f.write(b"%PDF-original")
    workdir(m, ".work-20260923-101112-abcdef", [("p001.png", png())])
    m.recover_orphan_workdirs()
    assert open(target, "rb").read() == b"%PDF-original"
    assert listing(m) == ["scan-20260923-101112-1p.pdf"]


def test_unparseable_workdir_name_uses_current_stamp(m, img2pdf):
    workdir(m, ".work-weird", [("p001.png", png())])
    m.recover_orphan_workdirs()
    (name,) = listing(m)
    assert m._scan_name_ok(name) and name.endswith("-1p.pdf")


def test_publish_failure_is_logged_and_dir_still_removed(m, bindir):
    sup.write_exe(str(bindir / "img2pdf"), "#!/bin/sh\necho boom >&2\nexit 1\n")
    workdir(m, ".work-20260923-101112-abcdef", [("p001.png", png())])
    m.recover_orphan_workdirs()
    assert listing(m) == []
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tscan-recover-fail\t.work-20260923-101112-abcdef" in f.read()
