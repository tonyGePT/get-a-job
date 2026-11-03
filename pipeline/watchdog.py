#!/usr/bin/env python
"""Silence detector for the daily pipeline.

The failure that matters is not a crash — daily.py logs and exits — it is a run
that never happened: Task Scheduler disabled, the PC asleep at 08:10, omp hung.
This checks the heartbeat and leaves an ALERT file plus a Windows notification
when the pipeline has gone quiet.

Usage: python watchdog.py [--max-age-hours 26] [--quiet]
"""

import argparse
import subprocess
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import state as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=26.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    hb = S.read_json(S.path("heartbeat.json"), None)
    age_h = None
    if hb and hb.get("at"):
        try:
            age_h = (time.time() - time.mktime(time.strptime(hb["at"], "%Y-%m-%dT%H:%M:%S"))) / 3600
        except ValueError:
            age_h = None

    stale = hb is None or age_h is None or age_h > args.max_age_hours
    if not stale:
        if not args.quiet:
            print("ok: last stage %s at %s (%.1fh ago)" % (hb.get("stage"), hb.get("at"), age_h))
        return 0

    why = ("no heartbeat file" if hb is None
           else "last stage %s at %s (%.1fh ago)" % (hb.get("stage"), hb.get("at"), age_h or -1))
    msg = "jobhunt ALERT %s: %s — check Task Scheduler and state/logs/" % (
        time.strftime("%Y-%m-%d %H:%M"), why)
    open(S.path("ALERT"), "w", encoding="utf-8").write(msg + "\n")
    toast(msg)
    print(msg)
    return 4


def toast(msg):
    """Best-effort Windows notification: a stale pipeline must be visible, not silent."""
    ps = ("[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null;"
          "[reflection.assembly]::loadwithpartialname('System.Drawing') | Out-Null;"
          "$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
          "$n.ShowBalloonTip(10000,'jobhunt','%s',"
          "[System.Windows.Forms.ToolTipIcon]::Warning)" % msg.replace("'", ""))
    try:
        subprocess.run(["powershell.exe",
                        "-NoProfile", "-Command", ps], capture_output=True, timeout=30)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
