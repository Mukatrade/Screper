#!/usr/bin/env python3
"""GitHub-Actions RFQ sender for Muka Trade.

Processes every job folder under --outbox, sends each email from
quotes@mukatrade.com with the tender PDF attached (Gmail API), updates the
job's suppliers.csv (Status=sent/failed, SentDate), and moves the finished job
folder to --sent. Safe to re-run: rows already Status=sent are skipped.

This is the same send logic as the Cowork email-sender skill's send_rfq.py,
adapted to read jobs from the repo and to run where the internet is open.

Job folder layout (outbox/<name>/):
  job.json       {"subject": str, "body_file": str, "pdf_file": str|null, "csv_file": str}
  body.html      email body, may contain {{supplier_name}}
  tender.pdf     attachment (optional)
  suppliers.csv  columns Name,Email[,Status,SentDate]

Requires the Gmail token at ~/.muka_trade/gmail_token.json with the
https://mail.google.com/ scope (written by the workflow from the GMAIL_TOKEN_JSON
secret). quotes@mukatrade.com must be a verified send-as alias on that account.
"""
import argparse, base64, csv, datetime, json, os, shutil, sys, warnings
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

warnings.filterwarnings("ignore")

FROM_ALIAS = "quotes@mukatrade.com"
TOKEN_PATH = os.path.expanduser("~/.muka_trade/gmail_token.json")
REQUIRED_SCOPE = "https://mail.google.com/"
DASHES = ["—", "–", "‒", "―", "−"]


def sanitize(text):
    for d in DASHES:
        text = text.replace(d, "-")
    return text


def get_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not os.path.exists(TOKEN_PATH):
        sys.exit(f"ERROR: token missing at {TOKEN_PATH} (set the GMAIL_TOKEN_JSON secret).")
    scopes = json.load(open(TOKEN_PATH)).get("scopes") or []
    if REQUIRED_SCOPE not in scopes:
        sys.exit(f"ERROR: token scopes {scopes} lack {REQUIRED_SCOPE}.")
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, [REQUIRED_SCOPE])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        open(TOKEN_PATH, "w").write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def build_raw(to_email, name, subject, html, pdf_path):
    msg = MIMEMultipart("mixed")
    msg["From"] = FROM_ALIAS
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html.replace("{{supplier_name}}", name), "html"))
    if pdf_path:
        with open(pdf_path, "rb") as fh:
            part = MIMEBase("application", "pdf")
            part.set_payload(fh.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(pdf_path)}"')
        msg.attach(part)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def process_job(job_dir, service):
    cfg = json.load(open(os.path.join(job_dir, "job.json")))
    subject = sanitize(cfg["subject"])
    html = sanitize(open(os.path.join(job_dir, cfg["body_file"])).read())
    pdf_path = os.path.join(job_dir, cfg["pdf_file"]) if cfg.get("pdf_file") else None
    if pdf_path and not os.path.exists(pdf_path):
        print(f"  SKIP job {job_dir}: pdf {pdf_path} missing"); return False
    csv_path = os.path.join(job_dir, cfg["csv_file"])
    rows = list(csv.DictReader(open(csv_path, newline="")))
    today = datetime.date.today().isoformat()
    sent = failed = 0
    for r in rows:
        email = (r.get("Email") or "").strip()
        if (r.get("Status") or "").strip().lower() == "sent" or not email:
            continue
        try:
            service.users().messages().send(
                userId="me", body={"raw": build_raw(email, (r.get("Name") or "").strip(), subject, html, pdf_path)}
            ).execute()
            r["Status"], r["SentDate"] = "sent", today; sent += 1
            print(f"  sent -> {email}")
        except Exception as e:
            r["Status"] = "failed"; failed += 1
            print(f"  FAILED -> {email}: {e}")
    # write CSV back
    fn = list(rows[0].keys()) if rows else []
    for c in ("Status", "SentDate"):
        if c not in fn:
            fn.append(c)
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fn); w.writeheader(); w.writerows(rows)
    print(f"  job {os.path.basename(job_dir)}: {sent} sent, {failed} failed")
    return failed == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outbox", default="outbox")
    ap.add_argument("--sent", default="sent")
    a = ap.parse_args()
    if not os.path.isdir(a.outbox):
        print("no outbox/ - nothing to do"); return
    jobs = [os.path.join(a.outbox, d) for d in sorted(os.listdir(a.outbox))
            if os.path.isfile(os.path.join(a.outbox, d, "job.json"))]
    if not jobs:
        print("no pending jobs"); return
    service = get_service()
    os.makedirs(a.sent, exist_ok=True)
    for job in jobs:
        print(f"Processing {job}")
        ok = process_job(job, service)
        if ok:
            dest = os.path.join(a.sent, os.path.basename(job))
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(job, dest)
            print(f"  moved to {dest}")
        else:
            print(f"  left in outbox for retry (had failures)")


if __name__ == "__main__":
    main()
