#!/usr/bin/env python3
"""
State-Aware Web Scraper with Delta Notifications
-------------------------------------------------
Reads monitoring targets from a public Google Sheet CSV, scrapes each URL,
saves cleaned Markdown snapshots, diffs against the previous Git commit,
sends diffs to Claude for analysis, and emails actionable findings via Gmail.

Required environment variables:
  ANTHROPIC_API_KEY      - Anthropic API key
  GMAIL_USER             - Gmail address used to send reports
  GMAIL_APP_PASSWORD     - Gmail App Password (not your account password)
  RECIPIENT_EMAIL        - Address(es) to receive reports (comma-separated)
  GOOGLE_SHEET_CSV_URL   - Public CSV export URL of the monitoring sheet
"""

import csv
import io
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER          = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL     = os.environ["RECIPIENT_EMAIL"]          # comma-separated
GOOGLE_SHEET_CSV_URL = os.environ["GOOGLE_SHEET_CSV_URL"]

SITES_DIR       = Path("sites")
CLAUDE_MODEL    = "claude-opus-4-8"
REQUEST_TIMEOUT = 30          # seconds per HTTP request
REQUEST_DELAY   = 2           # seconds between scrapes (be polite)
MAX_DIFF_CHARS  = 12_000      # truncate very large diffs before sending to Claude

# HTML tags whose entire subtree is noise
NOISE_TAGS = [
    "script", "style", "noscript", "header", "footer",
    "nav", "aside", "iframe", "form", "button", "meta", "link",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TenderMonitorBot/1.0; "
        "+https://github.com/your-org/tender-monitor)"
    )
}

# ---------------------------------------------------------------------------
# Helpers: scraping
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> Optional[str]:
    """Download a page and return its HTML, or None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"  [WARN] Could not fetch {url}: {exc}", file=sys.stderr)
        return None


def html_to_markdown(html: str, url: str) -> str:
    """
    Strip noise from HTML and return clean text formatted as Markdown.
    Preserves headings and list structure where possible.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove noise subtrees
    for tag in NOISE_TAGS:
        for el in soup.find_all(tag):
            el.decompose()

    # Try to find the main content area; fall back to <body>
    main = (
        soup.find("main")
        or soup.find(id=re.compile(r"(content|main|body)", re.I))
        or soup.find(class_=re.compile(r"(content|main|body)", re.I))
        or soup.body
        or soup
    )

    lines: list[str] = []
    for el in main.descendants:
        if not hasattr(el, "name"):          # NavigableString
            text = str(el).strip()
            if text:
                lines.append(text)
        elif el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(el.name[1])
            heading_text = el.get_text(" ", strip=True)
            if heading_text:
                lines.append(f"\n{'#' * level} {heading_text}\n")
        elif el.name in ("li",):
            item = el.get_text(" ", strip=True)
            if item:
                lines.append(f"- {item}")
        elif el.name in ("tr",):
            cells = [td.get_text(" ", strip=True) for td in el.find_all(["td", "th"])]
            if any(cells):
                lines.append(" | ".join(cells))

    # Collapse excessive blank lines
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    header = (
        f"# Snapshot\n\n"
        f"**Source:** {url}  \n"
        f"**Captured:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"---\n\n"
    )
    return header + text.strip()


def safe_filename(name: str) -> str:
    """Convert a site name to a safe filename stem."""
    return re.sub(r"[^\w\-]", "_", name.strip().lower())

# ---------------------------------------------------------------------------
# Helpers: CSV / sheet reading
# ---------------------------------------------------------------------------

def load_targets(csv_url: str) -> list[dict]:
    """
    Download the published Google Sheet CSV and return a list of
    {'name': str, 'link': str} dicts.
    """
    print(f"Loading targets from: {csv_url}")
    resp = requests.get(csv_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    targets = []
    for row in reader:
        name = row.get("name", "").strip()
        link = row.get("link", "").strip()
        if name and link:
            targets.append({"name": name, "link": link})
    print(f"  → {len(targets)} target(s) loaded.")
    return targets

# ---------------------------------------------------------------------------
# Helpers: Git diff
# ---------------------------------------------------------------------------

def git_diff_for_file(filepath: Path) -> str:
    """
    Return the unstaged diff for a specific file against HEAD.
    Returns an empty string if there is no change or the file is new.
    """
    result = subprocess.run(
        ["git", "diff", "HEAD", "--", str(filepath)],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()

# ---------------------------------------------------------------------------
# Helpers: Claude analysis
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM_PROMPT = """You are a procurement intelligence assistant.
You receive a Git diff (unified diff format) from a website that publishes tender or contract notices.
Your job is to determine whether the change is actionable:

1. NEW TENDER — a brand-new tender / contract opportunity has appeared.
2. STATUS UPDATE — an existing tender has changed status (e.g., awarded, cancelled, extended, deadline changed).
3. CONTENT EDIT — minor edits inside an existing tender block (clarifications, amendments).
4. NOISE — navigation changes, date stamps, cookie banners, or other non-tender content.

Reply with a JSON object (no markdown fences) with exactly these keys:
{
  "actionable": true | false,
  "category": "NEW_TENDER" | "STATUS_UPDATE" | "CONTENT_EDIT" | "NOISE",
  "summary": "<one or two sentence plain-English description of what changed>",
  "details": "<any specific tender names, reference numbers, deadlines, values you can extract>"
}
"""


def analyse_diff(client: Anthropic, site_name: str, diff_text: str) -> dict:
    """Send a diff to Claude and return parsed analysis dict."""
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... [truncated]"

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=ANALYSIS_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Site: {site_name}\n\n"
                    f"```diff\n{diff_text}\n```"
                ),
            }
        ],
    )

    raw = message.content[0].text.strip()

    # Parse JSON — be lenient if Claude wraps it in fences anyway
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    import json
    try:
        return json.loads(raw)
    except Exception:
        # Fallback: treat as unparseable but non-noise
        return {
            "actionable": True,
            "category": "CONTENT_EDIT",
            "summary": "Claude returned an unparseable response; manual review recommended.",
            "details": raw[:500],
        }

# ---------------------------------------------------------------------------
# Helpers: Email
# ---------------------------------------------------------------------------

def send_email(subject: str, body_html: str, body_text: str) -> None:
    recipients = [r.strip() for r in RECIPIENT_EMAIL.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(recipients)

    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())

    print(f"  → Email sent to: {', '.join(recipients)}")


def build_report(findings: list[dict]) -> tuple[str, str, str]:
    """
    Build (subject, html, plaintext) from a list of finding dicts:
    {'site_name', 'link', 'category', 'summary', 'details'}
    """
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    count    = len(findings)
    subject  = f"[Tender Monitor] {count} change(s) detected — {date_str}"

    # ---- Plain text ----
    lines = [f"Tender Monitor Daily Report — {date_str}", "=" * 50, ""]
    for i, f in enumerate(findings, 1):
        lines += [
            f"{i}. [{f['category']}] {f['site_name']}",
            f"   URL     : {f['link']}",
            f"   Summary : {f['summary']}",
            f"   Details : {f['details']}",
            "",
        ]
    plain = "\n".join(lines)

    # ---- HTML ----
    rows = ""
    for f in findings:
        category_color = {
            "NEW_TENDER":    "#d4edda",
            "STATUS_UPDATE": "#fff3cd",
            "CONTENT_EDIT":  "#d1ecf1",
            "NOISE":         "#f8f9fa",
        }.get(f["category"], "#ffffff")

        rows += f"""
        <tr style="background:{category_color}">
          <td style="padding:8px;border:1px solid #dee2e6;font-weight:bold">{f['category']}</td>
          <td style="padding:8px;border:1px solid #dee2e6">
            <a href="{f['link']}">{f['site_name']}</a>
          </td>
          <td style="padding:8px;border:1px solid #dee2e6">{f['summary']}</td>
          <td style="padding:8px;border:1px solid #dee2e6;font-size:0.9em;color:#555">{f['details']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;margin:20px">
  <h2 style="color:#343a40">Tender Monitor Daily Report</h2>
  <p style="color:#6c757d">{date_str} &mdash; {count} actionable change(s) detected</p>
  <table style="border-collapse:collapse;width:100%;font-size:0.95em">
    <thead>
      <tr style="background:#343a40;color:#fff">
        <th style="padding:10px;border:1px solid #dee2e6;text-align:left">Category</th>
        <th style="padding:10px;border:1px solid #dee2e6;text-align:left">Site</th>
        <th style="padding:10px;border:1px solid #dee2e6;text-align:left">Summary</th>
        <th style="padding:10px;border:1px solid #dee2e6;text-align:left">Details</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="margin-top:20px;color:#adb5bd;font-size:0.8em">
    Generated by Tender Monitor &mdash; powered by Claude {CLAUDE_MODEL}
  </p>
</body>
</html>"""

    return subject, html, plain

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    SITES_DIR.mkdir(exist_ok=True)

    # 1. Load monitoring targets
    targets = load_targets(GOOGLE_SHEET_CSV_URL)
    if not targets:
        print("No targets found. Exiting.")
        sys.exit(0)

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # 2. Scrape each target and write snapshot
    print("\n--- Scraping ---")
    for target in targets:
        name, url = target["name"], target["link"]
        print(f"Scraping: {name} ({url})")
        html = fetch_page(url)
        if html is None:
            continue
        md = html_to_markdown(html, url)
        outfile = SITES_DIR / f"{safe_filename(name)}.md"
        outfile.write_text(md, encoding="utf-8")
        print(f"  → Saved {outfile}")
        time.sleep(REQUEST_DELAY)

    # 3. Diff against HEAD and analyse
    print("\n--- Diffing & Analysing ---")
    findings: list[dict] = []

    for target in targets:
        name = target["name"]
        filepath = SITES_DIR / f"{safe_filename(name)}.md"
        if not filepath.exists():
            continue

        diff = git_diff_for_file(filepath)
        if not diff:
            print(f"  No change: {name}")
            continue

        print(f"  Change detected: {name} — sending to Claude...")
        analysis = analyse_diff(client, name, diff)
        print(f"    category={analysis.get('category')}  actionable={analysis.get('actionable')}")

        if analysis.get("actionable"):
            findings.append({
                "site_name": name,
                "link":      target["link"],
                "category":  analysis.get("category", "UNKNOWN"),
                "summary":   analysis.get("summary", ""),
                "details":   analysis.get("details", ""),
            })

    # 4. Email report if there is anything worth reporting
    print("\n--- Reporting ---")
    if findings:
        subject, html, plain = build_report(findings)
        print(f"Sending report: {subject}")
        send_email(subject, html, plain)
    else:
        print("No actionable changes found. No email sent.")

    print("\nDone.")


if __name__ == "__main__":
    main()
