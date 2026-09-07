#!/bin/bash
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then bash installers/install_unix.sh; else .venv/bin/python run.py; fi
read -r -p 'Press Enter to close.'
