# Third-party notices

The station's original application and packaging code are licensed under Apache License 2.0. Third-party components retain their own licenses. This source repository contains no private documents, commercial font binaries, or vendor scanner drivers.

## Document converter

The station image derives from [DOCX Print Converter](https://github.com/aezizhu/docx-print-converter). Its wrapper is Apache-2.0 licensed. Its runtime includes docx4j and an Apache FOP-derived renderer, ONLYOFFICE Community, OpenJDK, Poppler, and open fonts. Preserve that project's complete `THIRD_PARTY_NOTICES.md`, bundled license files, and corresponding-source information when redistributing the combined image.

ONLYOFFICE Community is AGPLv3 licensed and has additional upstream license conditions. Apache-2.0 licensing of the station does not relicense the converter or waive its obligations. The converter's generated font metadata adjustments do not modify font glyph files.

## Application and operating-system packages

The Docker build installs packages from Debian. Their exact copyright and license notices remain in `/usr/share/doc/<package>/copyright` and must be retained in redistributed images. Significant components include:

- Flask, Werkzeug, Jinja2, MarkupSafe, and related Python web dependencies.
- Pillow, NumPy, OpenCV, and img2pdf for image and PDF handling.
- CUPS and cups-filters for printer queues and filtering.
- SANE and sane-airscan for scanner access.
- Tesseract and its language data, plus OCRmyPDF for optional OCR processing.
- LibreOffice and unoconv for legacy Office formats that are not handled by the DOCX converter.
- Poppler, Ghostscript, qpdf, and other transitive PDF tools.
- `tini` for child-process reaping and signal delivery.

These components use a mixture of permissive, LGPL, GPL, AGPL, MPL, and font-specific licenses. This list is an inventory, not a substitute for the packages' exact licenses. Consult the installed notices and upstream projects before redistributing binaries or providing a network service.

## Optional vendor backend

HP's Unified Linux Driver, including the Samsung/HP `smfp` scanner backend, is proprietary vendor software and is **not included** in this repository or its default image. The optional helper only downloads an archive from HP and verifies its checksum. It does not accept the vendor license or install the driver.

If an operator separately installs that backend, the operator must review its license, supported platform, and redistribution conditions. Do not upload the driver archive, extracted libraries, or a private derivative image containing them to this repository.

## Fonts

Only open fonts supplied by the converter build and Debian package sources are included by default. No SimSun, SimHei, Microsoft YaHei, Calibri, or other font binary is copied from Microsoft Windows or Office. Font family names in compatibility rules identify requested document fonts; they are not redistributed font files.
