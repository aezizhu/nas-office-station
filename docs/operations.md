# Configuration and operation

## Service model

The container runs one threaded Python application and one local CUPS daemon under `tini`. The entrypoint exits if either process stops, allowing the container restart policy to recover the service. CUPS listens only on the container loopback interface and Unix socket; only port 8071 is published.

The application uses in-memory locks and job state. Do not launch several independent application workers against the same scanner without designing shared coordination first. Office conversion admits one request at a time and rejects another with a retryable busy response.

CUPS requires privileges to configure and manage the local queue, so this compatibility image runs as root inside the container. It does not request Docker privileged mode, host networking, a Docker socket, or host device access. Use a dedicated container and volume. `/data` is private working storage; there is no blanket world-writable home directory.

## Startup

`PRINTER_URI` is mandatory. `STATION_PASSWORD` is mandatory unless the operator deliberately sets `ALLOW_ANONYMOUS=1` when launching the image directly. The provided Compose file always requires a password.

The default queue is created with Generic PostScript, A4 paper, and 600 dpi. Existing queues retain their paused state. If an administrator stopped a queue, investigate before re-enabling it. When using a different printer driver, check `lpoptions -p <queue> -l` rather than assuming that the requested options are supported.

The static scanner entry is generated from `SCANNER_NAME`, `SCANNER_PROTOCOL`, and `SCANNER_URL`. The defaults target the HP M437 WSD service. Do not assume another printer exposes that endpoint.

## Health and jobs

```sh
docker compose ps
docker compose logs --tail=100 office-station
docker compose exec office-station lpstat -r
docker compose exec office-station lpstat -o
docker compose exec office-station scanimage -L
```

The container health check verifies the application response and local CUPS scheduler. A disconnected printer or unavailable scanner can still leave the application healthy; the browser reports device state separately.

The status response has a build fingerprint and per-job state. Preserve a scan or print job identifier returned by the API and use it when polling. A global busy indicator is not the outcome of your particular job.

## Updating safely

1. Build the next image and run hardware-free tests in a disposable container.
2. Compare representative document output and inspect complete page images.
3. Wait until no scan, conversion, or print job is active. Check both station status and `lpstat -o`.
4. Record the current image ID and back up configuration. Do not terminate an in-flight scan, converter, or CUPS write.
5. Recreate the container with the new image, retaining the `/data` volume.
6. Check the new build fingerprint, logs, queue, and scanner discovery. Exercise the real upload path.
7. Keep the prior image until physical output acceptance is complete.

To roll back, stop accepting new work, wait for active jobs to finish, and recreate with the recorded prior image. Do not replace the data volume with an unrelated backup merely to change application versions.

## Retention and logs

The application is a short-lived document workspace. Download important scans before their 15-minute expiry. Do not use the working volume as a records-management system. Event logs are bounded and can contain filenames and operational details; keep access restricted and sanitize them before sharing a bug report.

No automatic external telemetry is required for conversion or printing. Network access is needed to the configured printer/scanner and, during image builds only, to package sources.
