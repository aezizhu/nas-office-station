# API outline

The web interface is the primary client. Routes use the same HTTP Basic credentials when authentication is enabled. Browser POST requests must originate from the station's own origin. This outline describes the existing interface; it is not a promise of permanent versioned API stability.

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Web application |
| `/status` | GET | Build, device state, conversion activity, and requested job state |
| `/files` | GET | Currently retained scan PDFs and metadata |
| `/upload` | POST | Multipart `file`; convert and prepare print previews |
| `/preview/<token>?p=0` | GET | Zero-based page preview; `hi=1` requests higher resolution |
| `/scan` | POST | Start one scan job with current build fingerprint |
| `/print` | POST | Submit selected pages of a prepared document |
| `/copy` | POST | Load a retained scan PDF into the print preview flow |
| `/cancel` | POST | Request cancellation of a print job owned by its job token |
| `/delete` | POST | Delete a retained scan and associated variants |
| `/f/<name>` | GET | Download a retained scan PDF |
| `/small/<name>` | GET | Download an available smaller email copy |
| `/raw/<name>` | GET | Download the retained original scan representation |
| `/events.log` | GET | Download bounded operational events |

## Upload and print

Upload a document as multipart field `file`. A successful response supplies a document token, name, actual PDF page count, size label, and optional review information. Inspect every page before printing. A review notice can indicate page-count differences or potentially unstable floating layout; saved DOCX page metadata is not an authoritative target count.

Print parameters include `token`, `paper`, `sides`, `scale`, `copies`, and optional `pages`. Duplex values are `None`, `DuplexNoTumble`, and `DuplexTumble`. A blank `pages` field means all pages. A successful submission returns a job identifier; poll `/status?print=<job>` for that job. Submission is distinct from physical completion.

## Scanning

First obtain the current `build` value from `/status`. Include it in `/scan` so an outdated browser cannot unknowingly submit obsolete options.

Core fields are `source` (`Flatbed`, `ADF`, or `ADF Duplex`), `res`, `mode` (`Color` or `Gray`), `size`, and `paper_white`. A successful start returns a scan job identifier; poll `/status?scan=<job>`. The scanner is exclusive and may reject a second request while busy.

The current web flow keeps cropping, rotation, darkening, blank-page removal, and OCR out of its normal scan controls. Legacy API options remain for compatibility and require their own acceptance testing; they must not be inferred from a healthy default scan.

## Failure handling

Check both the HTTP status and JSON response. Busy Office conversion returns HTTP 429. Unsupported or damaged uploads and conversion failures return an error instead of a usable document token. Do not infer success from a subprocess exit or a nonempty temporary file. Tokens and original filenames should be removed before sharing diagnostic logs publicly.
