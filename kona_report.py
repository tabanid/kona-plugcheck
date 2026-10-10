#!/usr/bin/env python3
"""
Kona daily note and weekly PDF report, emailed via Gmail.

  kona_report.py --daily      short note in the email body (run each morning)
  kona_report.py --weekly     PDF for the previous Mon-Sun, attached (run Monday)
  kona_report.py --daily --dry-run   print / save instead of emailing

Everything comes from car_log.csv, trips.csv and plugcheck.log; the only
BlueLink use is one cached read (never wakes the car) to get this morning's state.
"""
import configparser
import csv
import datetime as dt
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import kona_plug_check as k
import kona_weather as wx

HERE = k.HERE
R = k.CFG["report"] if k.CFG.has_section("report") else {}
RATE = float(R.get("rate_per_kwh", 0.185))           # $ per kWh
PACK_KWH = float(R.get("battery_kwh", 64.8))          # usable pack size
CHARGE_EFF = float(R.get("charging_efficiency", 0.9))  # wall -> battery
LOW_12V = float(R.get("low_12v_pct", 75))
EMAIL_CFG = HERE / "email.cfg"

# palette (reference instance, single blue hue + neutrals)
BLUE, BLUE_DARK, BLUE_LIGHT = "#2a78d6", "#104281", "#9ec5f4"
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"
EARLIER = "#c3c2b7"  # earlier trips, recessive


# ---------------- data ----------------
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _b(x):
    return str(x).strip().lower() in ("true", "1", "2")


def load_snaps():
    rows = []
    if k.CAR_LOG.exists():
        with open(k.CAR_LOG) as f:
            for r in csv.DictReader(f):
                try:
                    r["t"] = dt.datetime.strptime(r["logged_at"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                rows.append(r)
    return sorted(rows, key=lambda r: r["t"])


def load_trips():
    rows = []
    if k.TRIP_LOG.exists():
        with open(k.TRIP_LOG) as f:
            for r in csv.DictReader(f):
                try:
                    r["t"] = dt.datetime.strptime(r["start"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                rows.append(r)
    return sorted(rows, key=lambda r: r["t"])


def load_events():
    """(time, text) from plugcheck.log: alerts sent, errors, code requests."""
    ev = []
    if k.LOG_FILE.exists():
        for line in k.LOG_FILE.read_text().splitlines():
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(.*)", line)
            if not m:
                continue
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            ev.append((t, m.group(2)))
    return ev


def between(rows, a, b):
    return [r for r in rows if a <= r["t"] < b]


def charging_in(snaps):
    """Sum of SOC increases between consecutive readings -> kWh, sessions."""
    added_pct, sessions, rising = 0.0, 0, False
    for prev, cur in zip(snaps, snaps[1:]):
        p, c = _f(prev["soc_pct"]), _f(cur["soc_pct"])
        if p is None or c is None:
            continue
        if c > p:
            added_pct += c - p
            if not rising:
                sessions += 1
            rising = True
        else:
            rising = False
    kwh = added_pct / 100 * PACK_KWH
    return kwh, sessions, kwh / CHARGE_EFF * RATE


def driving_in(trips, snaps):
    dist = sum(_f(t["distance_km"]) or 0 for t in trips)
    wh = sum(_f(t["total_wh"]) or 0 for t in trips)
    if not trips:  # fall back to odometer
        odo = [_f(s["odometer_km"]) for s in snaps if _f(s["odometer_km"])]
        dist = (max(odo) - min(odo)) if len(odo) > 1 else 0
    eff = (wh / 10 / dist) if dist and wh else None
    return dist, len(trips), eff, wh


def daily_min_12v(snaps):
    by = {}
    for s in snaps:
        v = _f(s["battery_12v_pct"])
        if v is not None:
            d = s["t"].date()
            by[d] = min(v, by.get(d, 999))
    return sorted(by.items())


def full_range_by_day(snaps):
    by = {}
    for s in snaps:
        r, soc = _f(s["range_km"]), _f(s["soc_pct"])
        if r and soc and soc >= 20:
            by.setdefault(s["t"].date(), []).append(r / soc * 100)
    return sorted((d, sorted(v)[len(v) // 2]) for d, v in by.items())


def fmt(x, nd=0, unit=""):
    return "n/a" if x is None else f"{x:,.{nd}f}{unit}"


# ---------------- email ----------------
def send(subject, body, attachment=None):
    c = configparser.ConfigParser()
    c.read(EMAIL_CFG)
    e = c["email"]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = e["sender"], e["to"], subject
    msg.set_content(body)
    if attachment:
        msg.add_attachment(Path(attachment).read_bytes(), maintype="application",
                           subtype="pdf", filename=Path(attachment).name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(e["sender"], e["app_password"].replace(" ", ""))
        s.send_message(msg)
    k.log(f"report emailed: {subject}")


# ---------------- daily ----------------
def daily(dry=False):
    now = dt.datetime.now()
    try:  # one cached read for this morning's state (free; also logs a row)
        k.read_car(force=False, allow_stale_refresh=False, source="report")
    except Exception as ex:
        k.log(f"daily report: car read failed: {ex!r}")
    try:
        wx.update()
    except Exception as ex:
        k.log(f"weather update failed: {ex!r}")
    snaps, trips, events = load_snaps(), load_trips(), load_events()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yest = today - dt.timedelta(days=1)
    twx = wx.load_trip_wx()

    lines = []
    last = snaps[-1] if snaps else None
    if last:
        where = "home" if _b(last["home"]) else f"{last['km_from_home']} km from home"
        plug = "plugged in" if _b(last["plugged"]) else "NOT plugged in"
        lines.append(f"Now: {fmt(_f(last['soc_pct']))}% · {fmt(_f(last['range_km']))} km range · "
                     f"{where}, {plug}")

    kwh, _, cost = charging_in(between(snaps, yest.replace(hour=18), now))
    if kwh > 0.5:
        lines.append(f"Overnight charge: about {kwh:.1f} kWh added (~${cost:.2f})")
    else:
        lines.append("Overnight charge: none")

    dist, n, eff, _ = driving_in(between(trips, yest, today), between(snaps, yest, today))
    if dist:
        yt = [r["temp_c"] for r in twx if yest <= r["t"] < today and r["temp_c"] is not None]
        lines.append(f"Yesterday: {dist:.1f} km in {n} trip{'s' if n != 1 else ''}"
                     + (f" · {eff:.1f} kWh/100 km ({100/eff:.1f} km/kWh)" if eff else "")
                     + ((f" · {min(yt):.0f} °C" if round(min(yt)) == round(max(yt))
                        else f" · {min(yt):.0f} to {max(yt):.0f} °C") if yt else ""))
    else:
        lines.append("Yesterday: not driven")

    fc, f = wx.forecast_today(), wx.fit(twx)
    if fc:
        line = f"Today: {fc['min']:.0f} to {fc['max']:.0f} °C"
        if f:
            kk = wx.predict(f, fc["mean"])
            line += f" · expect about {kk:.1f} km/kWh, so roughly {kk * PACK_KWH:.0f} km on a full charge"
        lines.append(line)

    mins = daily_min_12v(snaps)
    if mins:
        v = mins[-1][1]
        falling = len(mins) >= 4 and all(a[1] > b[1] for a, b in zip(mins[-4:], mins[-3:]))
        flag = "  ⚠ low" if v < LOW_12V else ("  ⚠ falling 3 days running" if falling else "")
        lines.append(f"12 V battery: {v:.0f}%{flag}")

    # last successful car reading: errors before it have been resolved
    ok_times = [t for t, x in events if x.startswith("home=") or "evening check (" in x
                or "nightly log" in x] + [s_["t"] for s_ in snaps]
    last_ok = max(ok_times) if ok_times else dt.datetime.min
    issues = []
    for t, txt in events:
        if yest.replace(hour=12) <= t < now:
            if "notification sent" in txt and ("Plug in" in txt or "PLUG IN" in txt):
                issues.append(f"{t:%a %H:%M} unplugged alert sent")
            elif ("ERROR" in txt or "one-time code" in txt) and t > last_ok:
                issues.append(f"{t:%a %H:%M} {txt[:90]}")
    night = [s for s in between(snaps, yest.replace(hour=23), today + dt.timedelta(hours=3))
             if s["source"].startswith("nightly")]
    if night and not _b(night[-1]["home"]):
        issues.append("car was not home at bedtime")
    if issues:
        lines.append("Attention: " + "; ".join(issues))

    subj = f"Kona {now:%a %d %b}: " + (lines[0].split(" · ")[0].replace("Now: ", "") if lines else "no data")
    if issues:
        subj += " ⚠"
    body = "\n".join(lines) + "\n"
    if dry:
        print(subj); print(body)
    else:
        send(subj, body)


# ---------------- weekly ----------------
def weekly(dry=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    now = dt.datetime.now()
    this_mon = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    wk_start, wk_end = this_mon - dt.timedelta(days=7), this_mon
    try:
        wx.update()
    except Exception as ex:
        k.log(f"weather update failed: {ex!r}")
    snaps, trips, events = load_snaps(), load_trips(), load_events()
    twx = wx.load_trip_wx()
    wk_wx = [r for r in twx if wk_start <= r["t"] < wk_end]
    tw = [r["temp_c"] for r in wk_wx if r["temp_c"] is not None]
    wet = sorted({r["t"].date() for r in wk_wx if (r["precip_mm"] or 0) > 0.2})
    snowy = sorted({r["t"].date() for r in wk_wx if (r["snow_cm"] or 0) > 0})

    def week(i):  # i = 0 report week, 1 the week before ...
        a = wk_start - dt.timedelta(days=7 * i)
        return a, a + dt.timedelta(days=7)

    def wstats(i):
        a, b = week(i)
        d, n, e, wh = driving_in(between(trips, a, b), between(snaps, a, b))
        return {"start": a, "dist": d, "trips": n, "eff": e, "wh": wh}

    weeks = [wstats(i) for i in range(16)][::-1]  # oldest -> newest
    first = next((i for i, w in enumerate(weeks) if w["dist"]), len(weeks) - 1)
    weeks = weeks[min(first, len(weeks) - 4):]  # drop empty leading weeks, keep >= 4
    cur, prev = weeks[-1], weeks[-2]
    avg4 = [w["dist"] for w in weeks[-5:-1] if w["dist"]]
    avg4 = sum(avg4) / len(avg4) if avg4 else None

    wt = between(trips, wk_start, wk_end)
    parts = {lbl: sum(_f(t[col]) or 0 for t in wt) / 1000 for lbl, col in
             [("Drivetrain", "drivetrain_wh"), ("Climate", "climate_wh"),
              ("Accessories", "accessories_wh"), ("Battery care", "battery_care_wh")]}
    regen = sum(_f(t["regen_wh"]) or 0 for t in wt) / 1000
    kwh, sessions, cost = charging_in(between(snaps, wk_start, wk_end))

    nightly = [s for s in between(snaps, wk_start, wk_end) if s["source"].startswith("nightly")]
    nights_ok = sum(1 for s in nightly if _b(s["home"]) and _b(s["plugged"]))
    alerts = sum(1 for t, x in events if wk_start <= t < wk_end and "notification sent" in x
                 and ("Plug in" in x or "PLUG IN" in x))
    fresh = sum(1 for s in between(snaps, wk_start, wk_end) if s["source"].endswith("fresh"))

    v12 = [(d, v) for d, v in daily_min_12v(snaps) if d >= (wk_end - dt.timedelta(days=60)).date()]
    rng = [(d, v) for d, v in full_range_by_day(snaps) if d >= (wk_end - dt.timedelta(days=120)).date()]

    # ---- page ----
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
                         "axes.titlesize": 10, "axes.titleweight": "bold", "axes.titlecolor": INK,
                         "axes.titlelocation": "left"})
    fig = plt.figure(figsize=(8.5, 11), facecolor=SURFACE)
    label = f"{wk_start:%a %d %b} – {(wk_end - dt.timedelta(days=1)):%a %d %b %Y}"
    fig.text(0.07, 0.955, "Kona weekly report", fontsize=18, weight="bold", color=INK)
    fig.text(0.07, 0.935, label, fontsize=10, color=INK2)

    def delta(a, b):
        if not a or not b:
            return ""
        p = (a - b) / b * 100
        return f"  ({'+' if p >= 0 else ''}{p:.0f}% vs last week)"

    rows = [
        ("Distance", f"{cur['dist']:.0f} km in {cur['trips']} trips{delta(cur['dist'], prev['dist'])}"
                     + (f"; 4-week avg {avg4:.0f} km" if avg4 else "")),
        ("Efficiency", f"{fmt(cur['eff'], 1)} kWh/100 km" + (f" = {100/cur['eff']:.1f} km/kWh" if cur['eff'] else "") + (f"  (last week {fmt(prev['eff'], 1)})" if prev['eff'] else "")),
        ("Conditions", (f"{sum(tw)/len(tw):.0f} °C average while driving ({min(tw):.0f} to {max(tw):.0f} °C)"
                        + (f"; rain/snow on {len(wet)} driving day(s)" if wet else "")
                        + (f", snow on {len(snowy)}" if snowy else "")) if tw else "no weather data"),
        ("Charging", f"{sessions} session{'s' if sessions != 1 else ''}, about {kwh:.0f} kWh added, ~${cost:.2f} at {RATE*100:.1f}¢/kWh"),
        ("12 V battery", f"{fmt(v12[-1][1])}% (lowest {fmt(min(v for _, v in v12))}% over 60 days)" if v12 else "n/a"),
        ("Range at 100%", f"about {fmt(rng[-1][1])} km (car's estimate)" if rng else "n/a"),
        ("Reminder", f"plugged in at bedtime {nights_ok} of {len(nightly)} nights · {alerts} unplugged alert(s) · "
                     f"{fresh} car wake-up(s)"),
    ]
    y = 0.895
    for k_, v in rows:
        fig.text(0.07, y, k_, fontsize=10, weight="bold", color=INK)
        fig.text(0.25, y, v, fontsize=10, color=INK2)
        y -= 0.024

    def style(ax, title):
        ax.set_title(title, pad=8)
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(length=0)

    # 1. weekly distance
    ax = fig.add_axes([0.07, 0.53, 0.40, 0.18])
    xs = range(len(weeks))
    cols = [BLUE_LIGHT] * (len(weeks) - 1) + [BLUE_DARK]
    ax.bar(xs, [w["dist"] for w in weeks], color=cols, width=0.75, edgecolor=SURFACE, linewidth=1.5)
    ax.set_xticks([0, len(weeks) - 1], [f"{weeks[0]['start']:%d %b}", f"{weeks[-1]['start']:%d %b}"])
    style(ax, "Distance per week (km)")

    # 2. weekly efficiency
    ax = fig.add_axes([0.55, 0.53, 0.40, 0.18])
    pts = [(i, w["eff"]) for i, w in enumerate(weeks) if w["eff"]]
    if pts:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=BLUE, linewidth=2,
                marker="o", markersize=4, solid_capstyle="round")
        ax.annotate(f"{pts[-1][1]:.1f}", pts[-1], xytext=(4, 4), textcoords="offset points", color=INK2)
    ax.set_xlim(-0.5, len(weeks) - 0.5)
    ax.set_xticks([0, len(weeks) - 1], [f"{weeks[0]['start']:%d %b}", f"{weeks[-1]['start']:%d %b}"])
    style(ax, "Efficiency per week (kWh/100 km)")

    # 3. 12 V
    ax = fig.add_axes([0.07, 0.27, 0.40, 0.18])
    if v12:
        ax.plot([d for d, _ in v12], [v for _, v in v12], color=BLUE, linewidth=2)
        ax.axhline(LOW_12V, color=MUTED, linewidth=1)
        ax.text(v12[0][0], LOW_12V + 0.8, f"watch below {LOW_12V:.0f}%", color=MUTED, fontsize=8)
        ax.set_ylim(min(LOW_12V - 5, min(v for _, v in v12) - 3), 101)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d %b"))
    ax.xaxis.set_major_locator(matplotlib.dates.AutoDateLocator(maxticks=4))
    style(ax, "12 V battery, daily low (%)")

    # 4. range at full
    ax = fig.add_axes([0.55, 0.27, 0.40, 0.18])
    if rng:
        ax.plot([d for d, _ in rng], [v for _, v in rng], color=BLUE, linewidth=2)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d %b"))
    ax.xaxis.set_major_locator(matplotlib.dates.AutoDateLocator(maxticks=4))
    style(ax, "Estimated range at 100% (km)")

    # 5. where the energy went
    ax = fig.add_axes([0.25, 0.08, 0.70, 0.11])
    shown = {a: b for a, b in parts.items() if b > 0.05} or parts
    labels, vals = list(shown.keys())[::-1], list(shown.values())[::-1]
    ax.barh(labels, vals, color=BLUE, height=0.6, edgecolor=SURFACE, linewidth=1.5)
    tot = sum(vals) or 1
    for i, v in enumerate(vals):
        ax.text(v + tot * 0.01, i, f"{v:.1f} kWh ({v / tot * 100:.0f}%)", va="center", color=INK2, fontsize=8)
    ax.set_xlim(0, max(vals + [0.1]) * 1.35)
    style(ax, f"Where the energy went this week  ·  regen recovered {regen:.1f} kWh")
    ax.grid(False)
    ax.set_xticks([])
    ax.tick_params(axis="y", labelcolor=INK2)

    fig.text(0.07, 0.025, f"Charging kWh estimated from battery % × {PACK_KWH} kWh pack; "
             f"cost assumes {CHARGE_EFF:.0%} charging efficiency.", fontsize=7, color=MUTED)

    out = HERE / "reports"
    out.mkdir(exist_ok=True)
    pdf = out / f"kona_week_{wk_start:%Y-%m-%d}.pdf"
    fig2 = weather_page(plt, matplotlib, twx, wk_start, wk_end)
    with PdfPages(pdf) as pp:
        pp.savefig(fig, facecolor=SURFACE)
        pp.savefig(fig2, facecolor=SURFACE)
    plt.close(fig)
    plt.close(fig2)

    subj = f"Kona week of {wk_start:%d %b}: {cur['dist']:.0f} km, {fmt(cur['eff'], 1)} kWh/100 km, ~${cost:.2f} charging"
    body = "\n".join(f"{a}: {b}" for a, b in rows) + "\n\nFull report attached (PDF).\n"
    if dry:
        print(subj); print(body); print("PDF:", pdf)
    else:
        send(subj, body, pdf)


def weather_page(plt, matplotlib, twx, wk_start, wk_end):
    fig = plt.figure(figsize=(8.5, 11), facecolor=SURFACE)
    fig.text(0.07, 0.955, "Temperature and efficiency", fontsize=18, weight="bold", color=INK)
    fig.text(0.07, 0.935, "All trips logged so far; this week's trips in blue. Dot size = trip length.",
             fontsize=10, color=INK2)
    rows = [r for r in twx if r["temp_c"] is not None and r["km_per_kwh"] and r["distance_km"]]
    f = wx.fit(twx)

    def style(ax, title, xlabel, ylabel):
        ax.set_title(title, pad=8)
        ax.set_facecolor(SURFACE)
        ax.grid(color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.tick_params(length=0)
        ax.set_xlabel(xlabel, color=INK2)
        ax.set_ylabel(ylabel, color=INK2)

    def scatter(ax, ykey):
        old = [r for r in rows if not (wk_start <= r["t"] < wk_end)]
        new = [r for r in rows if wk_start <= r["t"] < wk_end]
        size = lambda rs: [max(14, min(160, r["distance_km"] * 1.6)) for r in rs]
        if old:
            ax.scatter([r["temp_c"] for r in old], [r[ykey] for r in old], s=size(old),
                       color=EARLIER, edgecolors=SURFACE, linewidths=1.5, label="earlier trips")
        if new:
            ax.scatter([r["temp_c"] for r in new], [r[ykey] for r in new], s=size(new),
                       color=BLUE, edgecolors=SURFACE, linewidths=1.5, label="this week")
        if old and new:
            ax.legend(frameon=False, loc="best", fontsize=8, labelcolor=INK2)

    ax = fig.add_axes([0.10, 0.56, 0.82, 0.32])
    scatter(ax, "km_per_kwh")
    if f:
        xs = [f["tmin"], f["tmax"]]
        ax.plot(xs, [wx.predict(f, x) for x in xs], color=BLUE_DARK, linewidth=2)
    style(ax, "Efficiency vs temperature at trip start", "Temperature (°C)", "km/kWh")

    ax = fig.add_axes([0.10, 0.25, 0.82, 0.22])
    for r in rows:
        r["clim100"] = (r["climate_kwh"] or 0) * 100 / r["distance_km"]
    scatter(ax, "clim100")
    style(ax, "Heating/cooling energy vs temperature", "Temperature (°C)", "Climate kWh per 100 km")

    y = 0.18
    if f:
        ref = wx.predict(f, 15)
        per10 = f["b"] * 10 / ref * 100
        verb = "costs" if per10 > 0 else "gains"
        fig.text(0.07, y, f"Rule of thumb so far: each 10 °C colder {verb} about {abs(per10):.0f}% in km/kWh "
                 f"(fit on {f['n']} trips, {f['tmin']:.0f} to {f['tmax']:.0f} °C).", fontsize=10, color=INK)
    else:
        fig.text(0.07, y, "Not enough spread of temperatures yet for a rule of thumb; it appears once "
                 f"there are {wx.MIN_TRIPS}+ trips across {wx.MIN_TEMP_SPAN}+ °C.", fontsize=10, color=INK2)
    y -= 0.03
    bands = wx.by_band(rows)
    if bands:
        fig.text(0.07, y, "Temperature band", fontsize=9, weight="bold", color=INK)
        for x, h in ((0.30, "Trips"), (0.40, "km"), (0.52, "km/kWh"), (0.66, "Climate share")):
            fig.text(x, y, h, fontsize=9, weight="bold", color=INK)
        for b in bands:
            y -= 0.02
            fig.text(0.07, y, b["band"], fontsize=9, color=INK2)
            fig.text(0.30, y, f"{b['trips']}", fontsize=9, color=INK2)
            fig.text(0.40, y, f"{b['km']:.0f}", fontsize=9, color=INK2)
            fig.text(0.52, y, f"{b['km_per_kwh']:.2f}", fontsize=9, color=INK2)
            fig.text(0.66, y, f"{b['climate_pct']:.0f}%", fontsize=9, color=INK2)
    fig.text(0.07, 0.025, "Weather: Open-Meteo at home location (reanalysis when available, otherwise model). "
             "Trips under 5 km excluded from the fit. Data: trip_weather.csv.", fontsize=7, color=MUTED)
    return fig


if __name__ == "__main__":
    a = sys.argv[1:]
    dry = "--dry-run" in a
    try:
        if "--weekly" in a:
            weekly(dry)
        else:
            daily(dry)
    except Exception as e:
        k.log(f"report ERROR: {e!r}")
        raise
