#!/bin/bash
# One-shot setup for the Kona plug-in reminder. Safe to re-run.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
ALERT=$(grep '^ntfy_topic' plugcheck.cfg | cut -d= -f2 | tr -d ' ')
ARRIVE=$(grep '^arrival_topic' plugcheck.cfg | cut -d= -f2 | tr -d ' ')

echo "== 1. Python environment"
chmod +x run_plugcheck.sh kona_plug_check.py kona_arrival_listener.py enroll.py
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip hyundai_kia_connect_api matplotlib
echo "ok"

echo; echo "== 2. Verifying this PC with Hyundai (one-time code, first run only)"
if [ ! -f token.json ]; then
  ./venv/bin/python enroll.py || { echo "!! Verification didn't finish. Paste the output to Claude."; exit 1; }
else
  echo "already verified"
fi

echo; echo "== 2b. Reading the car from BlueLink (one request)"
if ./run_plugcheck.sh --status; then echo "ok"; else
  echo "!! Could not read the car. Don't re-run; paste the output to Claude."; exit 1; fi

echo; echo "== 3. Sending a test alert to ntfy topic: $ALERT"
./run_plugcheck.sh --test && echo "(subscribe to that topic in the ntfy app to see it)"

echo; echo "== 4. Starting the arrival listener"
mkdir -p ~/.config/systemd/user
cp kona-arrival.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kona-arrival
loginctl enable-linger "$USER" 2>/dev/null || echo "(couldn't enable linger; the listener runs while you're logged in)"
sleep 2
systemctl --user --no-pager status kona-arrival | head -5

echo
echo "================ DONE ================"
echo "Phone settings (both phones):"
echo "  ntfy app -> subscribe to:   $ALERT"
echo "  MacroDroid HTTP POST URL:   https://ntfy.sh/$ARRIVE"
echo
echo "End-to-end test: curl -d YourName https://ntfy.sh/$ARRIVE"
echo "   -> about 10 min later an alert arrives. Watch with: tail -f $DIR/plugcheck.log"
