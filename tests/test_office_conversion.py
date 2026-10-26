"""Office conversion admission, artifact validation and layout review metadata."""
import hashlib
import io
import json
import subprocess
import threading
from pathlib import Path
from zipfile import ZipFile

import pytest

import station_support as sup


SELECTED = '/opt/test-libreoffice/program/soffice'


def docx(pages=None, body=None):
    out = io.BytesIO()
    with ZipFile(out, 'w') as z:
        z.writestr('word/document.xml', body or '<document>Source text and picture reference</document>')
        z.writestr('word/media/seal.png', b'unchanged seal data')
        if pages is not None:
            z.writestr('docProps/app.xml', '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Pages>%s</Pages></Properties>' % pages)
    return out.getvalue()


def upload(client, data=None, name='form.docx'):
    return client.post('/upload', data={'file': (io.BytesIO(docx() if data is None else data), name)},
                       content_type='multipart/form-data')


@pytest.fixture()
def engine(m, monkeypatch):
    calls = []
    payload = sup.pdf_bytes([sup.A4_PT, sup.A4_PT])

    def run(cmd, timeout, **kw):
        calls.append((cmd, kw))
        if '--version' in cmd:
            return subprocess.CompletedProcess(cmd, 0, 'LibreOffice 26.2.7.2\n', '')
        if cmd[0] == 'unoconv':
            return subprocess.CompletedProcess(cmd, 1, '', 'listener unavailable')
        out = Path(cmd[cmd.index('--outdir') + 1]) / (Path(cmd[-1]).stem + '.pdf')
        out.write_bytes(payload)
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setenv('STATION_SOFFICE', SELECTED)
    monkeypatch.setattr(m, 'run_group', run)
    return calls, payload, run


def test_selected_engine_bypasses_listener_and_preserves_source(m, client, poppler, engine):
    data = docx(2)
    r = upload(client, data)
    assert r.status_code == 200
    assert all(cmd[0] == SELECTED for cmd, _ in engine[0])
    assert r.json['pages'] == 2
    target = Path(m.JOBS) / r.json['token']
    assert (target / 'src.docx').read_bytes() == data
    meta = json.loads((target / 'conversion.json').read_text())
    assert meta['source_sha256'] == hashlib.sha256(data).hexdigest()
    assert meta['pdf_sha256'] == hashlib.sha256(engine[1]).hexdigest()
    assert meta['version'] == 'LibreOffice 26.2.7.2'
    assert meta['pages'] == 2
    assert isinstance(meta['elapsed_seconds'], (int, float)) and meta['elapsed_seconds'] >= 0
    assert len((target / 'conversion.json').read_bytes()) < 4096
    events = Path(m.EVENTS).read_text()
    assert 'office-conversion' in events and 'pages=2' in events
    assert 'Source text' not in events


def test_selected_engine_has_unique_explicit_profiles(m, client, poppler, engine):
    a, b = upload(client), upload(client)
    assert a.status_code == b.status_code == 200
    calls = [(cmd, kw) for cmd, kw in engine[0] if '--convert-to' in cmd]
    profiles = [[x for x in cmd if x.startswith('-env:UserInstallation=')] for cmd, _ in calls]
    assert all(len(p) == 1 for p in profiles)
    assert profiles[0] != profiles[1]
    assert all(p[0].startswith('-env:UserInstallation=file:///') for p in profiles)


@pytest.mark.parametrize('pages', [None, 'garbage', '0', '100001', '2'])
def test_absent_invalid_or_matching_cached_pages_do_not_warn(client, poppler, engine, pages):
    r = upload(client, docx(pages))
    assert r.status_code == 200
    assert not r.json.get('quality')


def test_cached_page_discrepancy_is_review_hint_not_rewrite(m, client, poppler, engine):
    data = docx(1)
    r = upload(client, data)
    assert r.status_code == 200 and r.json['pages'] == 2
    q = r.json.get('quality', {})
    assert q.get('kind') == 'office_conversion' and q.get('state') == 'review'
    message = ' '.join(q['issues'])
    assert '1' in message and '2' in message and 'stale' in message
    assert (Path(m.JOBS) / r.json['token'] / 'src.docx').read_bytes() == data


def test_selected_nonzero_partial_output_is_rejected(m, client, poppler, engine, monkeypatch):
    calls, _, run = engine
    def fail(cmd, timeout, **kw):
        r = run(cmd, timeout, **kw)
        if '--convert-to' in cmd:
            r.returncode = 1
        return r
    monkeypatch.setattr(m, 'run_group', fail)
    r = upload(client)
    assert r.status_code == 422
    assert all(cmd[0] == SELECTED for cmd, _ in calls)
    assert not list(Path(m.JOBS).iterdir())
    assert 'outcome=failed' in Path(m.EVENTS).read_text()


def test_invalid_selected_pdf_is_rejected(m, client, poppler, engine, monkeypatch):
    _, _, run = engine
    def bad(cmd, timeout, **kw):
        r = run(cmd, timeout, **kw)
        if '--convert-to' in cmd:
            (Path(cmd[cmd.index('--outdir') + 1]) / 'src.pdf').write_bytes(b'not a pdf')
        return r
    monkeypatch.setattr(m, 'run_group', bad)
    assert upload(client).status_code == 422


def test_legacy_partial_output_removed_before_fallback(m, client, poppler, engine, monkeypatch):
    monkeypatch.delenv('STATION_SOFFICE')
    calls, payload, run = engine
    def fallback(cmd, timeout, **kw):
        if cmd[0] == 'unoconv':
            calls.append((cmd, kw))
            Path(cmd[4]).write_bytes(payload)
            return subprocess.CompletedProcess(cmd, 1, '', 'failed after partial write')
        if '--convert-to' in cmd:
            assert not (Path(cmd[cmd.index('--outdir') + 1]) / 'doc.pdf').exists()
            assert not (Path(cmd[cmd.index('--outdir') + 1]) / 'listener.pdf').exists()
        return run(cmd, timeout, **kw)
    monkeypatch.setattr(m, 'run_group', fallback)
    r = upload(client)
    assert r.status_code == 200
    assert any(cmd[0] == 'soffice' and '--convert-to' in cmd for cmd, _ in calls)


def test_conversion_busy_visible_and_second_request_rejected(m, client, poppler, engine, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    _, _, run = engine
    response = []
    def wait(cmd, timeout, **kw):
        if '--convert-to' in cmd:
            entered.set()
            assert release.wait(5)
        return run(cmd, timeout, **kw)
    monkeypatch.setattr(m, 'run_group', wait)
    def first():
        with m.app.test_client() as c:
            response.append(upload(c))
    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(3)
        state = client.get('/status').json
        assert state.get('conversion', {}).get('busy') is True
        assert state['busy'] is True
        r = upload(client)
        assert r.status_code == 429 and 'converted' in r.json['error']
        assert len(list(Path(m.JOBS).iterdir())) == 1
    finally:
        release.set()
        worker.join(6)
    assert response[0].status_code == 200
    assert client.get('/status').json['conversion']['busy'] is False


def test_conversion_quality_reaches_ui_with_conversion_wording(client):
    html = client.get('/').get_data(as_text=True)
    assert "quality.kind === 'office_conversion'" in html
    assert 'Review required: conversion may change the layout' in html
    assert "quality: r.data.quality}, {kind: 'upload'}" in html


def test_unconfigured_engine_preserves_legacy_route(client, poppler, engine, monkeypatch):
    monkeypatch.delenv('STATION_SOFFICE')
    assert upload(client).status_code == 200
    assert engine[0][0][0][0] == 'unoconv'
    assert any(cmd[0] == 'soffice' for cmd, _ in engine[0])


@pytest.mark.parametrize('properties', [
    b'<invalid',
    ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE Properties [<!ENTITY x "1">]><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Pages>&x;</Pages></Properties>').encode('utf-16'),
    b'<Properties>' + b' ' * 65536 + b'</Properties>',
    b'<!DOCTYPE Properties [<!ENTITY x "1">]><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Pages>&x;</Pages></Properties>',
])
def test_untrusted_or_oversized_metadata_is_ignored(client, poppler, engine, properties):
    out = io.BytesIO()
    with ZipFile(out, 'w') as z:
        z.writestr('docProps/app.xml', properties)
    r = upload(client, out.getvalue())
    assert r.status_code == 200
    assert not r.json['quality']


def test_failed_conversion_releases_admission(m, client, poppler, engine, monkeypatch):
    _, _, run = engine
    def fail(cmd, timeout, **kw):
        if '--convert-to' in cmd:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return run(cmd, timeout, **kw)
    monkeypatch.setattr(m, 'run_group', fail)
    assert upload(client).status_code == 422
    assert client.get('/status').json['conversion']['busy'] is False
    monkeypatch.setattr(m, 'run_group', run)
    assert upload(client).status_code == 200


def test_failure_event_has_status_without_converter_output(m, client, poppler, engine, monkeypatch):
    _, _, run = engine
    def bad(cmd, timeout, **kw):
        result = run(cmd, timeout, **kw)
        if '--convert-to' in cmd:
            result.returncode = 7
            result.stderr = 'private document text /root/printjobs/secret/src.docx'
        return result
    monkeypatch.setattr(m, 'run_group', bad)
    assert upload(client).status_code == 422
    events = Path(m.EVENTS).read_text()
    assert 'reason=converter_exit status=7' in events
    assert 'private document text' not in events and '/root/printjobs/secret' not in events


def test_unknown_metadata_encoding_is_optional(client, poppler, engine):
    out = io.BytesIO()
    with ZipFile(out, 'w') as z:
        z.writestr('docProps/app.xml', '<?xml version="1.0" encoding="unknown-encoding"?><Properties><Pages>1</Pages></Properties>')
    r = upload(client, out.getvalue())
    assert r.status_code == 200
    assert not r.json['quality']


def test_late_listener_output_cannot_replace_published_pdf(m, client, poppler, engine, monkeypatch):
    monkeypatch.delenv('STATION_SOFFICE')
    _, payload, run = engine
    listener_paths = []
    def slow_listener(cmd, timeout, **kw):
        if cmd[0] == 'unoconv':
            listener_paths.append(Path(cmd[4]))
            raise subprocess.TimeoutExpired(cmd, timeout)
        return run(cmd, timeout, **kw)
    monkeypatch.setattr(m, 'run_group', slow_listener)
    r = upload(client)
    assert r.status_code == 200
    listener_paths[0].write_bytes(b'late output from old listener')
    assert (Path(m.JOBS) / r.json['token'] / 'doc.pdf').read_bytes() == payload


@pytest.mark.parametrize('pages', [None, 2])
@pytest.mark.parametrize('markup', [
    '<w:tblPr><w:tblpPr w:vertAnchor="page"/></w:tblPr>',
    '<v:shape><v:textbox><w:txbxContent/></v:textbox></v:shape>',
])
def test_floating_layout_warns_even_without_page_discrepancy(client, poppler, engine, pages, markup):
    body = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml">' + markup + '</w:document>'
    r = upload(client, docx(pages, body))
    assert r.status_code == 200 and r.json['pages'] == 2
    quality = r.json.get('quality') or {}
    assert quality.get('kind') == 'office_conversion' and quality.get('state') == 'review'
    assert 'page count matches' in ' '.join(quality['issues'])


def test_large_layout_is_reviewable_without_unbounded_xml_read(client, poppler, engine):
    body = '<document>' + ' ' * (4 * 1024 * 1024) + '</document>'
    r = upload(client, docx(2, body))
    assert r.status_code == 200
    assert 'incomplete' in ' '.join((r.json.get('quality') or {}).get('issues', []))


@pytest.mark.parametrize('field,value', [(10, 4097), (12, 2 * 1024 * 1024 + 1), (10, 65535)])
def test_archive_directory_bound_before_zipfile(m, client, poppler, engine, monkeypatch, field, value):
    import struct
    raw = bytearray(docx(2))
    end = raw.rfind(b'PK\x05\x06')
    struct.pack_into('<H' if field == 10 else '<I', raw, end + field, value)
    opened = []
    def unexpected_open(*args, **kwargs):
        opened.append(args)
        raise AssertionError('central directory must be bounded first')
    monkeypatch.setattr(m.office_conversion, 'ZipFile', unexpected_open)
    r = upload(client, bytes(raw))
    assert r.status_code == 200
    assert not opened
    assert 'incomplete' in ' '.join((r.json.get('quality') or {}).get('issues', []))


def test_zip64_locator_before_maximum_comment_is_rejected_before_zipfile(m, client, poppler, engine, monkeypatch):
    import struct
    original = docx(2)
    end = original.rfind(b'PK\x05\x06')
    record = list(struct.unpack_from('<4s4H2LH', original, end))
    zip64 = struct.pack('<4sQ2H2L4Q', b'PK\x06\x06', 44, 45, 45, 0, 0,
                        record[4], record[4], record[5], record[6])
    locator = struct.pack('<4sLQL', b'PK\x06\x07', 0, end, 1)
    record[-1] = 65535
    # Small legacy directory fields can coexist with ZIP64 records. A maximum
    # comment places the locator just outside an EOCD+comment-only tail read.
    raw = original[:end] + zip64 + locator + struct.pack('<4s4H2LH', *record) + b'a' * 65535
    with ZipFile(io.BytesIO(raw)) as archive:
        assert len(archive.infolist()) == 3  # a valid small ZIP64 fixture
    opened = []
    def unexpected_open(*args, **kwargs):
        opened.append(args)
        raise AssertionError('ZIP64 must not reach the eager central-directory parser')
    monkeypatch.setattr(m.office_conversion, 'ZipFile', unexpected_open)
    r = upload(client, raw)
    assert r.status_code == 200
    assert not opened
    assert 'incomplete' in ' '.join((r.json.get('quality') or {}).get('issues', []))


DOCX_CONVERTER = '/opt/docx-print-converter/bin/docx-print-convert'


@pytest.fixture()
def docx_converter(m, monkeypatch):
    calls = []
    payload = sup.pdf_bytes([sup.A4_PT])
    def run(cmd, timeout, **kw):
        calls.append((cmd, timeout, kw))
        assert cmd[0] == DOCX_CONVERTER, 'The configured DOCX route must not call a legacy renderer'
        assert len(cmd) == 5 and cmd[3] == '--report'
        src, pdf, report = Path(cmd[1]), Path(cmd[2]), Path(cmd[4])
        pdf.write_bytes(payload)
        report.write_text(json.dumps({
            'source_sha256': hashlib.sha256(src.read_bytes()).hexdigest(),
            'pdf_sha256': hashlib.sha256(payload).hexdigest(),
            'pages': 1, 'engine': {'name': 'docx4j', 'version': '17.3.3'},
            'features': [], 'normalizations': [], 'elapsed_seconds': 0.1,
        }))
        return subprocess.CompletedProcess(cmd, 0, '{"ignored_stdout": true}', '')
    monkeypatch.setenv('STATION_DOCX_CONVERTER', DOCX_CONVERTER)
    monkeypatch.setenv('STATION_SOFFICE', SELECTED)
    monkeypatch.setattr(m, 'run_group', run)
    return calls, payload, run


def test_configured_docx_converter_is_exclusive_and_publishes_verified_metadata(m, client, poppler, docx_converter):
    data = docx(1)
    r = upload(client, data, name='Original.DOCX')
    assert r.status_code == 200, r.json
    assert r.json['pages'] == 1
    assert len(docx_converter[0]) == 1
    cmd, timeout, _ = docx_converter[0][0]
    assert timeout == 180
    assert cmd[0] == DOCX_CONVERTER
    d = Path(m.JOBS) / r.json['token']
    assert (d / 'src.docx').read_bytes() == data
    assert (d / 'doc.pdf').read_bytes() == docx_converter[1]
    meta = json.loads((d / 'conversion.json').read_text())
    assert meta['renderer'] == 'docx-print-converter/docx4j'
    assert meta['version'] == '17.3.3'
    assert meta['pages'] == 1
    assert meta['source_sha256'] == hashlib.sha256(data).hexdigest()
    assert meta['pdf_sha256'] == hashlib.sha256(docx_converter[1]).hexdigest()
    assert not (d / 'docx-converter.json').exists()


def test_configured_docx_converter_does_not_capture_other_office_formats(client, poppler, engine, monkeypatch):
    monkeypatch.setenv('STATION_DOCX_CONVERTER', DOCX_CONVERTER)
    r = upload(client, b'legacy document', name='report.doc')
    assert r.status_code == 200
    assert all(cmd[0] == SELECTED for cmd, _ in engine[0])


@pytest.mark.parametrize('field,value', [
    ('source_sha256', '0' * 64), ('pdf_sha256', '0' * 64), ('pages', 2),
    ('pages', True), ('engine', None), ('engine', {'name': 'unexpected', 'version': '1.0'}),
    ('engine', {'name': 'docx4j', 'version': ''}),
    ('engine', {'name': 'docx4j', 'version': 'private\nsource'}),
])
def test_docx_converter_report_mismatch_fails_without_legacy_fallback(m, client, poppler, docx_converter, monkeypatch, field, value):
    calls, _, run = docx_converter
    def bad(cmd, timeout, **kw):
        result = run(cmd, timeout, **kw)
        path = Path(cmd[4]); report = json.loads(path.read_text())
        report[field] = value; path.write_text(json.dumps(report))
        return result
    monkeypatch.setattr(m, 'run_group', bad)
    r = upload(client)
    assert r.status_code == 422
    assert len(calls) == 1
    assert not list(Path(m.JOBS).iterdir())
    assert 'reason=invalid_converter_report' in Path(m.EVENTS).read_text()


@pytest.mark.parametrize('report_bytes', [b'{broken', b'[]', b' ' * (64 * 1024 + 1)])
def test_docx_converter_report_malformed_or_oversized_is_rejected(m, client, poppler, docx_converter, monkeypatch, report_bytes):
    _, _, run = docx_converter
    def bad(cmd, timeout, **kw):
        result = run(cmd, timeout, **kw)
        Path(cmd[4]).write_bytes(report_bytes)
        return result
    monkeypatch.setattr(m, 'run_group', bad)
    assert upload(client).status_code == 422
    assert not list(Path(m.JOBS).iterdir())


def test_docx_converter_missing_report_is_rejected(m, client, poppler, docx_converter, monkeypatch):
    _, _, run = docx_converter
    def bad(cmd, timeout, **kw):
        result = run(cmd, timeout, **kw)
        Path(cmd[4]).unlink()
        return result
    monkeypatch.setattr(m, 'run_group', bad)
    assert upload(client).status_code == 422
    assert not list(Path(m.JOBS).iterdir())


def test_docx_converter_nonzero_partial_output_is_not_accepted(m, client, poppler, docx_converter, monkeypatch):
    calls, _, run = docx_converter
    def bad(cmd, timeout, **kw):
        result = run(cmd, timeout, **kw); result.returncode = 9
        return result
    monkeypatch.setattr(m, 'run_group', bad)
    assert upload(client).status_code == 422
    assert len(calls) == 1
    assert not list(Path(m.JOBS).iterdir())
    assert 'converter_exit status=9' in Path(m.EVENTS).read_text()


def test_docx_converter_actual_source_mutation_is_rejected(m, client, poppler, docx_converter, monkeypatch):
    _, _, run = docx_converter
    def bad(cmd, timeout, **kw):
        result = run(cmd, timeout, **kw)
        Path(cmd[1]).write_bytes(b'changed after reporting')
        return result
    monkeypatch.setattr(m, 'run_group', bad)
    assert upload(client).status_code == 422
    assert not list(Path(m.JOBS).iterdir())
    assert 'reason=source_changed' in Path(m.EVENTS).read_text()


def test_docx_converter_timeout_releases_admission_without_fallback(m, client, poppler, docx_converter, monkeypatch):
    calls, _, run = docx_converter
    def slow(cmd, timeout, **kw):
        calls.append((cmd, timeout, kw))
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(m, 'run_group', slow)
    assert upload(client).status_code == 422
    assert len(calls) == 1
    assert not m.office_lock.locked()
    monkeypatch.setattr(m, 'run_group', run)
    assert upload(client).status_code == 200
