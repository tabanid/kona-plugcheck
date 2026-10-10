#!/usr/bin/env python3
"""
One-time: verify this PC with Hyundai Canada using a one-time code.
After this, Hyundai recognises the PC and the checker logs in without a code.
"""
import os
import sys
import configparser

from hyundai_kia_connect_api import VehicleManager
from hyundai_kia_connect_api.ApiImpl import OTPRequest
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE

import kona_plug_check as k

m = configparser.ConfigParser()
m.read(k.MONITOR_CFG)
c = m["monitor"]
mgr = VehicleManager(region=int(c["region"]), brand=int(c["brand"]),
                     username=c["username"], password=c["password"], pin=c["pin"])

print("Signing in to Hyundai Canada...")
result = mgr.login()
if result is True:
    print("No code needed: this PC is already recognised.")
elif isinstance(result, OTPRequest):
    opts = []
    if result.has_email:
        opts.append(("1", OTP_NOTIFY_TYPE.EMAIL, f"email to {result.email}"))
    if result.has_sms:
        opts.append(("2", OTP_NOTIFY_TYPE.SMS, f"text to {result.sms}"))
    print("Hyundai wants a one-time code. Send it by:")
    for key, _, label in opts:
        print(f"  {key}) {label}")
    pick = input("Choose: ").strip() or opts[0][0]
    method = next((o[1] for o in opts if o[0] == pick), opts[0][1])
    mgr.send_otp(method)
    code = input("Enter the code you received: ").strip()
    mgr.verify_otp_and_complete_login(code)
    print("Code accepted.")
else:
    sys.exit(f"Unexpected login result: {result!r}")

k.save_token(mgr.token)
mgr.update_all_vehicles_with_cached_state()
v = next(iter(mgr.vehicles.values()))
print(f"OK: found {v.name or v.model}  battery {v.ev_battery_percentage}%  "
      f"plugged={v.ev_battery_is_plugged_in}")
print("This PC is now verified. Re-run setup.sh.")
