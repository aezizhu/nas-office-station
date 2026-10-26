# Lessons from building a reliable scan and print station

These are reusable engineering findings from the implementation and its tests. Examples are synthetic. No customer files, document identifiers, network inventory, credentials, raw logs or deployment backups are included.

The reference device is the **HP LaserJet MFP M437nda**. Findings about its WSD backend, feeder geometry and printer queue must be checked again for another model, firmware or backend. The links below identify the public code and regression tests that support each lesson. They do not replace physical acceptance on a configured device.

## 1. Define success at the layer you can actually observe

A station can return a successful HTTP response while the document is misrendered or the printer has no paper.

| Observation | What it establishes | What it does not establish |
|---|---|---|
| Health endpoint responds | Application is serving requests | Printer or scanner is available |
| Converter exits successfully | Process finished without a reported error | PDF is valid or correctly laid out |
| PDF opens and has the expected page count | Structural validity and page count | Complete text, correct positioning or intact images |
| CUPS returns a job ID | Scheduler accepted the job | Paper physically printed |
| Transport reports data delivered | Data reached the next layer | Correct physical output |
| Printed sheet is inspected | That physical sample passed | Every future document or configuration will pass |

The UI should say what is known: queued, transferring, blocked, canceled, result unknown, or data delivered. Do not turn uncertainty into a success message. A scheduler's list of finished jobs can include canceled or aborted jobs; inspect the job's state rather than treating membership in that list as success. A socket queue may expose less device-side evidence than an IPP implementation.

[CUPS documents `lp` as submission to the scheduler](https://openprinting.github.io/cups/doc/man-lp.html). The station's distinctions are exercised in [print-state tests](../tests/test_print_watch.py).

## 2. Requested resolution is not measured resolution

A backend can accept a resolution argument and return an image at another resolution. Labeling that image with the requested DPI changes its interpreted physical size without adding detail.

The conversion is simple:

```text
physical width in millimeters = pixel width / actual DPI * 25.4
```

For example, 1,000 pixels at 200 dpi represent 127 mm; labeling the same pixels as 300 dpi would incorrectly represent about 84.7 mm. This is a geometry error, not an image-quality upgrade.

Read the returned image metadata, retain that resolution through the PDF pipeline, and report a downgrade. On the tested M437nda WSD path, the observed ceiling was 200 dpi. Installing another backend requires a new measurement; it does not justify assuming its requested settings were delivered.

See [hardware notes](hardware.md) and [scan pipeline tests](../tests/test_run_scan_e2e.py).

## 3. Image dimensions cannot prove that paper edges were captured

A scanner can return a standard-sized canvas containing firmware margins, backing material or synthetic white filler. A PDF with A4 dimensions can therefore still omit marks at an edge.

Separate these questions:

1. What physical window was requested?
2. Which part of the sheet was inside that window?
3. What did firmware and the driver return?
4. What did application processing change?
5. What did the final PDF and physical print preserve?

Use a uniquely marked test sheet with visible features touching all four edges. Keep its raw frames privately and compare them with the delivered PDF. A page whose content starts well inside the margin cannot validate edge preservation. A successful unrelated scan cannot substitute for the intended calibration sheet.

A driver's apparent maximum width can also depend on its current left offset. Query the relevant source and geometry together before calling a conditional limit a hardware maximum. See [feeder edge tests](../tests/test_adf_edge.py) and [pipeline review tests](../tests/test_scan_pipeline_review.py).

## 4. Flatbed and feeder pages need different assumptions

A flatbed page may have a visible border against the surrounding platen. A feeder frame can instead be filled by the sheet, or include backing on only one side. A border detector that works on the flatbed can mistake text for the edge of a feeder page.

The normal feeder path therefore uses a configured capture window and read-only checks. It does not silently crop, rotate or discard pages. When a chosen window is too narrow, the result should request review or a wider capture rather than claim that post-processing recovered unseen content.

Mixed paper sizes, sideways sheets, colored paper, faint marks and content near each edge belong in acceptance tests. Synthetic frames are useful for deterministic regressions; real marked sheets are still necessary to establish device behavior.

## 5. Keep an original before applying enhancement

Background whitening, contrast enhancement, rotation, blank removal and JPEG recompression are separate transformations. A control labeled "off" is meaningful only if the actual production path bypasses the corresponding transform.

Keep original scan frames during the retention period. Use a lossless PDF path for the primary result and label a smaller lossy copy separately. A small signature or stamp can occupy very little area, so low coverage alone is not safe evidence that a page is blank. Colored paper also needs different treatment from a white background.

The tests check [color safety](../tests/test_color_safety.py), [small marks and blank-page handling](../tests/test_blank_ocr_errors.py), and [PDF publication](../tests/test_publish.py). These tests establish their stated pixel and artifact properties, not the meaning of a real document.

## 6. Preview and print scaling are different decisions

A correctly rendered PDF can still be printed at the wrong scale. Always fitting to the printable area shrinks ordinary documents; always printing at original size can clip content that reaches a printer's unprintable border.

Expose the choice. The station offers actual size with shrinking when necessary, and fit-to-printable-area. Scanned copies and authored documents can reasonably start with different defaults. Query the selected queue's real paper, duplex and resolution options rather than assuming a requested option took effect.

Document dimensions do not identify the paper loaded in a tray. Automatically selecting A3 media for an A3 PDF can cause a paper wait or clipping if the printer substitutes smaller paper. Uploads now keep the station's A4 default and explain the resulting scaling; A3 printing requires an explicit selection and a reminder to load matching paper. A printer's default-media attribute is also not proof of what is physically loaded. See the [paper-selection regression tests](../tests/test_print_paper_ui.py).

Test page selection, copies, orientation and scaling together. A page-range parser must reject invalid ranges before a real job is submitted. See [print options](../tests/test_print_options.py) and [page-selection tests](../tests/test_print_pages.py).

## 7. Job ownership and admission are correctness requirements

A shared "last result" lets one browser see another user's scan or print status. Acquire the resource lock before accepting a job, return that job's identifier, and poll its own state. If the resource is occupied, return an explicit busy response instead of accepting work that will later disappear.

Process-local locks are appropriate only while there is one application process. Adding several independent web workers changes that assumption. Shared storage alone does not make their locks or job dictionaries shared.

The current deployment intentionally uses one threaded application process. See [scan admission tests](../tests/test_scan_route.py), [recovery tests](../tests/test_recover.py), and [operations](operations.md).

## 8. A timeout must account for children and container supervision

Killing a command's immediate parent can leave a renderer or OCR worker running. Repeated failures then consume memory, process slots or listener ports even though the web request has ended.

Use bounded execution, process-group cleanup and a container init process. Treat application and CUPS lifecycle failures as service failures that the restart policy can recover. Keep process limits and memory limits, and avoid restarting in the middle of a scan, conversion or CUPS write.

Parent-death signaling and process groups have different boundaries; neither should be described as a complete sandbox. See [watchdog tests](../tests/test_scanimage_watchdog.py), [orphan-reaping tests](../tests/test_reaper.py), and [Docker's process-management guidance](https://docs.docker.com/engine/containers/multi-service_container/).

## 9. Test the production path and its real resource costs

Testing an unused helper does not validate the route that users call. Route tests should exercise upload, conversion, preview, selection and submission with guarded substitutes for physical hardware commands. A test that prints a success symbol without an assertion proves very little.

Image processing also needs realistic dimensions and data types. A float array uses more memory than the original byte image, and several intermediate copies can exceed a small NAS container's limit. Measure peak memory on representative sizes; reducing a display thumbnail does not fix a full-resolution allocation elsewhere.

Keep real printing and scanning opt-in. Report skipped hardware/sample tests explicitly. The [test harness](../tests/conftest.py) guards printer commands and network access, while [end-to-end scan tests](../tests/test_run_scan_e2e.py) model the device boundary.

## 10. Deploy and reproduce without exposing documents

Version the complete application and renderer bundle, preserve a rollback image and configuration, and verify the live build identity. An old browser tab can continue posting old defaults, so the station uses a build fingerprint to detect stale clients. A healthy restarted service still needs a real upload-path check.

For public reports, recreate the smallest layout or device condition with generated content. Filenames, document hashes, timestamps, printer serial numbers, image metadata and logs can identify an organization even when the page text is absent. Replacing a person's name is not sufficient anonymization. Do not upload proprietary driver binaries or fonts with the reproduction.

Use the [privacy policy](privacy.md), the [public-tree checker](../scripts/check_public_tree.py), and a manual review of the exact staged diff. Keep physical samples, reference PDFs, raw captures and deployment records outside the public repository.

## Related rendering findings

Word pagination, font metrics, theme-font resolution, document grids and renderer selection are covered in the converter's [lessons learned](https://github.com/aezizhu/docx-print-converter/blob/main/docs/lessons-learned.md). Matching page counts alone remains insufficient in both projects.
