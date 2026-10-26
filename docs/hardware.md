# Hardware notes

## HP LaserJet MFP M437nda

This is the tested reference device. It prints in monochrome. The tested queue uses Generic PostScript over JetDirect on port 9100 and a 600 dpi setting. Paper size and duplex options depend on the selected CUPS driver and device configuration.

Printing through a socket queue provides limited device-side job reporting. CUPS accepting or transferring a job does not prove physical completion. Check the printer panel for missing paper, jams, open covers, or other device conditions.

## Scanner geometry

The flatbed and feeder have different physical behavior. The M437 feeder is centered, and its firmware may replace narrow strips near scan-window edges with white pixels. The driver can also append white filler after the sheet ends. Neither the returned image dimensions nor PDF page dimensions prove that the whole physical sheet was captured.

The application requests extra feeder room for supported standard sizes and performs a read-only edge check. It does not automatically crop, rotate, or remove pages in the normal scan flow. The optional paper-white adjustment keeps original scan frames available during the retention period. Use a physical calibration sheet with identifiable marks touching all four edges when validating a new unit or paper orientation.

A sideways A4 sheet can be wider than the selected A4 scan window. Feed standard A4 in the intended orientation or choose the full scan area. A3 or other full-width sheets may encounter inherent firmware edge limitations.

## Open WSD backend

The default image includes `sane-airscan`. On the tested M437nda, its WSD path delivered at most 200 dpi. A request for a larger resolution must not be treated as evidence that the returned image actually has that resolution. The application reads returned image metadata and reports a downgrade when needed.

For another scanner, query its options directly:

```sh
docker compose exec office-station scanimage -L
docker compose exec office-station scanimage -d 'YOUR_SANE_DEVICE' -A
```

Static endpoints are configured through `SCANNER_URL`; no printer web-admin password is required for ordinary WSD scanning.

## Optional HP/Samsung `smfp` backend

Higher-resolution scanning on this model can use HP's Unified Linux Driver `smfp` backend. This is vendor software, not part of this open-source project. The default image and repository do not contain or install it.

The helper below only downloads and verifies an archive from HP's official distribution. It does not accept a license, extract executable files, or run an installer:

```sh
./scripts/download-hp-uld.sh /path/to/private-downloads
```

Review the vendor's license and supported-platform requirements yourself. If you decide to install it, do so in a separate private derivative image or disposable test container using HP's own instructions. Retain that private image and its license records; do not commit the driver binaries or package to this repository.

The application detects a `smfp` backend when it is installed and falls back to WSD if that path is unavailable. Confirm the actual supported resolutions and scan geometry with the real device. The current web interface caps requests at 300 dpi even if a backend exposes higher values.

A vendor-driver installation does not establish that edge clipping, feeder positioning, or all scan modes are correct. Repeat the marked-page physical test and keep raw frames for comparison.
