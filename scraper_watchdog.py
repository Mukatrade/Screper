#!/usr/bin/env python3
"""
scraper_watchdog.py - catches the "scraper still shows green but is silently
broken" failure mode (the Anthropic-credit incident, 2026-08-17 to
2026-09-22: every run reported success while diff analysis failed 100% of
the time behind the scenes).

Runs daily, after both tender scrapers' normal schedules, and checks:

  1. Mukatrade/Screper (this repo): did the latest daily_scan.yml run
     complete, and did its analysis step actually succeed (grepped from the
     run's own log - "Change detected" attempts vs "both failed for"
     errors), not just "did the job exit 0".
  2. Mukatrade/Screper: has a run happened at all in the last 30 hours
     (missed/disabled schedule).
  3. Mukatrade/embassy-monitor-cloud (cross-repo, read-only): same
     staleness + conclusion check. No LLM dependency there, so a plain
     failed/missing run is the whole signal.

Alert-only delivery (house style, see CLAUDE.md rule 41/43): NO email when
everything is fine - only when Yaron must actually act. A state file
(scraper_watchdog_state.json, committed back to the repo) makes an open
problem repeat daily as "still open since <date>" until it clears, instead
of being reported once and forgotten.

Required environment variables:
  GITHUB_TOKEN    - provided automatically by Actions, read access to this repo
  PAT_TOKEN       - same PAT daily_scan.yml already uses; needs read access
                    to Mukatrade/embassy-monitor-cloud too (best-effort: if it
                    can't reach that repo, this watchdog logs it and moves on
                    rather than failing the whole check)
  GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL - same as scraper.py
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
PAT_TOKEN      = os.environ.get("PAT_TOKEN", "")
GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]

STATE_FILE = "scraper_watchdog_state.json"
STALE_HOURS = 30
API = "https://api.github.com"


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def latest_run(repo, workflow_file, token):
    r = requests.get(
        f"{API}/repos/{repo}/actions/workflows/{workflow_file}/runs",
        headers=_headers(token), params={"per_page": 5}, timeout=30,
    )
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])
    return runs[0] if runs else None


def run_log_text(repo, run_id, token):
    r = requests.get(f"{API}/repos/{repo}/actions/runs/{run_id}/logs",
                      headers=_headers(token), timeout=60)
    r.raise_for_status()
    # The logs endpoint returns a zip; decode best-effort as text for grepping
    # (GitHub's zip entries are plain text logs, readable via zipfile).
    import io
    import zipfile
    text = []
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".txt"):
                text.append(zf.read(name).decode("utf-8", "replace"))
    return "\n".join(text)


def check_screper():
    """Returns (problem: str|None, detail: str)."""
    run = latest_run("Mukatrade/Screper", "daily_scan.yml", GITHUB_TOKEN or PAT_TOKEN)
    if not run:
        return "Screper: no workflow runs found at all", ""

    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > STALE_HOURS:
        return (f"Screper hasn't run in {age_hours:.0f}h (last: {run['created_at']})",
                "Check the daily_scan.yml schedule/cron and that the repo isn't archived or Actions disabled.")

    if run["status"] != "completed" or run["conclusion"] != "success":
        return (f"Screper's last run did not succeed (status={run['status']}, conclusion={run['conclusion']})",
                run["html_url"])

    # Job succeeded - now check whether the analysis step actually worked,
    # not just whether the process exited 0 (this is the exact gap that let
    # the Anthropic-credit outage run silently for 5 weeks).
    try:
        log = run_log_text("Mukatrade/Screper", run["id"], GITHUB_TOKEN or PAT_TOKEN)
    except Exception as exc:
        return None, f"Screper OK (could not fetch log to verify analysis step: {exc})"

    attempts = len(re.findall(r"Change detected:", log))
    failures = len(re.findall(r"both failed for", log))
    no_key = "diff analysis cannot run" in log

    if no_key:
        return ("Screper's analysis provider keys (GROQ_API_KEY/GEMINI_API_KEY) are missing",
                "The run exited early - check repo secrets.")
    if attempts > 0 and failures >= attempts:
        return (f"Screper ran but every diff analysis call failed ({failures}/{attempts}) "
                f"- Groq and Gemini both down or keys invalid",
                run["html_url"])
    if attempts > 0 and failures > 0:
        return (None, f"Screper OK - {failures}/{attempts} analysis calls failed but the rest succeeded (provider blip)")

    return None, f"Screper OK - {attempts} sites analysed, run {run['html_url']}"


def check_embassy_monitor():
    if not PAT_TOKEN:
        return None, "embassy-monitor-cloud: skipped (no PAT_TOKEN)"
    try:
        run = latest_run("Mukatrade/embassy-monitor-cloud", "monitor.yml", PAT_TOKEN)
    except Exception as exc:
        return None, f"embassy-monitor-cloud: could not check (no cross-repo access?): {exc}"
    if not run:
        return "embassy-monitor-cloud: no workflow runs found at all", ""
    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > STALE_HOURS:
        return (f"embassy-monitor-cloud hasn't run in {age_hours:.0f}h (last: {run['created_at']})", "")
    if run["status"] != "completed" or run["conclusion"] != "success":
        return (f"embassy-monitor-cloud's last run did not succeed (status={run['status']}, conclusion={run['conclusion']})",
                run["html_url"])
    return None, f"embassy-monitor-cloud OK - run {run['html_url']}"


def send_alert(problems, notes, state, today):
    lines = []
    for p in problems:
        since = state.get(p, today)
        if since == today:
            state[p] = today
            lines.append(f"- {p}  (new today)")
        else:
            lines.append(f"- {p}  (still open since {since})")
    body = (
        "The scraper watchdog found something that needs a look:\n\n"
        + "\n".join(lines)
        + "\n\n--- everything else checked ---\n"
        + "\n".join(f"- {n}" for n in notes)
        + "\n"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "[SCRAPER PROBLEM] tender scraper watchdog"
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg, from_addr=GMAIL_USER,
                             to_addrs=[r.strip() for r in RECIPIENT_EMAIL.split(",") if r.strip()])
    print("Alert email sent.")


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE))
        except Exception:
            state = {}

    problems, notes = [], []
    p1, n1 = check_screper()
    p2, n2 = check_embassy_monitor()
    for p in (p1, p2):
        if p:
            problems.append(p)
    for n in (n1, n2):
        if n:
            notes.append(n)

    # --self-test injects a fake problem so the alert path (subject/body/send)
    # can be proven end-to-end without waiting for a real outage. Never runs
    # on schedule - only via explicit CLI flag.
    if "--self-test" in sys.argv:
        problems.append("TEST: watchdog self-test (safe to ignore/delete)")
        notes.append("This is a manual self-test run, not a real finding.")

    # Clear resolved problems out of state so they don't linger forever.
    state = {k: v for k, v in state.items() if k in problems}

    print("Problems:", problems or "none")
    print("Notes:", notes)

    if problems:
        send_alert(problems, notes, state, today)

    if "--self-test" not in sys.argv:
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
