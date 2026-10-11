#!/usr/bin/env python3
"""
Kona plug-in reminder.

Run from cron at 9, 10 and 11 pm. If the car is home and NOT plugged in,
send one alert per night. Credentials come from the existing monitor.cfg,
so nothing is duplicated.

Usage:
  kona_plug_check.py            normal check (alerts if needed)
  kona_plug_check.py --status   print what the car reports, no alert
  kona_plug_check.py --test     send a test notification
"""
import configparser
import csv
import datetime as dt
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path

from hyundai_kia_connect_api import VehicleManager
from hyundai_kia_connect_api.Token import Token
from hyundai_kia_connect_api.exceptions import AuthenticationOTPRequired

HERE = Path(__file__).resolve().parent
CFG = configparser.ConfigParser()
CFG.read(HERE / "plugcheck.cfg")
P = CFG["plugcheck"]

# BlueLink login: bluelink.cfg in this folder if present, else the old monitor.cfg
MONITOR_CFG = (HERE / "bluelink.cfg") if (HERE / "bluelink.cfg").exists() \
    else Path(P.get("monitor_cfg")).expanduser()
HOME_LAT = P.getfloat("home_lat")
HOME_LON = P.getfloat("home_lon")
HOME_RADIUS_M = P.getfloat("home_radius_m", 150)
STALE_HOURS = P.getfloat("force_refresh_if_older_than_hours", 3)
ALERT_IF_AWAY_AT_LAST_CHECK = P.getboolean("alert_if_away_at_last_check", False)
LAST_CHECK_HOUR = P.getint("last_check_hour", 23)
STATE_FILE = HERE / "plugcheck.state"
FOLLOWUP_MAX_SOC = P.getfloat("followup_max_soc", 70)
LOG_FILE = HERE / "plugcheck.log"
TOKEN_FILE = HERE / "token.json"
CAR_LOG = HERE / "car_log.csv"
TRIP_LOG = HERE / "trips.csv"


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------- saved login ----------
def load_token():
    try:
        return Token.from_dict(json.loads(TOKEN_FILE.read_text()))
    except Exception:
        return None


def save_token(token):
    if token is None:
        return
    TOKEN_FILE.write_text(json.dumps(token.to_dict()))
    os.chmod(TOKEN_FILE, 0o600)  # holds the login; readable by you only


# ---------- car logging ----------
SNAP_COLS = ["logged_at", "source", "car_updated", "latitude", "longitude",
             "km_from_home", "home", "plugged", "charging", "soc_pct",
             "battery_12v_pct", "odometer_km", "range_km", "engine_on"]
TRIP_COLS = ["start", "distance_km", "total_wh", "drivetrain_wh", "climate_wh",
             "accessories_wh", "battery_care_wh", "regen_wh", "kwh_per_100km"]


def _append(path, cols, row):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow(row)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def log_car(v, source):
    """Append one status row, plus any trips not yet recorded."""
    try:
        lat, lon = v.location_latitude, v.location_longitude
        d = distance_m(lat, lon, HOME_LAT, HOME_LON) if lat is not None and lon is not None else None
        rng = getattr(v, "ev_driving_range", None)
        _append(CAR_LOG, SNAP_COLS, {
            "logged_at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
            "source": source,
            "car_updated": getattr(v, "last_updated_at", ""),
            "latitude": lat, "longitude": lon,
            "km_from_home": None if d is None else round(d / 1000, 2),
            "home": None if d is None else d <= HOME_RADIUS_M,
            "plugged": getattr(v, "ev_battery_is_plugged_in", None),
            "charging": getattr(v, "ev_battery_is_charging", None),
            "soc_pct": getattr(v, "ev_battery_percentage", None),
            "battery_12v_pct": getattr(v, "car_battery_percentage", None),
            "odometer_km": getattr(v, "odometer", None),
            "range_km": rng,
            "engine_on": getattr(v, "engine_is_running", None),
        })
        trips = getattr(v, "daily_stats", None) or []
        seen = set()
        if TRIP_LOG.exists():
            with open(TRIP_LOG) as f:
                seen = {r["start"] for r in csv.DictReader(f)}
        for t in sorted(trips, key=lambda t: t.date or dt.datetime.min):
            start = f"{t.date:%Y-%m-%d %H:%M:%S}" if t.date else ""
            if not start or start in seen:
                continue
            dist, tot = _num(t.distance), _num(t.total_consumed)
            _append(TRIP_LOG, TRIP_COLS, {
                "start": start, "distance_km": t.distance, "total_wh": t.total_consumed,
                "drivetrain_wh": t.engine_consumption, "climate_wh": t.climate_consumption,
                "accessories_wh": t.onboard_electronics_consumption,
                "battery_care_wh": t.battery_care_consumption, "regen_wh": t.regenerated_energy,
                "kwh_per_100km": round(tot / 10 / dist, 1) if dist and tot else None,
            })
            seen.add(start)
    except Exception as e:  # logging must never break the reminder
        log(f"car log failed: {e!r}")


def nightly():
    """Late-night snapshot from cached data only (never wakes the car)."""
    v = read_car(force=False, allow_stale_refresh=False, source="nightly")
    log(f"nightly log: SOC={v.ev_battery_percentage}% 12V={v.car_battery_percentage}% "
        f"odo={v.odometer}")


# ---------- notifications ----------
def notify(title, body):
    method = P.get("notify", "ntfy").lower()
    if method == "ntfy":
        server = P.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{server}/{P['ntfy_topic']}",
            data=body.encode(),
            headers={"Title": title, "Priority": "high", "Tags": "electric_plug"},
        )
    elif method == "twilio":
        T = CFG["twilio"]
        data = urllib.parse.urlencode(
            {"To": T["to"], "From": T["from"], "Body": f"{title}: {body}"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{T['account_sid']}/Messages.json",
            data=data,
        )
        auth = b64encode(f"{T['account_sid']}:{T['auth_token']}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
    else:
        raise ValueError(f"unknown notify method: {method}")
    with urllib.request.urlopen(req, timeout=30) as r:
        log(f"notification sent via {method} (HTTP {r.status}): {title}")


# ---------- once-per-night bookkeeping ----------
def tonight():
    # a 'night' runs noon to noon, so a 9-11 pm window is one key
    return (dt.datetime.now() - dt.timedelta(hours=12)).date().isoformat()


def _state():
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    return st if st.get("night") == tonight() else {"night": tonight()}


def _set(**kw):
    st = _state()
    st.update(kw)
    STATE_FILE.write_text(json.dumps(st))


def already_alerted():
    return bool(_state().get("alerted"))


def mark_alerted():
    _set(alerted=True, done=True)


# ---------- car ----------
def read_car(force=False, allow_stale_refresh=True, source="check"):
    m = configparser.ConfigParser()
    m.read(MONITOR_CFG)
    c = m["monitor"]
    mgr = VehicleManager(
        region=int(c["region"]), brand=int(c["brand"]),
        username=c["username"], password=c["password"], pin=c["pin"],
        token=load_token(),
    )
    try:
        mgr.check_and_refresh_token()
    except AuthenticationOTPRequired:
        log("Hyundai wants a one-time code: run enroll.py")
        try:
            if not already_alerted():
                notify("Kona checker needs you",
                       "Hyundai asked for a one-time code. On the PC run: "
                       "cd ~/konaev/plugcheck && ./venv/bin/python enroll.py")
                mark_alerted()
        except Exception:
            pass
        raise
    if not mgr.vehicles:
        mgr.initialize_vehicles()
    save_token(mgr.token)
    mgr.update_all_vehicles_with_cached_state()
    v = next(iter(mgr.vehicles.values()))

    upd = v.last_updated_at
    age_h = None
    if upd is not None:
        now = dt.datetime.now(upd.tzinfo) if upd.tzinfo else dt.datetime.now()
        age_h = (now - upd).total_seconds() / 3600
    if force or (allow_stale_refresh and (age_h is None or age_h > STALE_HOURS)):
        log("forcing refresh" if force else f"cached data is {age_h if age_h is None else round(age_h,1)} h old -> forcing refresh")
        try:
            mgr.force_refresh_vehicle_state(v.id)
            mgr.update_vehicle_with_cached_state(v.id)
            v = mgr.vehicles[v.id]
        except Exception as e:  # stale data is still better than nothing
            log(f"force refresh failed: {e!r}")
    save_token(mgr.token)
    log_car(v, source + ("-fresh" if force else ""))
    return v


def check(force=False, status_only=False, once_per_night=True, always_report=False, who=""):
    """Read the car; alert if home and unplugged. Returns True if alerted."""
    v = read_car(force=force, source="arrival" if always_report else ("status" if status_only else "check"))
    lat, lon = v.location_latitude, v.location_longitude
    plugged = bool(v.ev_battery_is_plugged_in)  # CA API gives 0 / 1 / 2
    soc = v.ev_battery_percentage
    if lat is None or lon is None:
        log("no GPS position from car; skipping")
        return False
    d = distance_m(lat, lon, HOME_LAT, HOME_LON)
    home = d <= HOME_RADIUS_M
    log(f"home={home} ({d:.0f} m)  plugged={v.ev_battery_is_plugged_in}  "
        f"SOC={soc}%  updated={v.last_updated_at}")

    if status_only:
        return False
    if once_per_night and already_alerted():
        log("already alerted tonight")
        return False

    if always_report:  # arrival mode: report either way
        by = f" ({who})" if who else ""
        if not home:
            notify("Kona not home?", f"Arrival signal{by}, but car is {d/1000:.1f} km away (battery {soc}%).")
        elif plugged:
            notify("Kona plugged in", f"Kona is home and plugged in (battery {soc}%){by}.")
        else:
            notify("PLUG IN THE KONA", f"Kona is home but NOT plugged in (battery {soc}%){by}.")
        return True

    if home and not plugged:
        notify("Plug in the Kona", f"Kona is home but not plugged in (battery {soc}%).")
        mark_alerted()
        return True
    if (not home and ALERT_IF_AWAY_AT_LAST_CHECK
          and dt.datetime.now().hour >= LAST_CHECK_HOUR):
        notify("Kona not home", f"Kona is {d/1000:.1f} km from home (battery {soc}%).")
        mark_alerted()
        return True
    return False


def evening():
    """Scheduled 9:30-11 pm check. First check of the night uses fresh data;
    later ones use cached data and confirm with fresh data before alerting.
    Stops once the car is confirmed home and plugged in. If it isn't, later
    checks send follow-up reminders unless the battery was above
    followup_max_soc at the first alert."""
    st = _state()
    if st.get("done"):
        log("evening check: already settled tonight, skipping")
        return
    first = not st.get("forced")
    if first:
        _set(forced=True)
    v = read_car(force=first, allow_stale_refresh=False, source="evening")
    lat, lon = v.location_latitude, v.location_longitude
    if lat is None or lon is None:
        log("evening check: no GPS position; skipping")
        return
    d = distance_m(lat, lon, HOME_LAT, HOME_LON)
    home, plugged, soc = d <= HOME_RADIUS_M, bool(v.ev_battery_is_plugged_in), v.ev_battery_percentage
    log(f"evening check ({'fresh' if first else 'cached'}): home={home} ({d:.0f} m) "
        f"plugged={v.ev_battery_is_plugged_in} SOC={soc}%")
    if home and not plugged and not first:
        # cached data may predate plugging in: confirm before alerting
        v = read_car(force=True, allow_stale_refresh=False, source="evening-confirm")
        plugged, soc = bool(v.ev_battery_is_plugged_in), v.ev_battery_percentage
        log(f"evening check (confirming, fresh): plugged={v.ev_battery_is_plugged_in} SOC={soc}%")
    if home and plugged:
        _set(done=True)
    elif home and not plugged:
        now = f"{dt.datetime.now():%H:%M}"
        if not st.get("alerted"):
            notify("Plug in the Kona", f"Kona is home but not plugged in (battery {soc}%).")
            # no follow-ups tonight if the battery is already well charged
            done = soc is not None and soc > FOLLOWUP_MAX_SOC
            _set(alerted=True, done=done, first_soc=soc)
            if done:
                log(f"battery {soc}% > {FOLLOWUP_MAX_SOC}%: no follow-up reminders tonight")
        else:
            notify("Kona still not plugged in", f"Still not plugged in at {now} (battery {soc}%).")
            _set(followups=st.get("followups", 0) + 1)


def main():
    args = sys.argv[1:]
    if "--test" in args:
        notify("Kona test", "If you see this, alerts work.")
        return
    if "--evening" in args:
        evening()
        return
    if "--nightly" in args:
        nightly()
        return
    check(force="--force" in args, status_only="--status" in args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e!r}")
        raise
