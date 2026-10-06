"""Sarmad Monitor: GitHub Actions port of the PythonAnywhere web_scraping_V2.py.

Same logic as the original: read (title, url) rows from the Google Sheet,
extract keyword-matching text from each page, compare with the previous
snapshot, email one alert per changed site, and always email a run summary.

Differences from the PythonAnywhere version (on purpose):
- Sheet is read from the GOOGLE_SHEET_CSV_URL secret (no token.json needed).
- Credentials come from GitHub secrets, never from the source file.
- Pages are fetched in parallel (8 at a time) so a slow site cannot stall the run.
- Fetch failures are listed in the summary email instead of one email per site.
- Snapshots are stored in sarmad_state/ and committed back to the repo.
"""

import csv
import hashlib
import io
import json
import os
import re
import smtplib
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

KEYWORDS = [
    "request", "quotations", "quote", "proposal", "proposals", "procurement",
    "bidding", "bid", "bids", "bidders", "rfq",
    "tender", "invitation", "solicitation", "solicitations", "responses",
    "purchase", "purchases", "purchasing",
    "services", "service",
    "supplies", "supply",
    "goods",
    "equipment", "parts",
    "repairs", "repairing", "replacement",
    "contractor", "vendor", "supplier", "suppliers",
    "installation", "installing",
    "rental", "lodging",
    "packaging",
    "opportunity", "opportunities",
    "contract", "quotation",
    "requirement",
    "pr",
    "please read the attached",
]
KW_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36"
    )
}

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sarmad_state")


# ---------- email ----------

def send_email(subject, plain_body, html_body):
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    message = MIMEMultipart("alternative")
    message["From"] = user
    message["To"] = recipient
    message["Subject"] = subject
    message.attach(MIMEText(plain_body, "plain"))
    message.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, recipient, message.as_string())
        print(f"Email sent to {recipient}: {subject}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def send_change_alert(url, title):
    plain = f"""Hey, Hope you are doing well.

We've detected changes on the website.
Website Name: {title}
It's recommended to visit the website to review the changes: {url}

Thanks,
Python Automation
"""
    html = f"""<html><body>
<p>Hey, Hope you are doing well.</p>
<p>We've detected changes on the website.</p>
<p>Website Name: <b>{title}</b></p>
<p>It's recommended to visit the website to review the changes:
<a href="{url}">{url}</a></p>
<p>Thanks,<br>Python Automation</p>
</body></html>"""
    send_email(f"Changes Detected on: {title}", plain, html)


def send_scan_summary(changed, checked, failed):
    stamp = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d %H:%M")

    if changed:
        subject = f"Sarmad Monitor: {len(changed)} change(s) found - {stamp}"
        changes_plain = "\n".join(f"- {t}: {u}" for t, u in changed)
        changes_html = "".join(f'<li><b>{t}</b> - <a href="{u}">{u}</a></li>' for t, u in changed)
        summary_plain = f"Changes were found on {len(changed)} website(s):\n{changes_plain}"
        summary_html = f"<p>Changes were found on <b>{len(changed)}</b> website(s):</p><ul>{changes_html}</ul>"
    else:
        subject = f"Sarmad Monitor: scan complete, NO changes - {stamp}"
        summary_plain = "The scan finished and no changes were found on any website."
        summary_html = "<p>The scan finished and <b>no changes</b> were found on any website.</p>"

    failed_plain = ""
    failed_html = ""
    if failed:
        failed_plain = "\n\nFetch failures:\n" + "\n".join(f"- {t}: {u} ({e})" for t, u, e in failed)
        failed_html = "<p>Fetch failures:</p><ul>" + "".join(
            f'<li><b>{t}</b> - <a href="{u}">{u}</a> ({e})</li>' for t, u, e in failed
        ) + "</ul>"

    plain = f"""Hey, Hope you are doing well.

{summary_plain}

Websites checked: {checked}
Fetch failures: {len(failed)}{failed_plain}

Thanks,
Python Automation
"""
    html = f"""<html><body>
<p>Hey, Hope you are doing well.</p>
{summary_html}
<p>Websites checked: <b>{checked}</b><br>
Fetch failures: <b>{len(failed)}</b></p>
{failed_html}
<p>Thanks,<br>Python Automation</p>
</body></html>"""
    send_email(subject, plain, html)


def send_script_error(error):
    plain = f"""Hey, Hope you are doing well.

There was an issue while running the script:
Error: {error}

Thanks,
Python Automation
"""
    html = f"""<html><body>
<p>Hey, Hope you are doing well.</p>
<p>There was an issue while running the script:</p>
<p>Error: <b>{error}</b></p>
<p>Thanks,<br>Python Automation</p>
</body></html>"""
    send_email("Error Detected", plain, html)


# ---------- extraction and snapshots ----------

def extract_information(url):
    """Return (data, error). Same extraction rules as the PythonAnywhere script."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return None, str(e)

    soup = BeautifulSoup(response.content, "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    title_tag = soup.find("title")
    data = {"title": [title_tag.text.strip().lower()] if title_tag else [""]}

    root = soup.find("main") or soup

    data["tables"] = []
    for table in root.find_all("table"):
        ths = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if any(KW_RE.search(h) for h in ths):
            rows = []
            for row in table.find_all("tr"):
                tds = [td.get_text(" ", strip=True).lower() for td in row.find_all(["td", "th"])]
                if tds:
                    rows.append(tds)
            if rows:
                data["tables"].append(rows)

    def matching(elements):
        out = []
        for el in elements:
            text = el.get_text(" ", strip=True).lower()
            if KW_RE.search(text):
                out.append(text)
        return out

    data["paragraphs"] = matching(root.find_all("p"))
    data["headings"] = []
    for level in range(1, 7):
        data["headings"].extend(matching(root.find_all(f"h{level}")))
    data["list_items"] = matching(root.find_all("li"))

    return data, None


def state_path(url):
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", url)
    if len(name) > 150:
        name = name[:150] + "_" + hashlib.sha1(url.encode()).hexdigest()[:10]
    return os.path.join(STATE_DIR, name + ".json")


def load_information(url):
    try:
        with open(state_path(url), "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_information(url, information):
    with open(state_path(url), "w") as f:
        json.dump(information, f, indent=4)


def has_changes(old_data, new_data):
    def content(d):
        return set(d.get("headings", []) + d.get("paragraphs", []) + d.get("list_items", []))

    def tables(d):
        return {json.dumps(t, sort_keys=True) for t in (d.get("tables") or [])}

    diff = (content(new_data) - content(old_data)) | (tables(new_data) - tables(old_data))
    return bool(diff)


# ---------- sheet ----------

def load_sites():
    sheet_url = os.environ["GOOGLE_SHEET_CSV_URL"]
    response = requests.get(sheet_url, timeout=60)
    response.raise_for_status()
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    sites = []
    for row in rows[1:]:  # row 1 is the header, like A2:C in the original
        if len(row) < 2:
            continue
        title, url = row[0].strip(), row[1].strip()
        if title and url:
            sites.append((title, url))
    return sites


# ---------- main ----------

def main():
    os.makedirs(STATE_DIR, exist_ok=True)

    try:
        sites = load_sites()
    except Exception as e:
        print(f"Error fetching data from spreadsheet: {e}")
        send_script_error(e)
        sys.exit(1)

    if not sites:
        print("No data found in the sheet.")
        send_script_error("No sites found in the Google Sheet (check GOOGLE_SHEET_CSV_URL).")
        sys.exit(1)

    print(f"Loaded {len(sites)} sites from the sheet.")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: extract_information(s[1]), sites))

    changed, failed, checked = [], [], 0

    for (title, url), (new_data, error) in zip(sites, results):
        if new_data is None:
            print(f"Error fetching data from {url}: {error}")
            failed.append((title, url, error))
            continue

        checked += 1
        old_data = load_information(url)
        if old_data is None:
            print(f"No previous information found for {title}")
        elif has_changes(old_data, new_data):
            print(f"Changes found on {title}")
            send_change_alert(url, title)
            changed.append((title, url))
        else:
            print(f"No Changes Detected on {title}")
        save_information(url, new_data)

    send_scan_summary(changed, checked, failed)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"Script error: {e}")
        send_script_error(e)
        sys.exit(1)
