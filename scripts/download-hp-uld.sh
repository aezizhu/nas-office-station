#!/bin/sh
# Download only. This does not accept HP's license, extract, or install anything.
set -eu
if [ "$#" -ne 1 ]; then
  echo 'Usage: download-hp-uld.sh PRIVATE_OUTPUT_DIRECTORY' >&2
  exit 64
fi
url='https://ftp.hp.com/pub/softlib/software13/printers/IPG/M433/uld-hp.tar.gz'
expected='2757c5db7f10bc8109043950ab864b00fb122d82ed81efde3acac18389218a36'
mkdir -p "$1"
target="$1/uld-hp.tar.gz"
if [ -e "$target" ]; then
  echo 'The target already exists; choose another directory or verify it manually.' >&2
  exit 1
fi
temporary="$(mktemp "$1/.uld-download.XXXXXX")"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
curl -fL --retry 3 --connect-timeout 15 --max-time 300 "$url" -o "$temporary"
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$temporary" | cut -d ' ' -f 1)"
else
  actual="$(shasum -a 256 "$temporary" | cut -d ' ' -f 1)"
fi
if [ "$actual" != "$expected" ]; then
  echo 'HP archive checksum changed. Stop and review the new vendor package before using it.' >&2
  exit 1
fi
chmod 600 "$temporary"
mv "$temporary" "$target"
printf 'Downloaded and verified: %s\nSHA-256: %s\n' "$target" "$actual"
printf '%s\n' 'Review the vendor license and instructions before installing. No installation was performed.'
