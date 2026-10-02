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

State: sites/_sam_state.json  one entry per solicitation number. A tender is
       reported as NEW once; afterwards only as an UPDATE (deadline moved,
       amended, awarded). Pruned 120 days after last activity.
       sites/_sam_runs.json   last 10 runs, for the dashboard.
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

import requests

API_URL = "https://api.sam.gov/opportunities/v2/search"
SITES_DIR = Path("sites")
SEEN_PATH = SITES_DIR / "_sam_seen.json"      # legacy, used once to seed the state
STATE_PATH = SITES_DIR / "_sam_state.json"     # one entry per solicitation
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
           "exclude_keywords": [], "lookback_days": 3, "max_items": 60,
           "use_dashboard_include": False, "use_dashboard_exclude": True,
           "goods_only": False, "exclude_psc_prefixes": [], "exclude_offices": [],
           "awards_only_for_my_bids": True}
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

def send_email(subject: str, html: str, plain: str, attachment: Path | None = None) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and RECIPIENT_EMAIL):
        print("  [WARN] Gmail env not set, email skipped.")
        return
    to = [r.strip() for r in RECIPIENT_EMAIL.split(",") if r.strip()]
    msg = MIMEMultipart("mixed")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(to)
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(plain, "plain", "utf-8"))
    body.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(body)
    if attachment and attachment.exists():
        part = MIMEApplication(attachment.read_bytes(), Name=attachment.name)
        part["Content-Disposition"] = f'attachment; filename="{attachment.name}"'
        msg.attach(part)
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
            if CFG["use_dashboard_include"]:
                inc = [k.lower().strip() for k in d.get("include", []) if k.strip()]
            if CFG["use_dashboard_exclude"]:
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
    if any(o.upper() in agency for o in CFG["exclude_offices"]):
        return False
    country = (((n.get("placeOfPerformance") or {}).get("country") or {}).get("code") or "").upper()
    if CFG["countries"] and country not in [c.upper() for c in CFG["countries"]]:
        return False
    if CFG["exclude_us"] and country in ("USA", "US"):
        return False
    psc = (n.get("classificationCode") or "").strip().upper()
    if CFG["goods_only"] and not (psc[:1].isdigit()):
        return False
    if any(psc.startswith(p) for p in CFG["exclude_psc_prefixes"]):
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


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def is_award(x: dict) -> bool:
    return "award" in x["type"].lower()


def office(x: dict) -> str:
    o = x["agency"].split(" / ")[-1].title() if x["agency"] else ""
    return re.sub(r"\bUs\b", "US", re.sub(r"\bDla\b", "DLA", o))


def money(v) -> str:
    try:
        return f"${float(str(v).replace(',', '')):,.0f}"
    except Exception:
        return f"${v}" if v else "-"


def group_of(x: dict) -> str:
    a = x["agency"].upper()
    if "STATE, DEPARTMENT OF" in a:
        return "state"
    if "DLA" in a or "DEFENSE LOGISTICS" in a:
        return "dla"
    return "other"


GROUP_TITLE = {"state": "Embassies / State Dept", "other": "Defense and other agencies",
               "dla": "DLA small buys"}


def days_left(deadline: str):
    try:
        d = datetime.strptime(deadline[:10], "%Y-%m-%d").date()
        return (d - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


def key_of(x: dict) -> str:
    return (x["sol"] or x["id"] or "").strip().upper()


# ---------------------------------------------------------------------------
# State: what has already been reported
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def legacy_seen() -> set:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")).keys())
        except Exception:
            pass
    return set()


def classify(items: list[dict], state: dict) -> tuple[list, list, list]:
    """Return (new_tenders, updates, new_awards). Updates carry a 'change' text.
    Mutates state so every notice is remembered, shown or not."""
    seed = legacy_seen() if not state else set()
    today = datetime.now(timezone.utc).date().isoformat()
    new, updates, awards = [], [], []
    # oldest first so an original notice is recorded before its amendment
    for x in sorted(items, key=lambda i: i["posted"]):
        if is_award(x):
            continue                          # awards are handled by my_bid_awards()
        k = key_of(x)
        if not k:
            continue
        rec = state.get(k)
        if rec is None:
            state[k] = {"title": x["title"], "deadline": x["deadline"], "type": x["type"],
                        "notices": [x["id"]], "first_seen": today, "last_seen": today,
                        "awarded": is_award(x)}
            if x["id"] in seed:
                continue                      # already shown by the old version
            (awards if is_award(x) else new).append(x)
            continue
        if x["id"] in rec["notices"]:
            continue                          # same notice as before, nothing new
        rec["notices"].append(x["id"])
        rec["last_seen"] = today
        if is_award(x):
            if rec.get("awarded"):
                continue
            rec["awarded"] = True
            amt = f" for {money(x['award_amount'])}" if x["award_amount"] else ""
            x["change"] = f"Awarded to {x['awardee'] or 'n/a'}{amt}"
        elif x["deadline"] and rec.get("deadline") and x["deadline"][:10] != rec["deadline"][:10]:
            x["change"] = f"Deadline moved {rec['deadline'][:10]} to {x['deadline'][:10]}"
        elif x["title"] != rec.get("title"):
            x["change"] = "Amended (title changed)"
        else:
            x["change"] = "Amended / new version posted"
        rec.update({"title": x["title"], "deadline": x["deadline"] or rec.get("deadline"),
                    "type": x["type"]})
        updates.append(x)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    for k in [k for k, v in state.items() if not k.startswith("_") and v.get("last_seen", "") < cutoff]:
        del state[k]
    return new, updates, awards


# ---------------------------------------------------------------------------
# Awards: only for tenders we bid on
# ---------------------------------------------------------------------------

BIDS_PATH = Path("sam_bids.txt")


def norm_sol(v: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (v or "").upper())


def manual_bids() -> set:
    if not BIDS_PATH.exists():
        return set()
    return {norm_sol(l.split("#")[0]) for l in BIDS_PATH.read_text(encoding="utf-8").splitlines()
            if norm_sol(l.split("#")[0])}


def gmail_service():
    raw = os.environ.get("GMAIL_TOKEN_JSON", "").strip()
    if not raw:
        print("  [WARN] GMAIL_TOKEN_JSON not set, award check uses sam_bids.txt only")
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_info(json.loads(raw), ["https://mail.google.com/"])
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"  [WARN] Gmail unavailable for award check: {e}")
        return None


def sent_mentions(svc, sols: list[str]) -> set:
    """Return the solicitation numbers that appear in Muka's sent mail.
    Checks 15 numbers per query; only drills into chunks that hit."""
    def hit(q):
        try:
            return bool(svc.users().messages().list(userId="me", q=q, maxResults=1).execute().get("messages"))
        except Exception as e:
            print(f"  [WARN] gmail query failed: {e}")
            return False
    # Ignore our own internal reports (they list every solicitation number).
    own = ' -subject:"SAM.gov" -subject:"Sarmad Monitor" -subject:digest -subject:Benny -to:yaron@mukatrade.com -to:info@mukatrade.com'
    found = set()
    for i in range(0, len(sols), 15):
        chunk = sols[i:i + 15]
        if not hit("in:sent (" + " OR ".join(f'"{s}"' for s in chunk) + ")" + own):
            continue
        for s in chunk:
            if hit(f'in:sent "{s}"' + own):
                found.add(s)
    return found


def my_bid_awards(award_items: list[dict], state: dict) -> list[dict]:
    """Awards whose solicitation Muka bid on, each reported once."""
    reported = state.setdefault("_awards_reported", {})
    cand = [x for x in award_items if x["sol"] and norm_sol(x["sol"]) not in reported]
    if not cand:
        return []
    mine_manual = manual_bids()
    svc = gmail_service()
    # Short numbers (e.g. "P270") match random text in mail, so mail check needs 8+ chars.
    sols = sorted({x["sol"] for x in cand if len(norm_sol(x["sol"])) >= 8})
    in_mail = sent_mentions(svc, sols) if svc else set()
    out, today = [], datetime.now(timezone.utc).date().isoformat()
    for x in cand:
        if x["sol"] in in_mail or norm_sol(x["sol"]) in mine_manual:
            reported[norm_sol(x["sol"])] = today
            out.append(x)
    print(f"  awards checked {len(sols)} solicitation(s), {len(out)} on our bids")
    return out


# ---------------------------------------------------------------------------
# Excel attachment (everything, nothing hidden)
# ---------------------------------------------------------------------------

def build_excel(new: list, updates: list, awards: list) -> Path | None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("  [WARN] openpyxl missing, no Excel attachment")
        return None
    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    sheets = [
        ("New tenders", new, ["Group", "Days left", "Deadline", "Title", "Solicitation", "Office",
                              "Country", "Set-aside", "NAICS", "PSC", "Link"]),
        ("Updates", updates, ["Group", "Change", "Days left", "Deadline", "Title", "Solicitation",
                              "Office", "Country", "Link"]),
        ("Awards", awards, ["Group", "Title", "Awardee", "Amount", "Office", "Country",
                            "Solicitation", "NAICS", "Link"]),
    ]
    first = True
    for name, rows, cols in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = name
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
        for x in rows:
            val = {"Group": GROUP_TITLE[group_of(x)], "Days left": days_left(x["deadline"]),
                   "Deadline": x["deadline"], "Title": x["title"], "Solicitation": x["sol"],
                   "Office": office(x), "Country": x["place"], "Set-aside": x["setaside"],
                   "NAICS": x["naics"], "PSC": x["psc"], "Link": x["link"],
                   "Change": x.get("change", ""), "Awardee": x["awardee"],
                   "Amount": x["award_amount"]}
            ws.append([val[c] for c in cols])
            ws.cell(ws.max_row, len(cols)).hyperlink = x["link"]
        widths = {"Title": 60, "Office": 35, "Change": 40, "Link": 40, "Country": 25, "Awardee": 30}
        for i, c in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 14)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    out = Path(f"SAM_tenders_{datetime.now(timezone.utc):%Y-%m-%d}.xlsx")
    wb.save(out)
    return out


# ---------------------------------------------------------------------------
# Email layout
# ---------------------------------------------------------------------------

C = {"ink": "#1f2933", "muted": "#6b7280", "line": "#e5e7eb", "head": "#1f3a5f",
     "bg": "#f6f7f9", "red": "#b42318", "amber": "#b54708", "green": "#067647"}
TD = f"padding:8px 10px;border-bottom:1px solid {C['line']};vertical-align:top;font-size:13px;color:{C['ink']}"
TH = f"padding:8px 10px;text-align:left;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#fff;background:{C['head']}"


def badge(d) -> str:
    if d is None:
        return f"<span style='color:{C['muted']}'>n/a</span>"
    col = C["red"] if d <= 7 else C["amber"] if d <= 14 else C["green"]
    return (f"<span style='display:inline-block;min-width:34px;text-align:center;padding:2px 6px;"
            f"border-radius:10px;background:{col};color:#fff;font-weight:bold;font-size:12px'>{d}d</span>")


def link_title(x: dict) -> str:
    sol = f"<div style='color:{C['muted']};font-size:11px'>{escape(x['sol'])}</div>" if x["sol"] else ""
    return f"<a href='{escape(x['link'])}' style='color:{C['head']};font-weight:bold;text-decoration:none'>{escape(x['title'])}</a>{sol}"


def table(cols: list[str], rows: list[list[str]]) -> str:
    h = "".join(f"<th style='{TH}'>{c}</th>" for c in cols)
    b = "".join("<tr>" + "".join(f"<td style='{TD}'>{v}</td>" for v in r) + "</tr>" for r in rows)
    return f"<table cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;margin:6px 0 18px'><tr>{h}</tr>{b}</table>"


def tile(n: int, label: str, col: str) -> str:
    return (f"<td style='padding:12px;background:#fff;border:1px solid {C['line']};border-radius:8px;text-align:center;width:25%'>"
            f"<div style='font-size:26px;font-weight:bold;color:{col}'>{n}</div>"
            f"<div style='font-size:12px;color:{C['muted']}'>{label}</div></td>")


def h2(t: str) -> str:
    return f"<h2 style='font-size:16px;color:{C['head']};margin:22px 0 4px;border-bottom:2px solid {C['head']};padding-bottom:4px'>{t}</h2>"


def build_digest(new: list, updates: list, awards: list, show_cap: int, has_xlsx: bool) -> tuple[str, str, str]:
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    core = lambda items: [x for x in items if group_of(x) != "dla"]
    closing = sum(1 for x in core(new + updates) if not is_award(x) and (days_left(x["deadline"]) if days_left(x["deadline"]) is not None else 99) <= 7)
    nc, uc, ac = len([x for x in new if group_of(x) != "dla"]), len([x for x in updates if group_of(x) != "dla"]), len(awards)
    subject = f"SAM.gov: {nc} new, {uc} updates" + (f", {ac} of your bids awarded" if ac else "") + f" ({today})"

    def by_group(items, g):
        return [x for x in items if group_of(x) == g]

    parts = [f"<div style='background:{C['bg']};padding:18px;font-family:Arial,Helvetica,sans-serif;color:{C['ink']}'>"
             f"<div style='max-width:900px;margin:0 auto'>"
             f"<div style='font-size:20px;font-weight:bold;color:{C['head']}'>SAM.gov tender digest</div>"
             f"<div style='color:{C['muted']};font-size:13px;margin-bottom:12px'>{today}. Only tenders you have not seen before, plus changes to ones you have. Counts exclude DLA small buys.</div>"
             f"<table cellspacing='8' style='width:100%'><tr>"
             + tile(len(core(new)), "New tenders", C["head"]) + tile(len(core(updates)), "Updates", C["amber"])
             + tile(closing, "Closing within 7 days", C["red"]) + tile(len(awards), "Your bids awarded", C["green"])
             + "</tr></table>"]
    plain = [f"SAM.gov tender digest, {today}",
             f"New: {len(new)} | Updates: {len(updates)} | Closing in 7 days: {closing} | Awards: {len(awards)}", ""]

    # NEW TENDERS by group (state first, DLA as a count only)
    for g in ("state", "other"):
        items = sorted(by_group(new, g), key=lambda x: x["deadline"] or "9")
        if not items:
            continue
        shown = items[:show_cap]
        parts.append(h2(f"New tenders: {GROUP_TITLE[g]} ({len(items)})"))
        parts.append(table(["Due", "Tender", "Office", "Country", "Set-aside"],
                           [[badge(days_left(x["deadline"])), link_title(x), escape(office(x)),
                             escape(x["place"]), escape(x["setaside"] or "-")] for x in shown]))
        if len(items) > show_cap:
            parts.append(f"<div style='color:{C['muted']};font-size:12px'>+{len(items) - show_cap} more in the Excel file.</div>")
        plain.append(f"NEW TENDERS: {GROUP_TITLE[g]} ({len(items)})")
        plain += [f"- [{days_left(x['deadline'])}d] {x['title']} | {office(x)} | {x['place']} | {x['link']}" for x in shown]
        plain.append("")

    dla_new = by_group(new, "dla")
    if dla_new:
        parts.append(h2(f"DLA small buys ({len(dla_new)})"))
        parts.append(f"<div style='font-size:13px'>{len(dla_new)} automated DLA part buys (bid through DIBBS). Full list in the Excel file, sheet New tenders, filter Group.</div>")
        plain += [f"DLA small buys: {len(dla_new)} (see Excel)", ""]

    if not new:
        parts.append(h2("New tenders"))
        parts.append("<div style='font-size:13px'>No new tenders today.</div>")

    # UPDATES (non-DLA in body)
    upd = sorted([x for x in updates if group_of(x) != "dla"], key=lambda x: (group_of(x) != "state", x["deadline"] or "9"))
    parts.append(h2(f"Updates to tenders already reported ({len(updates)})"))
    if upd:
        parts.append(table(["What changed", "Tender", "Office", "Due"],
                           [[f"<b>{escape(x['change'])}</b>", link_title(x), escape(office(x)),
                             badge(days_left(x["deadline"])) if not is_award(x) else "-"] for x in upd[:show_cap]]))
        plain.append(f"UPDATES ({len(updates)})")
        plain += [f"- {x['change']}: {x['title']} | {x['link']}" for x in upd[:show_cap]]
        plain.append("")
    dla_upd = len(updates) - len(upd)
    if dla_upd or not upd:
        parts.append(f"<div style='font-size:13px'>{'No updates.' if not updates else f'{dla_upd} DLA updates in the Excel file.'}</div>")

    # AWARDS (price intel, non-DLA)
    aw = sorted(awards, key=lambda x: group_of(x) != "state")
    if aw:
        parts.append(h2(f"Awards on tenders you bid ({len(awards)})"))
        parts.append(table(["Tender", "Winner", "Amount", "Office", "Country"],
                           [[link_title(x), escape(x["awardee"] or "n/a"),
                             escape(money(x["award_amount"])),
                             escape(office(x)), escape(x["place"])] for x in aw[:show_cap]]))
        plain.append(f"AWARDS ({len(awards)})")
        plain += [f"- {x['title']} | {x['awardee']} | {x['award_amount']} | {x['link']}" for x in aw[:show_cap]]

    foot = "Full list attached as Excel. " if has_xlsx else ""
    parts.append(f"<div style='color:{C['muted']};font-size:11px;margin-top:20px'>{foot}Filters: sam_config.json in Mukatrade/Screper. Bummer the scanner.</div></div></div>")
    return subject, "".join(parts), "\n".join(plain)


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

    kept, award_items, dropped_cfg, dropped_excl, dropped_nomatch = [], [], 0, 0, 0
    for n in raw:
        if "award" in (n.get("type") or "").lower():
            award_items.append(simplify(n))   # awards: only our own bids matter, no other filter
            continue
        if not passes_config(n):
            dropped_cfg += 1
            continue
        x = simplify(n)
        hay = f"{x['title']} {x['agency']} {x['naics']} {x['psc']}".lower()
        if word_hit(filters["exclude"], x["title"].lower()):
            dropped_excl += 1
            continue
        if filters["include"] and not word_hit(filters["include"], hay):
            dropped_nomatch += 1
            continue
        kept.append(x)

    state = load_state()
    new, updates, _ = classify(kept, state)
    if CFG["awards_only_for_my_bids"]:
        awards = my_bid_awards(award_items, state)
    else:
        awards = [x for x in award_items if passes_config({"fullParentPathName": x["agency"].replace(" / ", "."), "classificationCode": x["psc"], "type": x["type"]})]
    STATE_PATH.write_text(json.dumps(state, indent=0), encoding="utf-8")
    print(f"  fetched {len(raw)} | kept {len(kept)} | NEW {len(new)} | UPDATES {len(updates)} | "
          f"AWARDS {len(awards)} | off-config {dropped_cfg} | excluded {dropped_excl}")

    runs = []
    if RUNS_PATH.exists():
        try:
            runs = json.loads(RUNS_PATH.read_text(encoding="utf-8"))
        except Exception:
            runs = []
    runs.insert(0, {"ran_at": datetime.now(timezone.utc).isoformat(), "fetched": len(raw),
                    "tenders": len(new), "updates": len(updates), "awards": len(awards),
                    "filter_stats": {"off_config": dropped_cfg, "excluded_keyword": dropped_excl,
                                     "no_include_keyword": dropped_nomatch},
                    "results": [x for x in new + updates + awards if group_of(x) != "dla"][:200]})
    RUNS_PATH.write_text(json.dumps(runs[:10], indent=2), encoding="utf-8")

    if not (new or updates or awards):
        print("Nothing new on SAM.gov, no email.")
        return
    xlsx = build_excel(new, updates, awards)
    subject, html, plain = build_digest(new, updates, awards, MAX_ITEMS, bool(xlsx))
    if os.environ.get("SAM_DRY_RUN"):
        Path("sam_preview.html").write_text(html, encoding="utf-8")
        print(f"DRY RUN: {subject}")
        return
    print(f"Sending: {subject}")
    send_email(subject, html, plain, xlsx)


if __name__ == "__main__":
    main()
