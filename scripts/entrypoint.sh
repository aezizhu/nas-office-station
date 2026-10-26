#!/bin/bash
# Run CUPS and the single-process station application under the container's init.
set -euo pipefail
umask 027

if [[ -z "${STATION_PASSWORD:-}" && "${ALLOW_ANONYMOUS:-0}" != 1 ]]; then
  echo 'STATION_PASSWORD is required. Set ALLOW_ANONYMOUS=1 only for a deliberate trusted-LAN deployment.' >&2
  exit 64
fi
: "${PRINTER_URI:?Set PRINTER_URI to your printer URI}"
export STATION_QUEUE="${STATION_QUEUE:-M437}"
export STATION_USER="${STATION_USER:-office}"
export STATION_DEV="${STATION_DEV:-airscan:w0:HP M437}"
export HOME=/data
export STATION_PRINTER_IP="${STATION_PRINTER_IP:-}"

# Validate inputs before passing them to CUPS or generating its configuration.
STATION_PRINTER_IP="$(python3 - <<'PY'
import os, re
from urllib.parse import urlsplit
uri = urlsplit(os.environ['PRINTER_URI'])
if uri.scheme not in ('socket', 'ipp', 'ipps', 'lpd') or not uri.hostname or uri.username or uri.password:
    raise SystemExit('PRINTER_URI must be a socket, ipp, ipps, or lpd URI without credentials')
queue = os.environ['STATION_QUEUE']
if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', queue):
    raise SystemExit('STATION_QUEUE contains unsupported characters')
host = os.environ.get('STATION_PRINTER_IP') or uri.hostname
if not re.fullmatch(r'[A-Za-z0-9.:-]{1,253}', host):
    raise SystemExit('Invalid printer host')
print(host)
PY
)"
export STATION_PRINTER_IP
mkdir -p /data/scans /data/printjobs /data/printjobs/tmp /run/cups /var/spool/cups/tmp
chmod 750 /data /data/scans /data/printjobs /data/printjobs/tmp
chown root:lp /var/spool/cups/tmp
chmod 1770 /var/spool/cups/tmp

# A fixed SANE endpoint works in Docker bridge networking, where multicast
# discovery may not cross the bridge. Override SCANNER_URL for other devices.
python3 - <<'PY'
import os
from pathlib import Path
from urllib.parse import urlsplit
name = os.environ.get('SCANNER_NAME', 'HP M437')
protocol = os.environ.get('SCANNER_PROTOCOL', 'wsd')
host = os.environ['STATION_PRINTER_IP']
url_host = '[' + host + ']' if ':' in host else host
url = os.environ.get('SCANNER_URL') or 'http://' + url_host + ':8018/wsd/scan'
parsed = urlsplit(url)
if protocol not in ('wsd', 'escl') or parsed.scheme not in ('http', 'https') or not parsed.hostname:
    raise SystemExit('Invalid SCANNER_PROTOCOL or SCANNER_URL')
if parsed.username or parsed.password or any(c in name + url for c in '\r\n"'):
    raise SystemExit('Scanner configuration cannot contain credentials, quotes, or line breaks')
Path('/etc/sane.d/airscan.conf').write_text('[devices]\n"' + name + '" = ' + url + ', ' + protocol + '\n', encoding='utf-8')
dll = Path('/etc/sane.d/dll.conf')
lines = dll.read_text().splitlines() if dll.exists() else []
if 'airscan' not in (line.strip() for line in lines):
    dll.write_text('\n'.join(lines) + '\nairscan\n', encoding='utf-8')
PY

# CUPS is reachable only inside this container. Only the station's HTTP port is
# published; printer administration is performed with docker compose exec.
cat > /etc/cups/cupsd.conf <<'CUPS'
LogLevel warn
Listen localhost:631
Listen /run/cups/cups.sock
Browsing Off
WebInterface No
DefaultAuthType Basic
DefaultEncryption IfRequested
<Location />
  Order allow,deny
  Allow localhost
</Location>
<Location /admin>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
  Allow localhost
</Location>
CUPS

cups_pid=''
app_pid=''
# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2329
stop_children() {
  [[ -z "$app_pid" ]] || kill -TERM "$app_pid" 2>/dev/null || true
  [[ -z "$cups_pid" ]] || kill -TERM "$cups_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap stop_children EXIT
trap 'exit 143' TERM INT

env HOME=/var/spool/cups TMPDIR=/var/spool/cups/tmp cupsd -f &
cups_pid=$!
ready=0
for _ in {1..50}; do
  if lpstat -r >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "$cups_pid" 2>/dev/null || break
  sleep 0.1
done
if [[ "$ready" != 1 ]]; then
  echo 'CUPS did not become ready.' >&2
  exit 1
fi

if ! lpstat -p "$STATION_QUEUE" >/dev/null 2>&1; then
  lpadmin -p "$STATION_QUEUE" -E -v "$PRINTER_URI" \
    -m "${PRINTER_DRIVER:-drv:///sample.drv/generic.ppd}" \
    -o printer-is-shared=false -o printer-error-policy=retry-job -o PageSize=A4 -o Resolution=600dpi
else
  # Keep an existing queue's paused state. An administrator may have stopped it.
  lpadmin -p "$STATION_QUEUE" -v "$PRINTER_URI" -o printer-is-shared=false
fi
lpadmin -d "$STATION_QUEUE"
python3 /opt/nas-office-station/app/scan_app.py &
app_pid=$!
set +e
wait -n "$cups_pid" "$app_pid"
result=$?
set -e
# An unexpectedly successful child exit is still a service failure.
[[ "$result" != 0 ]] || result=1
exit "$result"
