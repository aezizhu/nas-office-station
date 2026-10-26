"""/upload (PDF, images, webp, HEIC refusal, encrypted/corrupt input), /copy, /delete, /files,
/f/<name>, GET /, login, 413."""
import base64
import io
import json
import os
import re
import time

import pytest
from PIL import Image

import station_support as sup

PASSWORD_MSG = "This PDF is encrypted. Save an unencrypted copy before uploading"
BROKEN_PDF_MSG = "Cannot open this file. It may be damaged or not a valid PDF"


def upload(client, data, name):
    r = client.post("/upload", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
    assert r.is_json, r.data[:200]
    return r.status_code, r.get_json()


def jobs(m):
    return os.listdir(m.JOBS)


def img_bytes(im, fmt="PNG", **kw):
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def page_size(m, token):
    (w, h), = sup.pdf_page_sizes(os.path.join(m.JOBS, token, "doc.pdf"))
    return w, h


# ---------------------------------------------------------------- PDFs

def test_encrypted_pdf_rejected_with_password_message(m, client, poppler, tmp_path):
    pikepdf = pytest.importorskip("pikepdf")
    p = tmp_path / "enc.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(str(p), encryption=pikepdf.Encryption(user="x", owner="y"))
    code, j = upload(client, p.read_bytes(), "合同.pdf")
    assert 400 <= code < 500 and j == {"error": PASSWORD_MSG}
    assert jobs(m) == [], "job dir not removed"
    with open(m.EVENTS, encoding="utf-8") as f:
        assert "\tupload-fail\t" in f.read()


def test_corrupt_pdf_rejected(m, client, poppler):
    code, j = upload(client, b"%PDF-1.4\nthis is not really a pdf\n", "x.pdf")
    assert code == 422 and j["error"] == BROKEN_PDF_MSG
    assert jobs(m) == []


def test_pdf_upload_ok(m, client, poppler):
    code, j = upload(client, sup.pdf_bytes([sup.A3_PT, sup.A4_PT]), "a3.PDF")
    assert code == 200 and j["pages"] == 2 and j["docsize"] == "A3" and j["name"] == "a3.PDF"
    d = os.path.join(m.JOBS, j["token"])
    assert open(os.path.join(d, "pages")).read() == "2"
    assert os.path.exists(os.path.join(d, "pg-1.png"))     # first preview rendered up front


# ---------------------------------------------------------------- images

@pytest.mark.parametrize("name,make,landscape", [
    ("wide.png", lambda: Image.new("RGB", (1200, 600), (200, 200, 200)), True),
    ("tall.png", lambda: Image.new("RGB", (600, 1200), (200, 200, 200)), False),
    ("pal.png", lambda: Image.new("RGB", (900, 600), (255, 0, 0)).convert("P"), True),
    ("pal-trns.png", lambda: _pal_trns(), False),
    ("rgba.png", lambda: Image.new("RGBA", (800, 1000), (0, 0, 255, 120)), False),
    ("la.png", lambda: Image.new("LA", (1000, 700), (90, 200)), True),
    ("photo.JPG", lambda: Image.new("RGB", (1600, 1200), (10, 120, 30)), True),
])
def test_image_upload(m, client, poppler, img2pdf, name, make, landscape):
    im = make()
    fmt = "JPEG" if name.lower().endswith(".jpg") else "PNG"
    code, j = upload(client, img_bytes(im, fmt), name)
    assert code == 200, j
    assert j["pages"] == 1 and j["docsize"] == "A4"
    w, h = page_size(m, j["token"])
    assert (w > h) == landscape, "page orientation should follow the image (%sx%s pt)" % (w, h)


def _pal_trns():
    im = Image.new("P", (600, 900), 0)
    im.putpalette([255, 255, 255, 0, 0, 0] + [0] * 762)
    im.info["transparency"] = 0
    return im


@pytest.mark.parametrize("mode,size,landscape", [("RGB", (1400, 700), True), ("RGBA", (500, 900), False)])
def test_webp_upload_converted(m, client, poppler, img2pdf, mode, size, landscape):
    from PIL import features
    if not features.check("webp"):
        pytest.skip("Pillow built without WebP")
    color = (30, 60, 90, 128) if mode == "RGBA" else (30, 60, 90)
    code, j = upload(client, img_bytes(Image.new(mode, size, color), "WEBP"), "shot.webp")
    assert code == 200, j
    w, h = page_size(m, j["token"])
    assert (w > h) == landscape


@pytest.mark.parametrize("name", ["IMG_0001.HEIC", "photo.heif", "x.heic"])
def test_heic_refused_with_iphone_guidance(m, client, name):
    code, j = upload(client, b"\x00\x00\x00\x18ftypheic", name)
    assert code == 400 and "iPhone" in j["error"] and "HEIC" in j["error"] and "JPG" in j["error"]
    assert jobs(m) == []


@pytest.mark.parametrize("name", ["virus.exe", "noext", "archive.zip", "a.svg"])
def test_unsupported_type_400(m, client, name):
    code, j = upload(client, b"x", name)
    assert code == 400 and "Unsupported file type" in j["error"]
    assert jobs(m) == []


def test_no_file_400(m, client):
    r = client.post("/upload", data={}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"].startswith("No file received")


def test_corrupt_image_plain_message(m, client, poppler, img2pdf):
    code, j = upload(client, b"\x89PNG\r\n\x1a\nbroken", "scan.png")
    assert code == 422
    err = j["error"]
    assert m.JOBS not in err and "/src.png" not in err and "Command" not in err
    assert "img2pdf" not in err and "Traceback" not in err
    assert jobs(m) == []


# ---------------------------------------------------------------- _image_to_png + img2pdf retry

def _png_header_only(w, h):
    """A PNG whose IHDR claims w×h but carries no real pixel data (a 'decompression bomb' stand-in)."""
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


@pytest.mark.parametrize("make,fmt,want_mode", [
    (lambda: Image.new("RGB", (60, 40), (1, 2, 3)), "WEBP", "RGB"),
    (lambda: Image.new("RGBA", (60, 40), (1, 2, 3, 100)), "WEBP", "RGBA"),
    (lambda: Image.new("RGBA", (60, 40), (1, 2, 3, 100)), "TIFF", "RGBA"),
    (lambda: Image.new("LA", (60, 40), (90, 100)), "TIFF", "RGBA"),
    (lambda: Image.new("RGB", (60, 40), (255, 0, 0)).convert("P"), "PNG", "RGB"),
    (lambda: Image.new("CMYK", (60, 40), (0, 50, 100, 0)), "TIFF", "RGB"),
    (lambda: Image.new("1", (60, 40), 1), "PNG", "L"),
])
def test_image_to_png_modes(m, tmp_path, make, fmt, want_mode):
    from PIL import features
    if fmt == "WEBP" and not features.check("webp"):
        pytest.skip("Pillow built without WebP")
    src = str(tmp_path / ("src." + fmt.lower()))
    make().save(src, fmt)
    dst = str(tmp_path / "conv.png")
    assert m._image_to_png(src, dst) == dst
    with Image.open(dst) as im:
        assert im.format == "PNG" and im.mode == want_mode and im.size == (60, 40)


def test_image_to_png_16bit_scaled_not_clipped(m, tmp_path):
    import numpy as np
    a = np.array([[0, 256, 32768, 65535]], dtype=np.uint16)
    src = str(tmp_path / "i16.png")
    Image.fromarray(a).save(src)
    assert Image.open(src).mode.startswith("I")
    dst = m._image_to_png(src, str(tmp_path / "conv.png"))
    with Image.open(dst) as im:
        assert im.mode == "L"
        assert list(np.asarray(im)[0]) == [0, 1, 128, 255]     # scaled by 1/256, not clipped to white


def test_image_to_png_refuses_huge_image_from_header(m, tmp_path):
    src = tmp_path / "bomb.png"
    src.write_bytes(_png_header_only(9000, 7000))              # 63 M px > MAX_IMAGE_PIXELS (60 M)
    dst = tmp_path / "conv.png"
    with pytest.raises(ValueError, match=r"Image too large \(9000 x 7000 pixels\)"):
        m._image_to_png(str(src), str(dst))
    assert not dst.exists()
    assert m.MAX_IMAGE_PIXELS == 60_000_000


@pytest.mark.parametrize("mode,size,landscape", [("RGBA", (1200, 600), True), ("LA", (500, 900), False)])
def test_transparent_tiff_upload_works(m, client, poppler, img2pdf, mode, size, landscape):
    """img2pdf 0.4.4 refuses TIFFs with alpha ('must not be called on images with alpha'): /upload
    retries once through _image_to_png instead of failing the user."""
    color = (20, 40, 60, 120) if mode == "RGBA" else (90, 120)
    code, j = upload(client, img_bytes(Image.new(mode, size, color), "TIFF"), "scan.tif")
    assert code == 200, j
    assert j["pages"] == 1 and j["docsize"] == "A4"
    w, h = page_size(m, j["token"])
    assert (w > h) == landscape


def test_img2pdf_failure_retries_once_via_png(m, client, poppler, img2pdf, bindir):
    calls = bindir.parent / "img2pdf.calls"
    real = os.path.join(img2pdf, "img2pdf")
    sup.write_exe(str(bindir / "img2pdf"),
                  '#!/bin/sh\necho "$1" >> "%s"\ncase "$1" in *src.png) echo "error: unsupported" >&2; exit 1;; esac\n'
                  'exec "%s" "$@"\n' % (calls, real))
    code, j = upload(client, img_bytes(Image.new("RGB", (800, 600), (5, 5, 5))), "photo.png")
    assert code == 200, j
    tried = [os.path.basename(x) for x in calls.read_text().split()]
    assert tried == ["src.png", "conv.png"]                    # exactly one retry, through the plain PNG


def test_img2pdf_failing_twice_is_plain_422(m, client, poppler, bindir):
    sup.write_exe(str(bindir / "img2pdf"), '#!/bin/sh\necho "error: nope" >&2\nexit 1\n')
    code, j = upload(client, img_bytes(Image.new("RGB", (80, 60), (5, 5, 5))), "photo.png")
    assert code == 422 and "Command" not in j["error"] and m.JOBS not in j["error"]
    assert jobs(m) == []


def test_oversized_webp_is_refused_before_decoding(m, client, monkeypatch):
    from PIL import features
    if not features.check("webp"):
        pytest.skip("Pillow built without WebP")
    monkeypatch.setattr(m, "MAX_IMAGE_PIXELS", 1000)
    code, j = upload(client, img_bytes(Image.new("RGB", (100, 100), (9, 9, 9)), "WEBP"), "big.webp")
    assert code == 422 and "Image too large (100 x 100 pixels). Resize it before uploading" in j["error"]
    assert jobs(m) == []


# ---------------------------------------------------------------- Office / WPS conversion

OFFICE_FAIL = "Conversion failed. The file may be damaged or unsupported. Export a PDF from the original application and upload it"


@pytest.fixture()
def office(bindir, tmp_path, m, monkeypatch):
    """Fake unoconv/soffice. mode per tool: ok (write a real 2-page PDF), fail (exit 1, no output)."""
    monkeypatch.delenv("STATION_SOFFICE", raising=False)
    pdf = tmp_path / "converted.pdf"
    pdf.write_bytes(sup.pdf_bytes([sup.A4_PT, sup.A4_PT]))
    log = tmp_path / "office.log"

    def install(unoconv="fail", soffice="fail"):
        # unoconv -f pdf -o <pdf> <src> ; soffice --headless --convert-to pdf --outdir <d> <src>
        ok_u = 'cp "%s" "$4"' % pdf
        ok_s = 'while [ "$1" != "--outdir" ]; do shift; done; shift; cp "%s" "$1/src.pdf"; echo "HOME=$HOME" >> "%s"' % (pdf, log)
        script = "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo \"LibreOffice test\"; exit 0; fi\necho %s >> '%s'\n%s\n"
        sup.write_exe(str(bindir / "unoconv"), script % (
            "unoconv", log, ok_u if unoconv == "ok" else "echo 'unoconv: Error: Unable to connect' >&2; exit 1"))
        sup.write_exe(str(bindir / "soffice"), script % (
            "soffice", log, ok_s if soffice == "ok" else "echo 'Error: source file could not be loaded' >&2; exit 1"))
    install.log = lambda: log.read_text().split() if log.exists() else []
    return install


@pytest.mark.parametrize("name", ["合同.docx", "report.wps"])        # one code path for all OFFICE_EXT
def test_office_conversion_failure_one_plain_message(m, client, office, name):
    office(unoconv="fail", soffice="fail")
    code, j = upload(client, b"PK\x03\x04 not really office", name)
    assert code == 422 and j == {"error": OFFICE_FAIL}
    assert jobs(m) == []
    assert office.log() == ["unoconv", "soffice"]             # resident listener first, then cold start


def test_office_converter_missing_is_not_raw_errno(m, client, monkeypatch):
    def missing(cmd, timeout, **kw):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])
    monkeypatch.setattr(m, "run_group", missing)
    code, j = upload(client, b"x", "a.doc")
    assert code == 422 and j == {"error": OFFICE_FAIL}
    assert jobs(m) == []


def test_office_conversion_timeout(m, client, monkeypatch):
    import subprocess

    def slow(cmd, timeout, **kw):
        if cmd[0] == "unoconv":
            raise RuntimeError("listener down")
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(m, "run_group", slow)
    code, j = upload(client, b"x", "big.pptx")
    assert code == 422 and "Conversion timed out" in j["error"]
    assert jobs(m) == []


def test_office_unoconv_ok(m, client, poppler, office):
    office(unoconv="ok", soffice="fail")
    code, j = upload(client, b"x", "memo.docx")
    assert code == 200 and j["pages"] == 2 and j["docsize"] == "A4"
    assert office.log() == ["unoconv"]


def test_office_cold_start_fallback_ok(m, client, poppler, office):
    office(unoconv="fail", soffice="ok")
    code, j = upload(client, b"x", "sheet.xlsx")
    assert code == 200 and j["pages"] == 2
    log = office.log()
    assert log[:2] == ["unoconv", "soffice"]
    assert log[2] == "HOME=" + os.path.join(m.JOBS, j["token"])   # isolated profile per conversion


def test_upload_too_large_413_json(m, client, monkeypatch):
    monkeypatch.setitem(m.app.config, "MAX_CONTENT_LENGTH", 1000)
    r = client.post("/upload", data={"file": (io.BytesIO(b"x" * 5000), "big.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 413 and r.is_json and "File too large" in r.get_json()["error"]


# ---------------------------------------------------------------- /copy

SCAN = "scan-20260923-101010-1p.pdf"


def put_scan(m, name=SCAN, data=None, age=0):
    p = os.path.join(m.SCANS, name)
    with open(p, "wb") as f:
        f.write(data if data is not None else sup.pdf_bytes([sup.A3_PT]))
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def test_copy_returns_docsize(m, client, poppler):
    put_scan(m)
    r = client.post("/copy", data={"name": SCAN})
    j = r.get_json()
    assert r.status_code == 200 and j["pages"] == 1 and j["docsize"] == "A3" and j["name"] == SCAN
    assert os.path.exists(os.path.join(m.JOBS, j["token"], "doc.pdf"))
    assert os.path.exists(os.path.join(m.SCANS, SCAN)), "copy must not consume the scan"


def test_copy_missing_410_and_no_leftover(m, client):
    r = client.post("/copy", data={"name": SCAN})
    assert r.status_code == 410 and "removed after" in r.get_json()["error"]
    assert jobs(m) == []


def test_copy_corrupt_422_and_no_leftover(m, client, poppler):
    put_scan(m, data=b"garbage")
    r = client.post("/copy", data={"name": SCAN})
    assert r.status_code == 422 and r.get_json()["error"] == BROKEN_PDF_MSG
    assert jobs(m) == []


@pytest.mark.parametrize("name", ["", "../etc/passwd", "scan-1.pdf", "events.log", SCAN + "/x"])
def test_copy_bad_name_400(m, client, name):
    r = client.post("/copy", data={"name": name})
    assert r.status_code == 400 and r.get_json()["error"] == "Invalid filename"


# ---------------------------------------------------------------- /delete, /f, /files

def test_delete_json_and_idempotent(m, client):
    put_scan(m)
    r = client.post("/delete", data={"name": SCAN})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert not os.path.exists(os.path.join(m.SCANS, SCAN))
    r = client.post("/delete", data={"name": SCAN})          # already gone → same result
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    with open(m.EVENTS, encoding="utf-8") as f:
        assert f.read().count("\tscan-delete\t" + SCAN) == 2


@pytest.mark.parametrize("name", ["", "../scans/events.log", "events.log", ".part-" + SCAN])
def test_delete_bad_name_400(m, client, name):
    put_scan(m)
    r = client.post("/delete", data={"name": name})
    assert r.status_code == 400 and r.is_json
    assert os.path.exists(os.path.join(m.SCANS, SCAN))


def test_download_only_scan_pdfs(m, client):
    put_scan(m)
    r = client.get("/f/" + SCAN)
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    r.close()
    for bad in ("events.log", "../printjobs/x", ".part-" + SCAN):
        assert client.get("/f/" + bad).status_code == 404


def test_files_json_sorted_and_hides_parts(m, client):
    put_scan(m, "scan-20260923-101010-1p.pdf", age=600)
    put_scan(m, "scan-20260923-111111-3p.pdf", data=b"%PDF-" + b"0" * 5000)
    put_scan(m, ".part-scan-20260923-121212-1p.pdf")
    r = client.get("/files")
    assert r.status_code == 200
    rows = r.get_json()["files"]
    assert [x["name"] for x in rows] == ["scan-20260923-111111-3p.pdf", "scan-20260923-101010-1p.pdf"]
    assert rows[0]["kb"] == 4 and rows[1]["kb"] >= 1
    assert rows[0]["left_min"] == 14 and rows[1]["left_min"] == 4
    assert set(rows[0]) == {"name", "kb", "left_min", "quality", "raw", "small"}
    # no .quality.json next to these hand-made PDFs: the list must say "check it", never a silent green
    assert rows[0]["quality"]["state"] == "review"


def test_file_rows_tolerates_a_vanishing_file(m, client, monkeypatch):
    keep = put_scan(m, "scan-20260923-101010-1p.pdf")
    gone = put_scan(m, "scan-20260923-111111-1p.pdf")
    real = os.path.getsize

    def flaky(p):
        if os.path.abspath(p) == os.path.abspath(gone):
            raise FileNotFoundError(2, "No such file or directory", p)
        return real(p)
    monkeypatch.setattr(os.path, "getsize", flaky)
    assert [r["name"] for r in m._file_rows()] == [os.path.basename(keep)]
    r = client.get("/files")
    assert r.status_code == 200 and [x["name"] for x in r.get_json()["files"]] == [os.path.basename(keep)]


def test_index_page(m, client):
    if "__BOOT_JSON__" not in m.PAGE:
        pytest.skip("PAGE template not yet converted to the __BOOT_JSON__ placeholder")
    put_scan(m)
    evil = "scan-</script><script>alert(1)-1p.pdf".replace("/", "")    # '<' in a listed file name
    put_scan(m, evil, age=60)
    r = client.get("/")
    assert r.status_code == 200 and r.mimetype == "text/html"
    body = r.get_data(as_text=True)
    assert "__BOOT_JSON__" not in body
    assert "<script>alert(1)" not in body                 # boot data can't break out of its <script>
    mt = re.search(r"const BOOT = (\{.*?\});\n", body)
    assert mt, "boot data not found in the page"
    boot = json.loads(mt.group(1))
    assert [f["name"] for f in boot["files"]] == [SCAN, evil]
    assert boot["res_default"] in boot["res"] and boot["retain_min"] == m.RETAIN_SEC // 60
    assert [k for k, _ in boot["sources"]] == list(m.SOURCES) and [k for k, _ in boot["scales"]] == list(m.SCALES)
    assert boot["adf_max_w"] == 297


# ---------------------------------------------------------------- login

def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()}


def test_login_required_when_password_set(m, client, monkeypatch):
    monkeypatch.setattr(m, "STATION_PASSWORD", "办公室密码")
    monkeypatch.setattr(m, "STATION_USER", "office")
    r = client.get("/files")
    assert r.status_code == 401 and "Basic" in r.headers["WWW-Authenticate"]
    assert client.get("/files", headers=_basic("office", "wrong")).status_code == 401
    assert client.get("/files", headers=_basic("用户", "办公室密码")).status_code == 401   # no 500 on non-ASCII
    assert client.get("/files", headers=_basic("office", "办公室密码")).status_code == 200


def test_no_password_means_open(m, client):
    assert client.get("/files").status_code == 200


# ---------------------------------------------------------------- disguised / oversized images (pre-deploy review V01)

@pytest.mark.parametrize("name", ["x.webp", "photo.png", "scan.jpg", "a.tif"])
def test_postscript_disguised_as_image_is_refused_without_ghostscript(m, client, name, monkeypatch):
    # EPS content under an image name: Pillow would hand it to Ghostscript (no timeout) inside PID 1.
    # The header check must refuse it by content before anything decodes it.
    called = []
    monkeypatch.setattr(m, "run_group", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("img2pdf must not run")))
    code, j = upload(client, b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\n{} loop\n", name)
    assert code == 422 and "Cannot open" in j["error"] and "export it as JPG or PNG" in j["error"]
    assert not called and jobs(m) == []


def test_decompression_bomb_webp_refused_from_header(m, client, monkeypatch):
    # ~200 KB solid-colour webp that would decode to 9400×9400×4 bytes per copy in the Flask process
    data = img_bytes(Image.new("RGB", (9400, 9400), (200, 10, 10)), "WEBP", quality=10)
    assert len(data) < 400_000
    monkeypatch.setattr(m, "_image_to_png", lambda *a: (_ for _ in ()).throw(AssertionError("must not decode")))
    code, j = upload(client, data, "long-screenshot.webp")
    assert code == 422 and "Image too large (9400 x 9400 pixels)" in j["error"]
    assert jobs(m) == []
