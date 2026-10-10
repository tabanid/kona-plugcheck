#!/usr/bin/env python3
"""
Nightly backup of this whole project to the NAS: code, config, logins, data, reports.

  <dest>/current/                    mirror of the project folder (minus venv/caches)
  <dest>/daily/kona-YYYY-MM-DD.tar.gz   one snapshot per day, last `keep_days` kept

Settings in plugcheck.cfg [backup]. If the NAS isn't mounted, nothing is written
and you get an alert, so a missed backup doesn't go unnoticed.
"""
import datetime as dt
import os
import shutil
import sys
import tarfile
from pathlib import Path

import kona_plug_check as k

B = k.CFG["backup"] if k.CFG.has_section("backup") else {}
DEST = Path(B.get("dest", "~/NASstorage2/BackupApps/kona-plugcheck")).expanduser()
MOUNT = Path(B.get("nas_mount", "~/NASstorage2")).expanduser()
KEEP = int(B.get("keep_days", 60))
SKIP = {"venv", "__pycache__", ".git"}


def skip(_dir, names):
    return [n for n in names if n in SKIP or n.endswith((".tmp", ".pyc"))]


def main():
    real = MOUNT.resolve()
    mounted = real.is_dir() and (os.path.ismount(real) or any(real.iterdir()))
    if not mounted or not os.access(real, os.W_OK):
        raise RuntimeError(f"NAS not mounted or not writable at {MOUNT}")
    (DEST / "daily").mkdir(parents=True, exist_ok=True)

    # 1. mirror (adds and updates; never deletes on the NAS)
    shutil.copytree(k.HERE, DEST / "current", ignore=skip, dirs_exist_ok=True)

    # 2. dated snapshot
    snap = DEST / "daily" / f"kona-{dt.date.today():%Y-%m-%d}.tar.gz"
    tmp = snap.with_suffix(".partial")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(k.HERE, arcname="plugcheck",
                filter=lambda ti: None if set(Path(ti.name).parts) & SKIP else ti)
    tmp.replace(snap)

    # 3. prune old snapshots
    cutoff = dt.date.today() - dt.timedelta(days=KEEP)
    pruned = 0
    for f in (DEST / "daily").glob("kona-*.tar.gz"):
        try:
            if dt.date.fromisoformat(f.name[5:15]) < cutoff:
                f.unlink()
                pruned += 1
        except ValueError:
            pass
    k.log(f"backup ok: {snap.name} ({snap.stat().st_size // 1024} KB), pruned {pruned}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        k.log(f"backup ERROR: {e!r}")
        try:
            k.notify("Kona backup failed", f"{e}. Is the NAS mounted?")
        except Exception:
            pass
        sys.exit(1)
