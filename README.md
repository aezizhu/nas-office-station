# NAS Office Station

A self-hosted scan and print web station for a local office network. It runs on a Linux NAS, turns scanner pages into downloadable PDFs, and previews uploaded documents before sending selected pages to a CUPS printer queue.

DOCX conversion uses the separate open-source [DOCX Print Converter](https://github.com/aezizhu/docx-print-converter) project. Conversion stays on the NAS and does not require a permanently running desktop computer, Microsoft Office, WPS, or a cloud conversion account.

## Features

- Flatbed, single-sided feeder, and duplex feeder scanning through SANE.
- Lossless PDF output from the scanner's PNG frames, plus a smaller optional email copy.
- Original scan frames retained alongside processed output during the retention period.
- Optional paper-background whitening, with conservative handling of colored paper.
- Read-only feeder edge checks that flag possible clipping without silently cropping the page.
- PDF, image, DOCX, and legacy Office uploads with previews and page selection.
- Actual-size or fit-to-printable-area output, paper size, copies, and duplex settings.
- Separate scan and print job state, conversion admission control, timeout cleanup, and persistent event logs.
- Password protection and same-origin checks for browser actions.

The browser interface and all repository documentation are maintained in English. The source repository contains no customer documents, scans, signatures, seals, credentials, private network addresses, or production logs.

## Tested hardware

The original hardware target is the **HP LaserJet MFP M437nda**, a monochrome multifunction printer. The tested CUPS path uses Generic PostScript over JetDirect (`socket://...:9100`) at **600 dpi**. A successful queue submission means that CUPS accepted the job; it does not prove that paper physically emerged.

The open `sane-airscan` WSD path on this printer was observed to deliver at most **200 dpi**. Higher-resolution scanning requires the optional HP/Samsung `smfp` backend and suitable device support. The vendor backend is not included in this repository or default image. The current web interface offers up to 300 dpi and reports the device's actual returned resolution.

The printer's firmware can leave unseen strips at scan edges. A full-size PDF does not prove that every physical edge was captured. Use a marked physical test sheet to validate your device, paper orientation, and feeder geometry. See [hardware notes](docs/hardware.md).

Other CUPS/SANE devices may work after configuration, but the model-specific feeder dimensions and edge checks must be validated before relying on them.

## Quick start

Requirements: an **amd64 Linux** Docker host, Docker Compose, a network printer/scanner, and at least 2 GiB available for this container. The image includes the converter's amd64 ONLYOFFICE runtime. ARM NAS devices are not currently supported by this build.

Build the converter image first:

```sh
git clone https://github.com/aezizhu/docx-print-converter.git
cd docx-print-converter
docker build -t docx-print-converter:0.1.0 .
cd ..
```

Build and start the station:

```sh
git clone https://github.com/aezizhu/nas-office-station.git
cd nas-office-station

# Documentation-only addresses: replace both values with your printer's address.
export PRINTER_URI='socket://192.0.2.10:9100'
export STATION_PRINTER_IP='192.0.2.10'
export STATION_USER='office'
read -r -s -p 'Station password: ' STATION_PASSWORD
printf '\n'
export STATION_PASSWORD

# Default bind is localhost. Set this explicitly for trusted office-LAN access.
export STATION_BIND_IP='0.0.0.0'
docker compose up -d --build
```

Open `http://YOUR_NAS:8071/` and sign in. All example IP addresses in this repository are reserved for documentation; none identify a real installation.

For an HTTPS reverse proxy, keep the default localhost binding and publish only the proxy. HTTP Basic authentication over plain HTTP does not encrypt the password or documents. Do not publish port 8071 directly to the Internet.

Configuration is supplied through shell variables or the NAS container manager. Do not commit passwords or create a public configuration file containing them.

## Printer and scanner configuration

| Variable | Purpose | Default |
|---|---|---|
| `PRINTER_URI` | CUPS destination; required | No default |
| `STATION_PRINTER_IP` | Device address for status checks; required in Compose | No default in Compose |
| `STATION_QUEUE` | CUPS queue name | `M437` |
| `PRINTER_DRIVER` | CUPS model identifier | `drv:///sample.drv/generic.ppd` |
| `SCANNER_NAME` | Static SANE airscan display name | `HP M437` |
| `SCANNER_PROTOCOL` | `wsd` or `escl` | `wsd` |
| `SCANNER_URL` | Explicit SANE endpoint | `http://<printer>:8018/wsd/scan` |
| `STATION_DEV` | SANE device identifier selected by the application | `airscan:w0:HP M437` |
| `STATION_USER` | Browser login username | `office` |
| `STATION_PASSWORD` | Browser login password | Required |
| `STATION_BIND_IP` | Host interface used by Compose | `127.0.0.1` |
| `STATION_PORT` | Host HTTP port | `8071` |
| `CONVERTER_IMAGE` | Locally built converter base image | `docx-print-converter:0.1.0` |

For an IPP printer that supports driverless printing, set an appropriate `ipp://` or `ipps://` URI and `PRINTER_DRIVER=everywhere`. Verify that queue's real capabilities; the M437nda's tested Generic PostScript settings are not universal.

Docker bridge networking often prevents multicast scanner discovery. The entrypoint writes a static airscan endpoint instead. Find the actual identifier with:

```sh
docker compose exec office-station scanimage -L
docker compose exec office-station lpstat -p M437 -l
```

## Data and retention

The `station-data` volume mounts at `/data`. The application uses `/data/scans` and `/data/printjobs`; event records are stored in `/data/scans/events.log` and downloadable through the authenticated web interface.

Scans, originals, and print previews are temporary: the default retention period is **15 minutes**. Download important output promptly. Print-job lifetime is refreshed by use of its previews. This volume is working storage, not a document archive. Back up application configuration and image versions separately.

## Verification before normal use

1. Upload a known PDF and inspect every preview page. Confirm page selection and paper size.
2. Upload representative DOCX files and compare with PDF output from their authoring application. Matching page counts alone are insufficient; inspect tables, headers, footers, and images.
3. Scan a marked physical page and compare the original scan, resulting PDF, actual resolution, orientation, and all four edges.
4. Print one approved test document and inspect the physical paper. A healthy HTTP endpoint or completed CUPS transfer does not replace this check.

The DOCX converter improves specific compatibility classes; it cannot guarantee perfect rendering of every Word file. Keep the preview step and use an authoring-application PDF for critical layouts that fail review.

## Operations and development

- [Configuration and operation](docs/operations.md)
- [Hardware and optional HP backend](docs/hardware.md)
- [API outline](docs/api.md)
- [Privacy and contribution policy](docs/privacy.md)

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

System tools such as Poppler, CUPS, SANE, Tesseract, and the Office converters are also required for their integration tests. Test guards must prevent unit tests from sending real print or scan jobs. Use a disposable container for hardware-free regression tests.

## Release validation

The initial release passed **685 automated tests** with 8 explicitly skipped optional tests, plus lint and public-source boundary checks. A clean Docker build also passed startup, CUPS readiness, health-check authentication, and anonymous-access rejection in a network-isolated CI environment. These checks do not replace physical scan and print acceptance on a configured device.

## License

Project code is licensed under Apache License 2.0. Runtime components retain their own licenses, including AGPLv3 for the converter's ONLYOFFICE Community component. The proprietary HP/Samsung scanner driver is optional and is not redistributed. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
