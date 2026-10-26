"""Bounded Office conversion with explicit engine choice and no source rewriting."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import time
import tempfile
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


MAX_PROPERTIES_BYTES = 64 * 1024
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_DIRECTORY_BYTES = 2 * 1024 * 1024
MAX_ZIP_ENTRIES = 4096
MAX_CONVERTER_REPORT_BYTES = 64 * 1024


def selected_soffice():
    """Only an explicitly validated engine selection replaces the legacy renderer."""
    return os.environ.get('STATION_SOFFICE', '').strip() or None


class InspectionLimit(ValueError):
    pass


def _bounded_archive(src):
    """Bound the central directory before ZipFile eagerly constructs its entry objects."""
    with open(src, 'rb') as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 65577))  # EOCD + maximum ZIP comment + preceding ZIP64 locator
        tail = fh.read(65577)
    offset = tail.rfind(b'PK\x05\x06')
    if offset < 0 or len(tail) - offset < 22:
        raise BadZipFile('missing ZIP directory')
    _, disk, start_disk, disk_entries, entries, directory_size, directory_offset, comment = struct.unpack_from('<4s4H2LH', tail, offset)
    if offset + 22 + comment != len(tail):
        raise BadZipFile('invalid ZIP directory')
    if (disk or start_disk or disk_entries != entries or entries > MAX_ZIP_ENTRIES
            or directory_size > MAX_DIRECTORY_BYTES or directory_offset == 0xffffffff
            or (offset >= 20 and tail[offset - 20:offset - 16] == b'PK\x06\x07')):
        raise InspectionLimit('ZIP metadata inspection limit')
    if directory_offset + directory_size > size - len(tail) + offset:
        raise BadZipFile('invalid ZIP directory range')
    return ZipFile(src)


def _xml(raw):
    declarations = raw.replace(b'\x00', b'').upper()  # includes UTF-16/32 declarations
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise ValueError('XML declarations are not inspected')
    return ET.fromstring(raw)


def _page_hint(archive):
    try:
        entry = archive.getinfo('docProps/app.xml')
        if entry.file_size > MAX_PROPERTIES_BYTES:
            return None
        root = _xml(archive.read(entry))
        value = root.findtext('{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Pages')
        count = int(value or '')
        return count if 1 <= count <= 100000 else None
    except Exception:
        # Cached properties are optional: unknown encodings/compression or corrupt
        # metadata cannot decide whether the actual Office renderer can open a file.
        return None


def _layout_risks(archive):
    try:
        entry = archive.getinfo('word/document.xml')
    except KeyError:
        return []
    if entry.file_size > MAX_DOCUMENT_BYTES:
        return ['inspection_limited']
    try:
        root = _xml(archive.read(entry))
        tags = {node.tag for node in root.iter()}
        risks = []
        if '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblpPr' in tags:
            risks.append('floating_tables')
        if '{urn:schemas-microsoft-com:vml}textbox' in tags:
            risks.append('legacy_textboxes')
        return risks
    except Exception:
        return ['layout_not_checked']


def inspect_docx(src):
    """Return advisory metadata only; never change the document or limit output pages."""
    if Path(src).suffix.lower() != '.docx':
        return None, []
    try:
        with _bounded_archive(src) as archive:
            return _page_hint(archive), _layout_risks(archive)
    except InspectionLimit:
        return None, ['inspection_limited']
    except Exception:
        return None, ['layout_not_checked']


def page_quality(hint, pages, risks=()):
    issues = []
    if hint is not None and hint != pages:
        issues.append('The saved document records %d pages; this conversion produced %d. The saved count may be stale, '
                      'or the layout may differ. Check every preview page.' % (hint, pages))
    if any(risk in risks for risk in ('floating_tables', 'legacy_textboxes')):
        issues.append('The document contains floating tables or legacy text boxes. Their position or pagination may differ from Word/WPS; '
                      'check text, tables and seals on every page even when the page count matches.')
    if any(risk in risks for risk in ('inspection_limited', 'layout_not_checked')):
        issues.append('Automatic layout inspection was incomplete. Check every preview page.')
    if not issues:
        return None
    issues.append('If the preview differs from Word/WPS, export a PDF from the original application and upload it.')
    return {'kind': 'office_conversion', 'state': 'review', 'issues': issues}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for data in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def _version(executable, run):
    try:
        result = run([executable, '--version'], timeout=10)
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip().splitlines()[0][:120]
    except Exception:
        pass
    return 'unknown'


class OfficeConversionError(RuntimeError):
    """A fixed diagnostic code that cannot contain converter output or source text."""
    def __init__(self, reason, status=None):
        self.reason = reason
        self.status = status
        super().__init__(reason if status is None else '%s status=%d' % (reason, status))


def _failure_reason(exc):
    if isinstance(exc, OfficeConversionError):
        return exc.reason + ('' if exc.status is None else ' status=%d' % exc.status)
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'timeout'
    if isinstance(exc, FileNotFoundError):
        return 'missing_executable_or_file'
    if isinstance(exc, OSError):
        return 'os_error errno=%s' % exc.errno
    return 'unexpected_%s' % type(exc).__name__


def _validate(result, path, pdf_info):
    if result.returncode != 0:
        raise OfficeConversionError('converter_exit', result.returncode)
    if not path.is_file() or path.stat().st_size == 0:
        raise OfficeConversionError('missing_pdf')
    try:
        return pdf_info(str(path))[0]
    except subprocess.TimeoutExpired:
        raise
    except Exception as exc:
        raise OfficeConversionError('invalid_pdf') from exc


def _converter_report(path, source_hash, pdf_hash, pages):
    """Accept only evidence tied to the independently validated input and PDF."""
    try:
        with open(path, 'rb') as fh:
            raw = fh.read(MAX_CONVERTER_REPORT_BYTES + 1)
        if len(raw) > MAX_CONVERTER_REPORT_BYTES:
            raise ValueError('Report size limit')
        report = json.loads(raw)
        if not isinstance(report, dict):
            raise ValueError('Report must be an object')
        engine = report.get('engine')
        if (report.get('source_sha256') != source_hash or report.get('pdf_sha256') != pdf_hash
                or type(report.get('pages')) is not int or report['pages'] != pages
                or not isinstance(engine, dict) or engine.get('name') not in ('docx4j', 'onlyoffice')
                or not isinstance(engine.get('version'), str)
                or not re.fullmatch(r'[A-Za-z0-9 ._()+:-]{1,120}', engine['version'])):
            raise ValueError('Report does not match validated artifacts')
        return engine['name'], engine['version']
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise OfficeConversionError('invalid_converter_report') from exc


def convert_office(src, pdf, run, pdf_info, log_event):
    """Return conversion evidence after validating the PDF; retain source bytes unchanged.

    The caller owns admission control and timeout-safe process groups. A selected
    engine never falls back to the old resident listener. Legacy installations keep
    their listener/cold-start sequence, rejecting and deleting partial outputs first.
    """
    started = time.monotonic()
    src, pdf = Path(src), Path(pdf)
    temporary_pdf = pdf.parent / (src.stem + '.pdf')
    listener_pdf = pdf.parent / 'listener.pdf'
    docx_pdf = pdf.parent / 'docx-converter.pdf'
    docx_report = pdf.parent / 'docx-converter.json'
    docx_converter = (os.environ.get('STATION_DOCX_CONVERTER', '').strip()
                      if src.suffix.lower() == '.docx' else '')
    selected = selected_soffice()
    engine = 'docx-print-converter' if docx_converter else selected or 'unoconv'
    version = ('unknown' if docx_converter else
               _version(selected, run) if selected else 'unknown (resident listener)')
    hint, risks = inspect_docx(src)
    meta = {'renderer': engine[:256], 'version': version, 'source_sha256': _sha256(src),
            'source_pages_hint': hint, 'layout_risks': risks}

    def clear_outputs():
        for p in {pdf, temporary_pdf, listener_pdf, docx_pdf, docx_report}:
            p.unlink(missing_ok=True)

    try:
        clear_outputs()
        pages = None
        if docx_converter:
            # This is an explicit DOCX route. A failure must not silently
            # repaginate the document with a different legacy renderer.
            result = run([docx_converter, str(src), str(docx_pdf), '--report', str(docx_report)], timeout=180)
            pages = _validate(result, docx_pdf, pdf_info)
            if _sha256(src) != meta['source_sha256']:
                raise OfficeConversionError('source_changed')
            name, version = _converter_report(docx_report, meta['source_sha256'], _sha256(docx_pdf), pages)
            engine = 'docx-print-converter/' + name
            meta.update(renderer=engine, version=version)
            os.replace(docx_pdf, pdf)
            docx_report.unlink()
        elif not selected:
            try:
                result = run(['unoconv', '-f', 'pdf', '-o', str(listener_pdf), str(src)], timeout=90)
                pages = _validate(result, listener_pdf, pdf_info)
                os.replace(listener_pdf, pdf)
            except Exception:
                clear_outputs()
        if pages is None:
            engine = selected or 'soffice'
            version = version if selected else _version(engine, run)
            meta.update(renderer=engine[:256], version=version)
            profile = tempfile.mkdtemp(prefix='lo-profile-', dir=str(pdf.parent))
            try:
                result = run([engine, '-env:UserInstallation=' + Path(profile).as_uri(),
                              '--headless', '--convert-to', 'pdf', '--outdir', str(pdf.parent), str(src)],
                             timeout=180, env=dict(os.environ, HOME=str(pdf.parent)))
                pages = _validate(result, temporary_pdf, pdf_info)
                os.replace(temporary_pdf, pdf)
            finally:
                shutil.rmtree(profile, ignore_errors=True)
        meta.update(pages=pages, pdf_sha256=_sha256(pdf), elapsed_seconds=round(time.monotonic() - started, 3))
        log_event('office-conversion', 'outcome=ok renderer=%s version=%s pages=%d elapsed=%.3f source_sha256=%s pdf_sha256=%s' %
                  (engine, version, pages, meta['elapsed_seconds'], meta['source_sha256'], meta['pdf_sha256']))
        return meta
    except Exception as exc:
        clear_outputs()
        log_event('office-conversion', 'outcome=failed renderer=%s version=%s reason=%s elapsed=%.3f source_sha256=%s' %
                  (engine, version, _failure_reason(exc), time.monotonic() - started, meta['source_sha256']))
        raise
