#!/bin/sh
set -eu
python3 - <<'PY'
import base64, json, os
from urllib.request import Request, urlopen
headers = {}
password = os.environ.get('STATION_PASSWORD', '')
if password:
    raw = (os.environ.get('STATION_USER', 'office') + ':' + password).encode()
    headers['Authorization'] = 'Basic ' + base64.b64encode(raw).decode()
with urlopen(Request('http://127.0.0.1:8071/status', headers=headers), timeout=4) as response:
    data = json.load(response)
if not data.get('build'):
    raise SystemExit('Station response has no build identity')
PY
lpstat -r >/dev/null
