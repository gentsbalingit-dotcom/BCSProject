#!/usr/bin/env python3
"""
BCS Calendar HTML Agent
Reads new Toddle emails via Gmail, extracts events with Claude,
updates bcs_calendar.html in place. Runs hourly via GitHub Actions.
"""

import os, re, json, datetime, pathlib, textwrap, base64
import anthropic
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HTML_FILE     = pathlib.Path(__file__).parent.parent / "bcs_calendar.html"
LOOKBACK_HRS  = int(os.getenv("LOOKBACK_HOURS", "2"))

# ── Gmail ─────────────────────────────────────────────────────────────────────
def gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    return build("gmail", "v1", credentials=creds)

def fetch_emails():
    print(f"[1/3] Searching Gmail — newer_than:{LOOKBACK_HRS}h from:toddleapp.com")
    svc = gmail_service()
    res = svc.users().messages().list(
        userId="me", q=f"from:toddleapp.com newer_than:{LOOKBACK_HRS}h", maxResults=20
    ).execute()
    msgs = res.get("messages", [])
    if not msgs:
        print("  → No new emails.")
        return []

    emails = []
    for m in msgs:
        full = svc.users().messages().get(userId="me", messageId=m["id"], format="full").execute()
        hdrs = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        emails.append({
            "subject": hdrs.get("Subject", ""),
            "date":    hdrs.get("Date", ""),
            "sender":  hdrs.get("From", ""),
            "body":    _body(full["payload"]),
        })
        print(f"  → {hdrs.get('Subject','')[:65]}")
    print(f"  → {len(emails)} email(s) found.")
    return emails

def _body(payload):
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _body(part)
        if result:
            return result
    return ""

# ── Claude extraction ─────────────────────────────────────────────────────────
PROMPT = textwrap.dedent("""
You are a school calendar assistant for the Balingit family at Berlin Cosmopolitan School.
Children: Elias (Class 4C, teacher Mr Vevers) and Isagani.

Extract ALL calendar-relevant info from this Toddle email.
Return ONLY raw JSON, no markdown fences, no explanation.

{
  "events": [
    { "date": "YYYY-M-D", "type": "u|e|s|n|t", "label": "emoji + short label ≤28 chars", "tip": "one sentence detail" }
  ],
  "weekNote": {
    "from": "Sender · Role",
    "sub": "Subject summary · Child",
    "date": "D Mon YYYY",
    "tag": "Weekly Update|Action Required|Announcement|Opportunity|Class Update|Health Notice|Newsletter",
    "body": "2-3 sentence summary",
    "highlights": [{ "ic": "emoji", "t": "highlight text" }],
    "action": "parent action or null"
  },
  "urgent": { "show": true/false, "title": "...", "body": "...", "due": "..." }
}

Types: u=urgent, e=event, s=deadline/opportunity, n=nature-campus/water-sports, t=announcement
urgent.show=true ONLY if parent action needed within 7 days.
If no calendar content: events:[], weekNote:null, urgent:{show:false}

Subject: {subject}
Date: {date}
From: {sender}
Body:
{body}
""")

def extract(email):
    print(f"[2/3] Extracting: {email['subject'][:55]}")
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": PROMPT.format(
            subject=email["subject"],
            date=email["date"],
            sender=email["sender"],
            body=email["body"][:6000],
        )}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ⚠ JSON error: {e}")
        return None

# ── HTML injection ─────────────────────────────────────────────────────────────
def esc(s):
    return (s or "").replace("\\", "\\\\").replace("'", "\\'")

def update_html(extractions):
    if not HTML_FILE.exists():
        print("[3/3] ⚠ bcs_calendar.html not found.")
        return

    print("[3/3] Updating bcs_calendar.html...")
    html = HTML_FILE.read_text(encoding="utf-8")
    changes = 0

    for data in extractions:
        if not data:
            continue

        # ── inject calendar day events ────────────────────────────────────────
        for ev in data.get("events", []):
            key = f"'{ev['date']}'"
            if key not in html:
                entry = f"  {key}:[{{t:'{ev['type']}',l:'{esc(ev['label'])}',tip:'{esc(ev['tip'])}'}}],\n"
                html = re.sub(
                    r"(\n};\s*\n\s*//\s*─+\s*WEEKLY NOTES)",
                    f"\n{entry}\\1", html
                )
                changes += 1
                print(f"  → Event added: {ev['date']} — {ev['label']}")

        # ── inject week note ──────────────────────────────────────────────────
        wn = data.get("weekNote")
        if wn and wn.get("from"):
            try:
                parsed = datetime.datetime.strptime(wn["date"], "%d %b %Y")
                mon    = parsed - datetime.timedelta(days=parsed.weekday())
                mon_key = f"'{mon.year}-{mon.month}-{mon.day}'"
            except Exception:
                mon_key = None

            if mon_key and mon_key not in html:
                tag_map = {
                    "Weekly Update":   ("🌱", "ic-g", "tg-g"),
                    "Action Required": ("⚠️",  "ic-y", "tg-r"),
                    "Announcement":    ("📣", "ic-b", "tg-b"),
                    "Opportunity":     ("⭐", "ic-b", "tg-b"),
                    "Class Update":    ("✏️",  "ic-g", "tg-g"),
                    "Health Notice":   ("🔔", "ic-p", "tg-p"),
                    "Newsletter":      ("📰", "ic-g", "tg-g"),
                }
                icon, cls, tg = tag_map.get(wn.get("tag", ""), ("📬", "ic-g", "tg-g"))
                hi = ",".join(
                    f"{{ic:'{esc(h['ic'])}',t:'{esc(h['t'])}'}}"
                    for h in wn.get("highlights", [])
                )
                act = f",action:'{esc(wn['action'])}'" if wn.get("action") else ""
                note = (
                    f"  {mon_key}: [{{icon:'{icon}',cls:'{cls}',"
                    f"from:'{esc(wn['from'])}',sub:'{esc(wn['sub'])}',"
                    f"date:'{esc(wn['date'])}',tag:'{esc(wn['tag'])}',tg:'{tg}',"
                    f"body:'{esc(wn['body'])}',"
                    f"highlights:[{hi}]{act}}}],\n"
                )
                html = re.sub(r"(const WEEK_NOTES = \{)", f"\\1\n{note}", html)
                changes += 1
                print(f"  → Week note added: {mon_key}")

        # ── update urgent banner ──────────────────────────────────────────────
        urgent = data.get("urgent", {})
        if urgent.get("show"):
            html = re.sub(r'class="urgent-banner(?!\s*show)[^"]*"', 'class="urgent-banner show"', html)
            if urgent.get("title"):
                html = re.sub(r"(<h3>).*?(</h3>)", f"\\g<1>{urgent['title']}\\g<2>", html, count=1)
            if urgent.get("body"):
                html = re.sub(
                    r'(<div class="ub-text">[\s\S]*?<h3>.*?</h3>[\s\S]*?<p>).*?(</p>)',
                    f"\\g<1>{urgent['body']}\\g<2>", html, count=1
                )
            if urgent.get("due"):
                html = re.sub(
                    r'(<div class="ub-badge">Due: ).*?(</div>)',
                    f"\\g<1>{urgent['due']}\\g<2>", html, count=1
                )
            changes += 1

    # ── update sync date + TODAY constant ─────────────────────────────────────
    today = datetime.date.today()
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    html = re.sub(
        r"Synced from Toddle · [\d]+ \w+ \d{4}",
        f"Synced from Toddle · {today.day} {months[today.month-1]} {today.year}",
        html
    )
    html = re.sub(
        r"const TODAY = new Date\(\d{4}, \d+, \d+\);",
        f"const TODAY = new Date({today.year}, {today.month - 1}, {today.day});",
        html
    )

    HTML_FILE.write_text(html, encoding="utf-8")
    print(f"  → {changes} change(s) written.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"BCS HTML Agent — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}\n")

    emails = fetch_emails()
    if not emails:
        print("Nothing to do.")
        return

    extractions = [extract(e) for e in emails]
    update_html(extractions)
    print("\n✅ Done.\n")

if __name__ == "__main__":
    main()
