#!/usr/bin/env python3
"""
Weather for the Kona log, from Open-Meteo (free, no key).

Raw data is kept, never thrown away:
  weather_hourly.csv   hourly weather at home, one row per hour, with its source:
                         archive  = reanalysis (ERA5-based, final, ~5 days behind)
                         forecast = model analysis/forecast (recent days and the next few)
                       Archive rows replace forecast rows for the same hour when they arrive.
Derived (rebuilt from raw every run, so you can change the analysis and re-run):
  trip_weather.csv     every trip from trips.csv joined to the weather at its start time

  kona_weather.py                 fetch recent + fill any gaps back to the first trip
  kona_weather.py --backfill 2024-11-01 2024-12-31   fetch an archive range
  kona_weather.py --rebuild       just rebuild trip_weather.csv
  kona_weather.py --summary       print the temperature/efficiency analysis
"""
import csv
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request

import kona_plug_check as k

HOURLY = k.HERE / "weather_hourly.csv"
TRIP_WX = k.HERE / "trip_weather.csv"
TZ = "America/Halifax"
VARS = ["temperature_2m", "precipitation", "rain", "snowfall", "wind_speed_10m", "weather_code"]
COLS = ["time", "temp_c", "precip_mm", "rain_mm", "snow_cm", "wind_kmh", "weather_code",
        "source", "fetched_at"]
TW_COLS = ["start", "distance_km", "total_kwh", "drivetrain_kwh", "climate_kwh", "regen_kwh",
           "kwh_per_100km", "km_per_kwh", "climate_share_pct", "temp_c", "precip_mm",
           "snow_cm", "wind_kmh", "weather_code", "wx_source"]
ARCHIVE_LAG_DAYS = 6


# ---------------- raw store ----------------
def load_hourly():
    out = {}
    if HOURLY.exists():
        with open(HOURLY) as f:
            for r in csv.DictReader(f):
                out[r["time"]] = r
    return out


def save_hourly(rows):
    tmp = HOURLY.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for t in sorted(rows):
            w.writerow(rows[t])
    tmp.replace(HOURLY)


def _get(url, params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{q}", timeout=60) as r:
        return json.loads(r.read())


def _merge(store, data, source):
    h = data.get("hourly", {})
    times = h.get("time", [])
    now = f"{dt.datetime.now():%Y-%m-%d %H:%M}"
    n = 0
    for i, t in enumerate(times):
        key = t.replace("T", " ")
        old = store.get(key)
        if old and old["source"] == "archive" and source != "archive":
            continue  # never replace final data with model data
        val = lambda v: (h.get(v) or [None] * len(times))[i]
        if val("temperature_2m") is None:
            continue
        store[key] = {"time": key, "temp_c": val("temperature_2m"),
                      "precip_mm": val("precipitation"), "rain_mm": val("rain"),
                      "snow_cm": val("snowfall"), "wind_kmh": val("wind_speed_10m"),
                      "weather_code": val("weather_code"), "source": source,
                      "fetched_at": now}
        n += 1
    return n


def fetch_recent(store):
    d = _get("https://api.open-meteo.com/v1/forecast", {
        "latitude": k.HOME_LAT, "longitude": k.HOME_LON, "hourly": ",".join(VARS),
        "timezone": TZ, "past_days": 10, "forecast_days": 3})
    return _merge(store, d, "forecast")


def fetch_archive(store, start, end):
    if end < start:
        return 0
    d = _get("https://archive-api.open-meteo.com/v1/archive", {
        "latitude": k.HOME_LAT, "longitude": k.HOME_LON, "hourly": ",".join(VARS),
        "timezone": TZ, "start_date": f"{start:%Y-%m-%d}", "end_date": f"{end:%Y-%m-%d}"})
    return _merge(store, d, "archive")


def update():
    """Fetch recent/forecast hours, and archive data for any trip days not yet final."""
    store = load_hourly()
    n = fetch_recent(store)
    trips = load_trips()
    if trips:
        first = min(t["t"] for t in trips).date()
        last_final = dt.date.today() - dt.timedelta(days=ARCHIVE_LAG_DAYS)
        need = sorted({d for d in (first + dt.timedelta(days=i)
                                   for i in range((last_final - first).days + 1))
                       if store.get(f"{d:%Y-%m-%d} 12:00", {}).get("source") != "archive"})
        if need:
            n += fetch_archive(store, need[0], need[-1])
    save_hourly(store)
    k.log(f"weather updated ({n} hourly values)")
    rebuild()


# ---------------- derived ----------------
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_trips():
    rows = []
    if k.TRIP_LOG.exists():
        with open(k.TRIP_LOG) as f:
            for r in csv.DictReader(f):
                try:
                    r["t"] = dt.datetime.strptime(r["start"], "%Y-%m-%d %H:%M:%S")
                    rows.append(r)
                except Exception:
                    pass
    return sorted(rows, key=lambda r: r["t"])


def wx_at(store, t):
    """Weather at time t: temperature interpolated between hours, the rest from the hour."""
    h0 = t.replace(minute=0, second=0, microsecond=0)
    a = store.get(f"{h0:%Y-%m-%d %H:%M}")
    b = store.get(f"{h0 + dt.timedelta(hours=1):%Y-%m-%d %H:%M}")
    if not a:
        return None
    ta, tb = _f(a["temp_c"]), _f(b["temp_c"]) if b else None
    frac = t.minute / 60
    temp = ta if tb is None else ta + (tb - ta) * frac
    return {"temp_c": round(temp, 1), "precip_mm": a["precip_mm"], "snow_cm": a["snow_cm"],
            "wind_kmh": a["wind_kmh"], "weather_code": a["weather_code"], "wx_source": a["source"]}


def rebuild():
    store = load_hourly()
    rows = []
    for t in load_trips():
        dist, tot = _f(t["distance_km"]), _f(t["total_wh"])
        clim = _f(t["climate_wh"]) or 0
        r = {"start": t["start"], "distance_km": dist,
             "total_kwh": None if tot is None else round(tot / 1000, 2),
             "drivetrain_kwh": round((_f(t["drivetrain_wh"]) or 0) / 1000, 2),
             "climate_kwh": round(clim / 1000, 2),
             "regen_kwh": round((_f(t["regen_wh"]) or 0) / 1000, 2),
             "kwh_per_100km": round(tot / 10 / dist, 2) if dist and tot else None,
             "km_per_kwh": round(dist / (tot / 1000), 2) if dist and tot else None,
             "climate_share_pct": round(clim / tot * 100, 1) if tot else None}
        wx = wx_at(store, t["t"]) or {}
        r.update({c: wx.get(c) for c in ["temp_c", "precip_mm", "snow_cm", "wind_kmh",
                                          "weather_code", "wx_source"]})
        rows.append(r)
    with open(TRIP_WX, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TW_COLS)
        w.writeheader()
        w.writerows(rows)
    return rows


def load_trip_wx():
    rows = []
    if TRIP_WX.exists():
        with open(TRIP_WX) as f:
            for r in csv.DictReader(f):
                r["t"] = dt.datetime.strptime(r["start"], "%Y-%m-%d %H:%M:%S")
                for c in ("distance_km", "km_per_kwh", "kwh_per_100km", "temp_c",
                          "climate_kwh", "total_kwh", "climate_share_pct", "precip_mm", "snow_cm"):
                    r[c] = _f(r[c])
                rows.append(r)
    return rows


# ---------------- analysis ----------------
MIN_TRIP_KM = 5          # ignore tiny trips in the fit
MIN_TRIPS = 15           # need at least this many trips ...
MIN_TEMP_SPAN = 8        # ... spread over at least this many degrees


def fit(rows):
    """Distance-weighted straight line km/kWh = a + b*temp. None if too little data."""
    pts = [(r["temp_c"], r["km_per_kwh"], r["distance_km"]) for r in rows
           if r["temp_c"] is not None and r["km_per_kwh"] and (r["distance_km"] or 0) >= MIN_TRIP_KM]
    if len(pts) < MIN_TRIPS:
        return None
    temps = [p[0] for p in pts]
    if max(temps) - min(temps) < MIN_TEMP_SPAN:
        return None
    W = sum(p[2] for p in pts)
    mx = sum(p[0] * p[2] for p in pts) / W
    my = sum(p[1] * p[2] for p in pts) / W
    sxx = sum(p[2] * (p[0] - mx) ** 2 for p in pts)
    b = sum(p[2] * (p[0] - mx) * (p[1] - my) for p in pts) / sxx
    a = my - b * mx
    return {"a": a, "b": b, "n": len(pts), "tmin": min(temps), "tmax": max(temps)}


def predict(f, temp):
    return None if not f else f["a"] + f["b"] * temp


BINS = [(-99, -10, "below −10 °C"), (-10, 0, "−10 to 0 °C"), (0, 10, "0 to 10 °C"),
        (10, 20, "10 to 20 °C"), (20, 99, "above 20 °C")]


def by_band(rows):
    out = []
    for lo, hi, label in BINS:
        rs = [r for r in rows if r["temp_c"] is not None and lo <= r["temp_c"] < hi
              and r["total_kwh"] and r["distance_km"]]
        if not rs:
            continue
        km = sum(r["distance_km"] for r in rs)
        kwh = sum(r["total_kwh"] for r in rs)
        clim = sum(r["climate_kwh"] or 0 for r in rs)
        out.append({"band": label, "trips": len(rs), "km": km, "km_per_kwh": km / kwh,
                    "climate_pct": clim / kwh * 100})
    return out


def forecast_today(store=None):
    """Daytime (7-21 h) mean/min/max temperature for today from the stored forecast."""
    store = store or load_hourly()
    today = dt.date.today()
    temps = [_f(r["temp_c"]) for t, r in store.items()
             if t.startswith(f"{today:%Y-%m-%d}") and 7 <= int(t[11:13]) <= 21]
    temps = [x for x in temps if x is not None]
    if not temps:
        return None
    return {"mean": sum(temps) / len(temps), "min": min(temps), "max": max(temps)}


def summary_text(rows=None):
    rows = rows if rows is not None else load_trip_wx()
    f = fit(rows)
    lines = []
    if f:
        ref = predict(f, 15)
        per10 = f["b"] * 10 / ref * 100 if ref else None
        lines.append(f"Fit on {f['n']} trips ({f['tmin']:.0f} to {f['tmax']:.0f} °C): "
                     f"{f['b']:+.2f} km/kWh per °C; each 10 °C colder {'costs' if per10 > 0 else 'gains'} "
                     f"about {abs(per10):.0f}% in km/kWh (relative to 15 °C)")
    else:
        lines.append("Not enough spread of temperatures yet for a reliable fit.")
    for b in by_band(rows):
        lines.append(f"{b['band']:>14}: {b['trips']:3d} trips, {b['km']:6.0f} km, "
                     f"{b['km_per_kwh']:.2f} km/kWh, climate {b['climate_pct']:.0f}% of energy")
    return "\n".join(lines)


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--backfill" in a:
        i = a.index("--backfill")
        s, e = (dt.date.fromisoformat(x) for x in a[i + 1:i + 3])
        st = load_hourly()
        print(f"{fetch_archive(st, s, e)} hourly values")
        save_hourly(st)
        rebuild()
    elif "--rebuild" in a:
        print(f"{len(rebuild())} trips rebuilt")
    elif "--summary" in a:
        rebuild()
        print(summary_text())
    else:
        update()
        print(summary_text())
