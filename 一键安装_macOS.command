#!/bin/bash
cd "$(dirname "$0")"
bash installers/install_unix.sh
read -r -p 'Press Enter to close.'
