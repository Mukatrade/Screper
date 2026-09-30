#!/usr/bin/env python3
"""
SAM.gov opportunity monitor
---------------------------
Pulls new SAM.gov notices (solicitations + award notices, worldwide) through the
official Get Opportunities API v2, keeps only the ones that match the dashboard
include keywords (and optional NAICS / PSC codes), drops the dashboard exclude
keywords, removes anything already reported, and emails ONE daily digest.

Environment:
  SAM_API_KEY          required. Public API key from sam.gov -> Account Details.
  GMAIL_USER           Gmail sender (same as scraper.py)
  GMAIL_APP_PASSWORD   Gmail app password
  RECIPIENT_EMAIL      comma-separated recipients
  DASHBOARD_URL        optional. Source of include/exclude keywords.

Search settings (agency, active only, due-date window, notice types, countries,
set-asides, NAICS/PSC, keywords) live in sam_config.json.

State: sites/_sam_seen.json (notice IDs already reported, pruned after 90 days)
       sites/_sam_runs.json (last 10 runs, for the dashboard)
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

import requests

API_URL = "https://api.sam.gov/opportunities/v2/search"
SITES_DIR = Path("sites")
SEEN_PATH = SITES_DIR / "_sam_seen.json"
RUNS_PATH = SITES_DIR / "_sam_runs.json"

CONFIG_PATH = Path("sam_config.json")
PTYPE_CODES = {"solicitation": "o", "combined": "k", "presolicitation": "p",
               "sources_sought": "r", "award": "a"}

SAM_API_KEY = os.environ.get("SAM_API_KEY", "").strip()
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")


def load_config() -> dict:
    cfg = {"agencies": [], "active_only": True, "due_min_days": 0, "due_max_days": 0,
           "notice_types": ["solicitation", "combined", "award"], "countries": [],
           "exclude_us": False, "set_asides": [], "naics": [], "psc": [], "keywords": [],
           "exclude_keywords": [], "lookback_days": 3, "max_items": 60}
    if CONFIG_PATH.exists():
        cfg.update({k: v for k, v in json.loads(CONFIG_PATH.read_text(encoding="utf-8")).items()
                    if not k.startswith("_")})
    return cfg


CFG = load_config()
LOOKBACK_DAYS = int(CFG["lookback_days"])
MAX_ITEMS = int(CFG["max_items"])


def csv_env(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, html: str, plain: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and RECIPIENT_EMAIL):
        print("  [WARN] Gmail env not set, email skipped.")
        return
    to = [r.strip() for r in RECIPIENT_EMAIL.split(",") if r.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg, from_addr=GMAIL_USER, to_addrs=to)
    print(f"  -> Email sent to {', '.join(to)}")


def alert(problem: str) -> None:
    """One alert email when the SAM side itself is broken (bad/expired key etc.)."""
    print(f"[ERROR] {problem}")
    body = (f"The SAM.gov part of the daily tender scan failed.\n\n{problem}\n\n"
            "Most common cause: the SAM.gov API key expired (they last 90 days). "
            "Get a new one at sam.gov > Account Details > Public API Key, then update "
            "the GitHub secret SAM_API_KEY in Mukatrade/Screper.\n\nBummer the scanner")
    try:
        send_email("SAM.gov monitor: ACTION NEEDED", f"<pre style='font-family:Arial'>{escape(body)}</pre>", body)
    except Exception as e:
        print(f"  alert email failed too: {e}")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def load_filters() -> dict:
    inc, exc = [], []
    if DASHBOARD_URL:
        try:
            r = requests.get(f"{DASHBOARD_URL}/api/scraper/filters", timeout=10)
            r.raise_for_status()
            d = r.json()
            inc = [k.lower().strip() for k in d.get("include", []) if k.strip()]
            exc = [k.lower().strip() for k in d.get("exclude", []) if k.strip()]
        except Exception as e:
            print(f"  [WARN] dashboard filters unavailable: {e}")
    inc += [k.lower() for k in CFG["keywords"]]
    exc += [k.lower() for k in CFG["exclude_keywords"]]
    print(f"  include: {inc or 'none'} | exclude: {exc or 'none'}")
    return {"include": sorted(set(inc)), "exclude": sorted(set(exc))}


def word_hit(words: list[str], text: str):
    for w in words:
        if re.search(r"\b" + re.escape(w) + r"\b", text):
            return w
    return None


# ---------------------------------------------------------------------------
# SAM API
# ---------------------------------------------------------------------------

class SamAuthError(Exception):
    pass


def sam_get(params: dict) -> dict:
    q = dict(params, api_key=SAM_API_KEY)
    for attempt in range(3):
        r = requests.get(API_URL, params=q, timeout=60)
        if r.status_code in (401, 403):
            raise SamAuthError(f"SAM.gov rejected the API key (HTTP {r.status_code}): {r.text[:300]}")
        if r.status_code == 429:
            raise SamAuthError(f"SAM.gov daily rate limit reached (HTTP 429): {r.text[:300]}")
        if r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def fetch_all(ptype: str, date_from: str, date_to: str, extra: dict) -> list[dict]:
    out, offset = [], 0
    while True:
        data = sam_get({"postedFrom": date_from, "postedTo": date_to, "ptype": ptype,
                        "limit": 1000, "offset": offset, **extra})
        batch = data.get("opportunitiesData") or []
        out += batch
        total = int(data.get("totalRecords") or 0)
        offset += len(batch)
        if not batch or offset >= total or offset >= 10000:
            break
        time.sleep(1)
    return out


def collect() -> list[dict]:
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    date_to = today.strftime("%m/%d/%Y")
    naics, psc = CFG["naics"], CFG["psc"]
    # Code filters are OR'ed: one query per code. No codes = one open query.
    variants = [{"ncode": c} for c in naics] + [{"ccode": c} for c in psc] or [{}]
    set_asides = CFG["set_asides"] or [None]
    by_id: dict[str, dict] = {}
    for t in CFG["notice_types"]:
        ptype = PTYPE_CODES.get(t)
        if not ptype:
            print(f"  [WARN] unknown notice type in config: {t}")
            continue
        base = {}
        if ptype != "a":
            if CFG["active_only"]:
                base["status"] = "active"
            if CFG["due_min_days"] or CFG["due_max_days"]:
                base["rdlfrom"] = (today + timedelta(days=int(CFG["due_min_days"]))).strftime("%m/%d/%Y")
                base["rdlto"] = (today + timedelta(days=int(CFG["due_max_days"] or 365))).strftime("%m/%d/%Y")
        for extra in variants:
            for sa in set_asides:
                q = dict(base, **extra)
                if sa:
                    q["typeOfSetAside"] = sa
                rows = fetch_all(ptype, date_from, date_to, q)
                print(f"  {t} {extra or ''} {sa or ''}: {len(rows)} notice(s)")
                for n in rows:
                    if n.get("noticeId"):
                        by_id[n["noticeId"]] = n
                time.sleep(1)
    return list(by_id.values())


def passes_config(n: dict) -> bool:
    """Client-side filters the API cannot do well: agency list, country, due window safety net."""
    agency = (n.get("fullParentPathName") or "").upper()
    if CFG["agencies"] and not any(a.upper() in agency for a in CFG["agencies"]):
        return False
    country = (((n.get("placeOfPerformance") or {}).get("country") or {}).get("code") or "").upper()
    if CFG["countries"] and country not in [c.upper() for c in CFG["countries"]]:
        return False
    if CFG["exclude_us"] and country in ("USA", "US"):
        return False
    if CFG["active_only"] and "award" not in (n.get("type") or "").lower():
        if str(n.get("active", "Yes")).lower() not in ("yes", "true"):
            return False
    return True


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def place(n: dict) -> str:
    p = n.get("placeOfPerformance") or {}
    parts = [(p.get("city") or {}).get("name"), (p.get("state") or {}).get("name"),
             (p.get("country") or {}).get("name") or (p.get("country") or {}).get("code")]
    return ", ".join(x for x in parts if x) or "n/a"


def simplify(n: dict) -> dict:
    award = n.get("award") or {}
    awardee = (award.get("awardee") or {}).get("name", "")
    return {
        "id": n.get("noticeId"),
        "title": (n.get("title") or "").strip(),
        "sol": n.get("solicitationNumber") or "",
        "type": n.get("type") or n.get("baseType") or "",
        "agency": (n.get("fullParentPathName") or "").replace(".", " / "),
        "posted": n.get("postedDate") or "",
        "deadline": (n.get("responseDeadLine") or "")[:16].replace("T", " "),
        "naics": n.get("naicsCode") or "",
        "psc": n.get("classificationCode") or "",
        "setaside": n.get("typeOfSetAsideDescription") or "",
        "place": place(n),
        "link": n.get("uiLink") or f"https://sam.gov/opp/{n.get('noticeId')}/view",
        "award_amount": award.get("amount") or "",
        "awardee": awardee,
        "award_date": award.get("date") or "",
    }


def build_digest(sols: list[dict], awards: list[dict], note: str) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"SAM.gov: {len(sols)} new tender(s), {len(awards)} award(s) - {now}"

    def row_html(x: dict, is_award: bool) -> str:
        meta = [f"<b>{escape(x['type'])}</b>", escape(x["agency"])]
        if is_award:
            meta.append(f"Awardee: <b>{escape(x['awardee'] or 'n/a')}</b>")
            if x["award_amount"]:
                meta.append(f"Amount: <b>${escape(str(x['award_amount']))}</b>")
        else:
            meta.append(f"Deadline: <b>{escape(x['deadline'] or 'n/a')}</b>")
            if x["setaside"]:
                meta.append(f"Set-aside: {escape(x['setaside'])}")
        meta.append(f"Place: {escape(x['place'])}")
        meta.append(f"NAICS {escape(x['naics'])} / PSC {escape(x['psc'])}")
        return (f"<li style='margin-bottom:10px'><a href='{escape(x['link'])}'>{escape(x['title'])}</a>"
                + (f" <span style='color:#666'>({escape(x['sol'])})</span>" if x['sol'] else "") + "<br>"
                f"<span style='font-size:12px'>{' | '.join(meta)}</span></li>")

    def row_txt(x: dict, is_award: bool) -> str:
        extra = (f"Awardee: {x['awardee'] or 'n/a'} | Amount: {x['award_amount'] or 'n/a'}"
                 if is_award else f"Deadline: {x['deadline'] or 'n/a'}")
        return f"* {x['title']}" + (f" ({x['sol']})" if x['sol'] else "") + f"\n  {x['agency']} | {extra} | {x['place']}\n  {x['link']}"

    html = "<div style='font-family:Arial,sans-serif;font-size:14px'><p>Hey, Hope you are doing well.</p>"
    plain = ["Hey, Hope you are doing well.", ""]
    if note:
        html += f"<p style='color:#a60'>{escape(note)}</p>"
        plain += [note, ""]
    for label, items, is_award in (("New tenders", sols, False), ("Award notices", awards, True)):
        html += f"<h3>{label} ({len(items)})</h3>"
        plain += [f"{label} ({len(items)})", ""]
        if items:
            html += "<ul>" + "".join(row_html(x, is_award) for x in items) + "</ul>"
            plain += [row_txt(x, is_award) for x in items] + [""]
        else:
            html += "<p>None today.</p>"
            plain += ["None today.", ""]
    html += "<p>Thanks,<br>Bummer the scanner</p></div>"
    plain += ["Thanks,", "Bummer the scanner"]
    return subject, html, "\n".join(plain)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    SITES_DIR.mkdir(exist_ok=True)
    if not SAM_API_KEY:
        alert("SAM_API_KEY secret is missing in the GitHub repo.")
        sys.exit(1)

    print("--- SAM.gov ---")
    filters = load_filters()
    try:
        raw = collect()
    except SamAuthError as e:
        alert(str(e))
        sys.exit(1)
    except Exception as e:
        alert(f"{type(e).__name__}: {e}")
        sys.exit(1)

    seen: dict = {}
    if SEEN_PATH.exists():
        try:
            seen = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except Exception:
            seen = {}

    new_items, dropped_excl, dropped_nomatch = [], 0, 0
    dropped_cfg = 0
    for n in raw:
        if not passes_config(n):
            dropped_cfg += 1
            continue
        x = simplify(n)
        if x["id"] in seen:
            continue
        hay = f"{x['title']} {x['agency']} {x['naics']} {x['psc']}".lower()
        if word_hit(filters["exclude"], hay):
            dropped_excl += 1
            continue
        if filters["include"] and not word_hit(filters["include"], hay):
            dropped_nomatch += 1
            continue
        new_items.append(x)

    note = ""
    has_codes = bool(CFG["naics"] or CFG["psc"] or CFG["agencies"] or CFG["countries"])
    if not filters["include"] and not has_codes and len(new_items) > MAX_ITEMS:
        note = (f"No keywords or NAICS/PSC codes are set, so only the newest {MAX_ITEMS} of "
                f"{len(new_items)} notices are shown. Add include keywords on the dashboard "
                f"or set naics / psc in sam_config.json to narrow it.")
        new_items.sort(key=lambda x: x["posted"], reverse=True)
        new_items = new_items[:MAX_ITEMS]

    sols = sorted([x for x in new_items if "award" not in x["type"].lower()], key=lambda x: x["deadline"] or "9")
    awards = sorted([x for x in new_items if "award" in x["type"].lower()], key=lambda x: x["posted"], reverse=True)
    print(f"  fetched {len(raw)} | new matches {len(new_items)} "
          f"(tenders {len(sols)}, awards {len(awards)}) | off-config {dropped_cfg} | excluded {dropped_excl} | no keyword {dropped_nomatch}")

    # Mark only what was reported as seen (so a later keyword change can still surface items)
    now_iso = datetime.now(timezone.utc).isoformat()
    for x in new_items:
        seen[x["id"]] = now_iso
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.write_text(json.dumps(seen, indent=0), encoding="utf-8")

    runs = []
    if RUNS_PATH.exists():
        try:
            runs = json.loads(RUNS_PATH.read_text(encoding="utf-8"))
        except Exception:
            runs = []
    from collections import Counter
    agencies = Counter((n.get("fullParentPathName") or "?").split(".")[0] for n in raw)
    types = Counter(n.get("type") or "?" for n in raw)
    runs.insert(0, {"ran_at": now_iso, "fetched": len(raw), "tenders": len(sols),
                    "awards": len(awards),
                    "filter_stats": {"off_config": dropped_cfg, "excluded_keyword": dropped_excl,
                                     "no_include_keyword": dropped_nomatch,
                                     "already_seen": len(raw) - dropped_cfg - dropped_excl - dropped_nomatch - len(new_items),
                                     "include_keywords": filters["include"],
                                     "exclude_keywords": filters["exclude"],
                                     "top_agencies_fetched": agencies.most_common(15),
                                     "types_fetched": types.most_common()},
                    "results": sols + awards})
    RUNS_PATH.write_text(json.dumps(runs[:10], indent=2), encoding="utf-8")

    if new_items:
        subject, html, plain = build_digest(sols, awards, note)
        print(f"Sending: {subject}")
        send_email(subject, html, plain)
    else:
        print("No new SAM.gov matches, no email.")


if __name__ == "__main__":
    main()
