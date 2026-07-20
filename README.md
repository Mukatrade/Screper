# Muka RFQ Auto-Send (GitHub Actions)

Automatic RFQ / quote sending that does NOT depend on the Cowork cloud session.
The Cowork cloud now blocks googleapis.com, so the in-session send script cannot
reach Gmail. GitHub Actions runners have open internet, so the send runs here
instead, from quotes@mukatrade.com with the tender PDF attached.

## One-time setup (about 5 minutes)

1. Copy these files into your repo (e.g. Mukatrade/Screper), keeping the paths:
   - `.github/workflows/muka-send-rfq.yml`
   - `scripts/gh_send_rfq.py`
   - the empty `outbox/` folder (keep the `.gitkeep`)

2. Add the Gmail token as a repository secret:
   - Open the token file `gmail_token.json` from your Google Drive folder
     "Muka Trade System" (the one with BOTH mail and spreadsheets scopes,
     file id 1KYzP1KPksjrLvMKS4ijG4LA3MKg4tWxl). Copy its entire JSON contents.
   - In GitHub: repo -> Settings -> Secrets and variables -> Actions ->
     New repository secret. Name it exactly `GMAIL_TOKEN_JSON`, paste the JSON,
     save.

3. Confirm quotes@mukatrade.com is a verified "send as" alias on that Gmail
   account (Gmail -> Settings -> Accounts -> Send mail as). The token account
   must be allowed to send as quotes@. This is already true for your normal
   sends.

That is it. The workflow now runs on three triggers: manually from the Actions
tab, automatically whenever a job is committed under `outbox/`, and every 15
minutes as a safety net.

## How to send a batch (a "job")

Create a folder under `outbox/` (any name, e.g. the RFQ number) containing:

    outbox/389-26/
      job.json
      body.html
      tender.pdf
      suppliers.csv

`job.json`:

    {
      "subject": "Price request - Electric fan motors - RFQ 389-26",
      "body_file": "body.html",
      "pdf_file": "tender.pdf",
      "csv_file": "suppliers.csv"
    }

- `body.html` is the email body. `{{supplier_name}}` is replaced per supplier.
- `suppliers.csv` needs at least `Name,Email` columns. A `Status` column is
  optional; rows already `sent` are skipped so re-runs are safe.
- For a text-only email with no attachment, set `"pdf_file": null` and omit the PDF.

Commit and push the folder. The Action sends every supplier, updates
`suppliers.csv` (Status=sent/failed, SentDate), and moves the finished job to
`sent/`. If any supplier failed, the job stays in `outbox/` so the next run
retries only the failures.

## How this connects to Cowork (later, optional)

Jerry prepares the batch in Cowork exactly as today. Instead of trying to send
from the blocked cloud, he (or you) drops the job folder into `outbox/` and
pushes. The Action does the actual send. Roy the dispatcher can be pointed at
this outbox as its "networked send" path. If you want, I can wire Jerry/Roy to
write the job folder automatically.

## Security note

The `GMAIL_TOKEN_JSON` secret grants send access to your account. Keep the repo
private. Rotate the token if it is ever exposed. GitHub never prints secrets in
logs.
