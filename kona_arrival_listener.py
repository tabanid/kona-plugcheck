#!/usr/bin/env python3
"""
Waits for the phone's "arrived home" signal on an ntfy topic, then, after
delay_minutes, makes ONE fresh BlueLink request and alerts if the Kona is
home but not plugged in. Runs continuously (systemd user service).

Several signals within the delay (Bluetooth flapping) collapse into one check.
"""
import json
import threading
import time
import urllib.request

import kona_plug_check as k

A = k.CFG["arrival"]
TOPIC = A["arrival_topic"]
DELAY_S = A.getfloat("delay_minutes", 10) * 60
SERVER = k.P.get("ntfy_server", "https://ntfy.sh").rstrip("/")

MIN_GAP_S = A.getfloat("min_minutes_between_checks", 30) * 60
MAX_PER_DAY = A.getint("max_checks_per_day", 6)
HIST = k.HERE / "arrival_checks.json"

_timer = None
_who = set()
_lock = threading.Lock()


def allowed():
    """Rate cap so stray or fake signals can't burn BlueLink requests."""
    now = time.time()
    try:
        hist = [t for t in json.loads(HIST.read_text()) if now - t < 86400]
    except Exception:
        hist = []
    if hist and now - hist[-1] < MIN_GAP_S:
        k.log(f"skipped: last check was {(now-hist[-1])/60:.0f} min ago")
        return False
    if len(hist) >= MAX_PER_DAY:
        k.log(f"skipped: already {len(hist)} checks in the last 24 h")
        return False
    HIST.write_text(json.dumps(hist + [now]))
    return True


def run_check():
    global _timer
    with _lock:
        _timer = None
        who = ", ".join(sorted(_who))
        _who.clear()
    if not allowed():
        return
    try:
        k.log("arrival check: asking BlueLink (one forced refresh)")
        # every arrival counts, not just one per night
        k.check(force=True, once_per_night=False, always_report=True, who=who)
    except Exception as e:
        k.log(f"arrival check ERROR: {e!r}")


def on_arrival(msg):
    global _timer
    with _lock:
        if msg.strip():
            _who.add(msg.strip())
        if _timer is not None:
            _timer.cancel()
            k.log("arrival signal again; restarting the countdown")
        else:
            k.log(f"arrival signal ({msg!r}); checking in {DELAY_S/60:.0f} min")
        _timer = threading.Timer(DELAY_S, run_check)
        _timer.daemon = True
        _timer.start()


def listen():
    since = str(int(time.time()))  # ignore old messages at startup
    backoff = 5
    while True:
        try:
            url = f"{SERVER}/{TOPIC}/json?since={since}"
            with urllib.request.urlopen(url, timeout=90) as r:
                backoff = 5
                for raw in r:
                    ev = json.loads(raw)
                    if ev.get("id"):
                        since = ev["id"]
                    if ev.get("event") == "message":
                        on_arrival(ev.get("message", ""))
        except Exception as e:
            k.log(f"listener reconnecting in {backoff}s ({e!r})")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    k.log(f"listening for arrivals on {SERVER}/{TOPIC}")
    listen()
