# Kona plug-in reminder — setup

**Quick way:** run `bash ~/konaev/plugcheck/setup.sh`. It does steps 1, 2, 5 and 6 below and prints
the two topic names your phones need. (Your topic names, home location and other settings are in `plugcheck.cfg`.)
Then do the phone steps (3, 4 and the second-phone section).

**How it works:** when your phone disconnects from the Kona's Bluetooth while you're at home,
it sends a signal. A listener on this PC waits 10 minutes, then asks BlueLink **once** for fresh data.
Then it **always reports**: "Kona plugged in" or "PLUG IN THE KONA", plus the battery % and
whose phone sent the signal. (Later we can switch it to alert only when the car isn't plugged in.)
Your BlueLink login lives in `bluelink.cfg` in this folder (readable by you only).

## 1. Install on the PC (once)

```bash
cd ~/konaev/plugcheck
chmod +x run_plugcheck.sh
python3 -m venv venv
./venv/bin/pip install hyundai_kia_connect_api
```

## 2. Pick your two private topics

Edit `plugcheck.cfg` and replace both `CHANGE-ME` names with something long and random:
- `ntfy_topic`: where the **alerts** go (your phone subscribes to this one)
- `arrival_topic`: where the phone **sends** "I'm home" (the PC listens to this one)

Anyone who knows a topic name can read it or post to it, so don't use guessable names.

## 3. Phone: get alerts

1. Install **ntfy** from the Play Store.
2. Tap **+** and subscribe to your `ntfy_topic`.
3. On the PC, run `./run_plugcheck.sh --test`. A test alert should arrive on the phone.

Want a real SMS instead? Set `notify = twilio` and fill in `[twilio]`.

## 4. Phone: send the arrival signal (MacroDroid, free)

Install **MacroDroid** from the Play Store, then **Add Macro**:

- **Trigger:** Connectivity → Bluetooth Event → *Device Disconnected* → pick the Kona.
- **Action:** **HTTP Request** (search for it; not "HTTP Server Request"). Leave the method as GET.
  - URL: `https://ntfy.sh/<your arrival_topic>/publish?message=YourName`
  - The `message=` part is your name; it tells the alert who arrived.
- **Constraint:** Location → *Geofence* → inside your home area. Or use Connectivity →
  *Wi-Fi SSID connected* → your home Wi-Fi, if the phone joins it as you pull in.

Give MacroDroid location permission, and in Android battery settings set MacroDroid to
**Unrestricted** so it isn't put to sleep.

### A second phone

Repeat steps 3 and 4 on the other phone:
- **ntfy:** subscribe to the same `ntfy_topic` if they want the alerts too.
- **MacroDroid:** set up the same macro with the same `arrival_topic`, but put their name after `message=`.
- **Bluetooth:** that phone has to be paired with the Kona for the disconnect trigger to work.

If you both arrive together, the two signals merge into **one** check, and the alert names you both.

## 5. Check the PC can read the car

```bash
./run_plugcheck.sh --status
```

You should see something like `home=True (4 m) plugged=2 SOC=80%`.
`plugged` is 0 when unplugged and 1 or 2 when plugged in.

## 6. Start the listener (runs in the background, restarts at boot)

```bash
mkdir -p ~/.config/systemd/user
cp kona-arrival.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kona-arrival
loginctl enable-linger $USER     # keeps it running when you're logged out
```

To make sure it's working: `journalctl --user -u kona-arrival -f`, then post a test signal from
another terminal with `curl -d arrived https://ntfy.sh/<your arrival_topic>`.
About 10 minutes later the PC will check the car.

## Evening checks

Three checks, at 9:30, 10:15 and 11:00 pm (`crontab -e`):

```
30 21 * * * $HOME/konaev/plugcheck/run_plugcheck.sh --evening >> $HOME/konaev/plugcheck/cron.log 2>&1
15 22 * * * $HOME/konaev/plugcheck/run_plugcheck.sh --evening >> $HOME/konaev/plugcheck/cron.log 2>&1
0 23 * * *  $HOME/konaev/plugcheck/run_plugcheck.sh --evening >> $HOME/konaev/plugcheck/cron.log 2>&1
```

How the evening checks behave:
- **9:30:** asks for fresh data (one car wake-up).
- **Later checks:** use cached data only.
- **Before alerting:** if a later check thinks the car is unplugged, it confirms with fresh data first.
- **Stopping early:** once the car is confirmed home and plugged in, the rest of that night's checks are skipped.
- **Alerts:** you only hear from it if the car is home and NOT plugged in. After the first reminder,
  the 10:15 and 11:00 checks send "still not plugged in" follow-ups, unless the battery was above 70%
  at the first reminder (`followup_max_soc` in `plugcheck.cfg`).

## Car log

Every time the checker reads the car it also records what it saw. A late-night snapshot at 11:45 pm
(cached data only, so it never wakes the car) guarantees at least one row per day. Add to `crontab -e`:

```
45 23 * * * $HOME/konaev/plugcheck/run_plugcheck.sh --nightly >> $HOME/konaev/plugcheck/cron.log 2>&1
```

- **`car_log.csv`**, one row per reading: where the car was, distance from home, plugged/charging,
  battery %, **12 V battery %**, odometer, range, and what triggered the reading
  (`arrival`, `evening`, `nightly`, …).
- **`trips.csv`**, one row per trip, as reported by Hyundai: distance, energy used, split into
  drivetrain / climate / accessories / battery care, regen, and kWh/100 km.
- **`history/monitor_2024.csv`**: your Nov–Dec 2024 log from the old monitor.

Both CSVs open in R or a spreadsheet. Watch `battery_12v_pct` over weeks for early 12 V trouble.

## Daily note and weekly report (email)

- **Daily note**, about 6 am: battery and range now, overnight charge (kWh and cost), yesterday's driving,
  12 V battery, and anything that needs attention.
- **Weekly PDF**, Monday about 6:15 am, covering the previous Mon–Sun: distance, efficiency, charging cost,
  12 V trend, range at 100%, where the energy went, and how the reminder did.
  Copies are kept in `reports/`.

One-time setup:
1. **Install the charting library:** `./venv/bin/pip install matplotlib`
2. **Create a Gmail app password:** Google Account → Security → 2-Step Verification → **App passwords**.
   Make one called "Kona" and paste the 16 letters into `email.cfg` (then `chmod 600 email.cfg`).
3. **Test:** `./venv/bin/python kona_report.py --daily` (you should get an email), and
   `./venv/bin/python kona_report.py --weekly --dry-run` to build a PDF without sending it.
4. **Schedule:** add to `crontab -e`:

```
0 6 * * * cd $HOME/konaev/plugcheck && ./venv/bin/python kona_report.py --daily >> cron.log 2>&1
15 6 * * 1 cd $HOME/konaev/plugcheck && ./venv/bin/python kona_report.py --weekly >> cron.log 2>&1
```

Settings (rate 18.5¢/kWh, battery size 64.8 kWh, 12 V warning level) are under `[report]` in `plugcheck.cfg`.

## Weather and efficiency

The daily note and weekly report fetch weather from Open-Meteo (free, no key) for the home location.
No extra schedule is needed.

- **`weather_hourly.csv`**: raw hourly weather. Each row is labelled `archive` (final reanalysis,
  about 5 days behind) or `forecast` (model data for recent days and the next few). Archive data
  replaces forecast data as it arrives.
- **`trip_weather.csv`**: every trip joined to its weather. It's rebuilt from the two raw files on
  every run, so changes to the analysis apply to all past data.
- **Daily note:** yesterday's temperatures, and once there's enough data (15+ trips over 8+ °C),
  an estimate of today's km/kWh and full-charge range.
- **Weekly PDF:** a conditions row on page 1, and page 2 with km/kWh and heating energy against
  temperature, a rule of thumb, and a table by temperature band.

By hand:
- `./venv/bin/python kona_weather.py --summary` prints the current analysis.
- `./venv/bin/python kona_weather.py --backfill 2026-01-01 2026-03-31` fetches a past date range.
- `./venv/bin/python kona_weather.py --rebuild` rebuilds `trip_weather.csv`.

## Notes

- **BlueLink use:** one forced refresh per arrival home, whichever phone sends it plus usually one for the evening checks (two on a night it alerts).
- **Countdown restarts:** if the phone drops and reconnects to Bluetooth several times, the
  countdown restarts, so you still get only one check.
- **False triggers:** arriving home in another car, or turning Bluetooth off at home, gets you a
  "Kona not home?" message. That's harmless, and it shows where the car is.
- **The PC has to be on:** if it's asleep, arrivals are missed.
- **Settings:** `delay_minutes` and `home_radius_m` are in `plugcheck.cfg`.
- **Log:** every check is logged to `plugcheck.log`.
- **Backup:** every night at 12:15 am the whole project (data and logins included) is copied to
  `~/NASstorage2/BackupApps/kona-plugcheck/`: a mirror in `current/` plus a dated snapshot in `daily/`,
  60 days kept. If the NAS isn't mounted you get an alert. Settings are under `[backup]` in `plugcheck.cfg`.
  Cron line: `15 0 * * * cd $HOME/konaev/plugcheck && ./venv/bin/python kona_backup.py >> cron.log 2>&1`
- **Fresh install:** copy `plugcheck.example.cfg` to `plugcheck.cfg` and `email.example.cfg` to `email.cfg`,
  fill them in (or restore them from the NAS backup), then run `setup.sh`.
- **Private files:** `bluelink.cfg`, `email.cfg` and `token.json` hold logins. Keep them out of git
  and backups you share (see `.gitignore`).
