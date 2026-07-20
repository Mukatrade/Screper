#!/usr/bin/env python3
"""Muka Trade - GitHub-Actions sender that reads its jobs from a Google DRIVE
folder (the "Muka Outbox"), not from the repo.

Why this exists: the Cowork cloud session can drop a job file into a Drive
folder (through the Drive connector) but it CANNOT push to this GitHub repo.
GitHub runners have open internet AND can read Drive with the same OAuth token,
so this script closes the loop: cloud -> Drive Outbox -> GitHub sends.

Flow every run:
  1. List job JSON files in the Drive Outbox folder.
  2. Skip any whose Drive file id is already in sent_jobs.txt (dedupe).
  3. For each new job: download it, download its Drive attachments, and send
     one email per recipient from the job's from_alias (Gmail API).
  4. Append the job's Drive file id to sent_jobs.txt so it never sends twice.
The workflow commits sent_jobs.txt back to the repo after each run.

A "job" is ONE JSON file in the Outbox folder. Shape:
  {
    "job_id": "389-26-rfq",              # human label (optional)
    "type": "supplier_rfq",              # for your records (optional)
    "from_alias": "quotes@mukatrade.com",# From address; must be a verified
                                         #   send-as alias, else Gmail uses info@
    "subject": "Price request - ... - RFQ 389-26",
    "body_html": "<p>Dear {{supplier_name}}, ...</p>",
    "attachments": [                     # optional; each read from Drive by id
      {"drive_id": "1AbC...", "filename": "tender-389-26.pdf",
       "mime": "application/pdf"}
    ],
    "recipients": [                      # one email each; {{supplier_name}} ->
      {"name": "National Pump", "email": "sales@nationalpump.com"}
    ],
    "thread": {"threadId": "...", "in_reply_to": "<msgid>",
               "references": "<msgid>"}  # optional, to reply in a thread
  }

Token: ~/.muka_trade/gmail_token.json (written by the workflow from the
GMAIL_TOKEN_JSON secret). It MUST carry all three scopes:
  https://mail.google.com/                          (send)
  https://www.googleapis.com/auth/drive.readonly    (read the Outbox + PDFs)
  https://www.googleapis.com/auth/spreadsheets       (brain, used elsewhere)
"""
import argparse
import base64
import io
import json
import os
import sys
import warnings
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

warnings.filterwarnings("ignore")

TOKEN_PATH = os.path.expanduser("~/.muka_trade/gmail_token.json")
SEND_SCOPE = "https://mail.google.com/"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
# Default "Muka Outbox" folder id (inside "Muka Trade System"). Override with
# --folder or the MUKA_OUTBOX_FOLDER_ID env var.
DEFAULT_FOLDER_ID = "1Ns2i0f2kCdj7Kv_W69D-mUrsTOCJX0mW"
DASHES = ["—", "–", "‒", "―", "−"]


def sanitize(text):
    for d in DASHES:
        text = text.replace(d, "-")
    return text


def load_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not os.path.exists(TOKEN_PATH):
        sys.exit(f"ERROR: token missing at {TOKEN_PATH} (set the GMAIL_TOKEN_JSON secret).")
    have = json.load(open(TOKEN_PATH)).get("scopes") or []
    missing = [s for s in (SEND_SCOPE, DRIVE_SCOPE) if s not in have]
    if missing:
        sys.exit(
            "ERROR: the Gmail token is missing required scope(s): "
            + ", ".join(missing)
            + f"\n  Token scopes present: {have}"
            + "\n  Re-mint the token with all of: "
            + f"{SEND_SCOPE} , {DRIVE_SCOPE} , {SHEETS_SCOPE}"
            + "\n  then update the GMAIL_TOKEN_JSON secret."
        )
    scopes = [SEND_SCOPE, DRIVE_SCOPE]
    if SHEETS_SCOPE in have:
        scopes.append(SHEETS_SCOPE)
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        open(TOKEN_PATH, "w").write(creds.to_json())
    return creds


def drive_download_bytes(drive, file_id):
    """Download the raw bytes of a Drive file (works for uploaded, non-Google
    files such as our application/json jobs and application/pdf attachments)."""
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def list_jobs(drive, folder_id):
    """Return job files [{id, name}] in the Outbox folder, oldest first."""
    q = (
        f"'{folder_id}' in parents and trashed = false and "
        "(mimeType = 'application/json' or name contains '.json')"
    )
    jobs, page = [], None
    while True:
        resp = drive.files().list(
            q=q, orderBy="createdTime",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=100, supportsAllDrives=True, includeItemsFromAllDrives=True,
            pageToken=page,
        ).execute()
        for f in resp.get("files", []):
            if f["name"].lower().endswith(".json") or f.get("mimeType") == "application/json":
                jobs.append(f)
        page = resp.get("nextPageToken")
        if not page:
            break
    return jobs


def build_raw(from_alias, to_email, name, subject, html, attachments, thread):
    msg = MIMEMultipart("mixed")
    msg["From"] = from_alias
    msg["To"] = to_email
    msg["Subject"] = subject
    if thread:
        if thread.get("in_reply_to"):
            msg["In-Reply-To"] = thread["in_reply_to"]
        if thread.get("references"):
            msg["References"] = thread["references"]
    msg.attach(MIMEText(html.replace("{{supplier_name}}", name or ""), "html"))
    for att in attachments:
        data, fname, mime = att
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
        msg.attach(part)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def process_job(gmail, drive, job_file):
    raw = drive_download_bytes(drive, job_file["id"])
    try:
        job = json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"  SKIP {job_file['name']}: not valid JSON ({e})")
        return None  # None => do not mark sent, leave for a human to fix

    from_alias = job.get("from_alias") or "info@mukatrade.com"
    subject = sanitize(job.get("subject", ""))
    body_html = sanitize(job.get("body_html", ""))
    recipients = job.get("recipients") or []
    thread = job.get("thread") or {}

    if not subject or not body_html or not recipients:
        print(f"  SKIP {job_file['name']}: missing subject, body_html, or recipients")
        return None

    # Download attachments once (shared across recipients).
    attachments = []
    for att in job.get("attachments") or []:
        did = att.get("drive_id")
        if not did:
            continue
        try:
            data = drive_download_bytes(drive, did)
        except Exception as e:
            print(f"  SKIP {job_file['name']}: attachment {did} unreadable ({e})")
            return None
        attachments.append((data, att.get("filename", "attachment"),
                            att.get("mime", "application/octet-stream")))

    sent = failed = 0
    for r in recipients:
        email = (r.get("email") or "").strip()
        if not email:
            continue
        body = {"raw": build_raw(from_alias, email, (r.get("name") or "").strip(),
                                 subject, body_html, attachments, thread)}
        if thread.get("threadId"):
            body["threadId"] = thread["threadId"]
        try:
            gmail.users().messages().send(userId="me", body=body).execute()
            sent += 1
            print(f"  sent -> {email}")
        except Exception as e:
            failed += 1
            print(f"  FAILED -> {email}: {e}")

    print(f"  {job_file['name']}: {sent} sent, {failed} failed")
    # Mark the job done as long as at least one recipient was attempted and the
    # job was well-formed. Per-recipient failures are logged; they do not force a
    # full resend of the whole batch (which would double-send the ones that
    # succeeded). If EVERYTHING failed, leave it for retry.
    return sent > 0 or (sent == 0 and failed == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=os.environ.get("MUKA_OUTBOX_FOLDER_ID", DEFAULT_FOLDER_ID))
    ap.add_argument("--sent-list", default="sent_jobs.txt")
    a = ap.parse_args()

    from googleapiclient.discovery import build

    creds = load_creds()
    gmail = build("gmail", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    already = set()
    if os.path.exists(a.sent_list):
        already = {ln.split("\t")[0].strip() for ln in open(a.sent_list) if ln.strip()}

    jobs = list_jobs(drive, a.folder)
    pending = [j for j in jobs if j["id"] not in already]
    print(f"Outbox {a.folder}: {len(jobs)} job file(s), {len(pending)} new.")
    if not pending:
        return

    newly_done = []
    for jf in pending:
        print(f"Processing {jf['name']} ({jf['id']})")
        result = process_job(gmail, drive, jf)
        if result:
            newly_done.append((jf["id"], jf["name"]))
        else:
            print("  left un-marked (will retry next run or needs a fix)")

    if newly_done:
        with open(a.sent_list, "a") as f:
            for fid, name in newly_done:
                f.write(f"{fid}\t{name}\n")
        print(f"Marked {len(newly_done)} job(s) done in {a.sent_list}.")


if __name__ == "__main__":
    main()
