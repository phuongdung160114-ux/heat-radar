#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY=""
for cmd in python3.13 python3.12 python3.11 python3; do
  if command -v "$cmd" >/dev/null 2>&1 && "$cmd" -c 'import sys;sys.exit(0 if (3,11)<=sys.version_info[:2]<=(3,13) else 1)' 2>/dev/null; then PY="$(command -v "$cmd")"; break; fi
done
if [[ -z "$PY" && "$(uname -s)" == Darwin ]]; then
  echo 'A compatible Python is missing. Downloading the official Python.org package.'
  echo 'macOS will request your administrator password to install Python.'
  PKG="${TMPDIR:-/tmp}/ashare-python-3.12.10.pkg"
  curl --fail --location --retry 2 --proto '=https' --tlsv1.2 -o "$PKG" https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg
  EXPECTED='8f4989592c9412e51fcad70273442c22'
  [[ "$(md5 -q "$PKG")" == "$EXPECTED" ]] || { echo 'Official release checksum mismatch. Stopped.'; exit 1; }
  pkgutil --check-signature "$PKG" || { echo 'Package signature invalid. Stopped.'; exit 1; }
  sudo /usr/sbin/installer -pkg "$PKG" -target /
  PY=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
fi
if [[ -z "$PY" ]]; then
  echo 'Linux requires Python 3.11-3.13 and its venv module. Install them with your system package manager, then rerun this script.'
  echo 'The Windows installer includes automatic Python installation; this Linux helper does not modify system packages.'
  exit 1
fi
mkdir -p data/logs
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
if [[ "$(uname -s)" == Darwin ]] && ! .venv/bin/python -c 'import lightgbm' 2>/dev/null; then
  if command -v brew >/dev/null 2>&1; then brew install libomp; fi
fi
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze > data/logs/installed-packages.txt
exec .venv/bin/python run.py
