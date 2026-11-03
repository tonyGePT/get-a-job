#!/usr/bin/env python3
"""Daily job pipeline: discover -> qualify -> prepare -> apply -> verify -> trace -> digest.

Stages 2 and 4 need an LLM agent (JD judgement and browser form filling); every
other stage is deterministic Python so a bad model call can never corrupt state.
A day with zero candidates skips only qualify/prepare/apply: verify, trace
and digest always run, because reconciliation is not intake.

Runs natively on Windows (C:\\Python313). Scheduled through daily-hidden.vbs.

Usage:
  python daily.py                  full run
  python daily.py --discover-only  cheap check, no LLM, no browser
  python daily.py --no-apply       everything except form submission
  python daily.py --no-trace       skip TraceHire runs
  python daily.py --days 3         override config.json query.date_posted_days
"""

import argparse
import msvcrt
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import state as S  # noqa: E402

PY = sys.executable


class Failed(Exception):
    """A deterministic stage failed: stop, but still send a partial digest."""


def log(msg=""):
    print(msg, flush=True)


def stage(name):
    log()
    log("=== [%s] %s ===" % (time.strftime("%H:%M:%S"), name))
    try:
        S.heartbeat(name.split()[0])
    except Exception as e:  # a heartbeat must never stop the run
        log("heartbeat failed: %s" % e)


def run(args, required=True, label=None):
    """Run a child with its output in our log. Returns True on exit 0."""
    rc = subprocess.run(args, cwd=ROOT, stdout=sys.stdout, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL).returncode
    if rc == 0:
        return True
    if required:
        raise Failed("%s exited %s" % (label or args[1], rc))
    log("%s returned %s; continuing" % (label or os.path.basename(args[1]), rc))
    return False


def script(name, *args, **kw):
    return run([PY, os.path.join(ROOT, name)] + list(args), **kw)


def agent(prompt_file, label):
    """Run one omp agent pass, with NO time limit (removed 2026-11-03: the model providers congest
    during Asian business hours, and a killed stage lost a whole day's verdicts). --config overlays
    omp-pipeline.yml (models, retries, fallback) for this run only; the global omp config is not touched."""
    prompt = open(os.path.join(ROOT, "prompts", prompt_file), encoding="utf-8").read()
    return run(["omp", "-p", "--no-title", "--cwd", ROOT,
                "--config", os.path.join(ROOT, "omp-pipeline.yml"), prompt],
               required=False, label=label)


def pipeline(args):
    stage("discover (window %s)" % ("%sd" % args.days if args.days else "from config"))
    script("discover.py", *(["--days", str(args.days)] if args.days else []))
    if args.discover_only:
        log("discover-only: done")
        return

    new = len(S.read_json(S.path("candidates-latest.json"), {}).get("candidates", []))
    if new == 0:
        log("nothing new today: skipping qualify/prepare/apply, reconciling the rest")
    else:
        stage("qualify (%d candidates)" % new)
        agent("qualify.md", "qualify stage")
        stage("prepare (tailored resumes)")
        prepared = script("prepare.py", required=False, label="prepare")
        if not prepared:
            log("prepare failed; nothing will be applied this run")
        elif args.no_apply:
            log("--no-apply: skipping form submission")
        else:
            stage("apply")
            agent("apply.md", "apply stage")
            script(os.path.join("lib", "notify.py"), "scan", required=False, label="captcha notify")

    stage("verify receipts")
    script("verify.py")

    if not args.no_trace:
        stage("tracehire start")
        script("trace.py", "start", required=False, label="trace start")
        stage("tracehire poll")
        script("trace.py", "poll", required=False, label="trace poll")
        # upstream extract failures are worth exactly one more attempt (cost-capped in trace.py)
        stage("tracehire retry")
        script("trace.py", "retry", required=False, label="trace retry")

    stage("digest")
    script("digest.py")
    log("[%s] done" % time.strftime("%H:%M:%S"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int)
    ap.add_argument("--no-apply", action="store_true")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--discover-only", action="store_true")
    args = ap.parse_args()

    S.ensure_dirs()
    logf = open(S.path("logs", "%s.log" % time.strftime("%Y-%m-%d")), "a",
                encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = logf

    # one run at a time; a stuck run must not stack up with the next one
    lock = open(os.path.join(ROOT, ".lock"), "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("[%s] another run holds the lock; exiting" % time.strftime("%H:%M:%S"))
        return 0

    try:
        pipeline(args)
        return 0
    except Exception as e:  # Failed, or a bug: either way the user gets a digest
        log("[%s] pipeline stopped (%s); sending partial digest" % (time.strftime("%H:%M:%S"), e))
        script("digest.py", required=False, label="digest")
        return 1


if __name__ == "__main__":
    sys.exit(main())
