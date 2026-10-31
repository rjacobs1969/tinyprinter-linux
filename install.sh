#!/bin/sh
set -eu

if [ "$(id -u)" != 0 ]; then
    echo "Run with sudo: sudo ./install.sh AA:BB:CC:DD:EE:FF" >&2
    exit 1
fi
address="${1:?Usage: sudo ./install.sh BLUETOOTH-MAC [QUEUE-NAME]}"
queue="${2:-X6h}"
source_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
for tool in python3 gs lpadmin lpstat cupstestppd cups-config systemctl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing $tool. On Arch: sudo pacman -S cups cups-filters ghostscript python" >&2
        exit 1
    fi
done
python3 -c 'import re,sys; assert re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",sys.argv[1]), "Invalid MAC address"; assert re.fullmatch(r"[a-zA-Z0-9_-]+",sys.argv[2]), "Invalid queue name"' "$address" "$queue"
cupstestppd -q -I filters "$source_dir/cups/X6h.ppd"
systemctl enable --now cups.service
lpstat -r
if lpstat -p "$queue" >/dev/null 2>&1; then
    echo "Queue $queue already exists; refusing to change it." >&2
    exit 1
fi
python3 -m venv /opt/tinyprint/venv
/opt/tinyprint/venv/bin/python -m pip install "$source_dir"
serverbin="$(cups-config --serverbin)"
install -m 755 "$source_dir/cups/tinyprint-filter" "$serverbin/filter/tinyprint-filter"
install -m 700 "$source_dir/cups/tinyprint-backend" "$serverbin/backend/tinyprint"
cupstestppd -q "$source_dir/cups/X6h.ppd"
lpadmin -p "$queue" -E -v "tinyprint:///$address?channel=1" -P "$source_dir/cups/X6h.ppd" \
    -o PageSize=Roll100 -o printer-is-shared=false -o printer-error-policy=stop-printer
lpstat -p "$queue"
echo "Installed $queue. Print with: lp -d $queue -o PageSize=Roll100 document.pdf"
