#!/usr/bin/env python3
"""Reject accidentally tracked documents, binary drivers, secrets and private IPs."""
import ipaddress
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
forbidden = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.png', '.jpg',
             '.jpeg', '.tif', '.tiff', '.zip', '.gz', '.deb', '.so', '.pem', '.key', '.log', '.jsonl'}
private = [ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
errors = []
for name in filter(None, tracked):
    path = root / name
    if path.suffix.lower() in forbidden or any(part in {'data', 'backups', 'vendor'} for part in path.parts) or path.name.startswith('.env'):
        errors.append(name + ': forbidden artifact type')
        continue
    text = path.read_text(encoding='utf-8')
    if name == 'scripts/check_public_tree.py':
        continue
    if path.suffix == '.md' and re.search('[\u3400-\u9fff]', text):
        errors.append(name + ': documentation must be English')
    for match in re.finditer(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', text):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if any(address in net for net in private):
            errors.append(name + ': private network literal; use a documentation address')
    if re.search(r'/Users/[^/\s]+/|/home/(?!converter/)[^/\s]+/|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----', text):
        errors.append(name + ': private workspace or key marker')
if errors:
    print('\n'.join(sorted(set(errors))), file=sys.stderr)
    sys.exit(1)
print('Public source boundary checks passed')
