"""Email alerts for new declarations. Uses SMTP creds from env
(GitHub Actions secrets): SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
ALERT_TO (comma-separated). Silently no-ops when unconfigured so the
pipeline never fails on a missing secret."""
from __future__ import annotations

import html
import os
import smtplib
from email.mime.text import MIMEText

from scrapers.common import log


def send_new_declarations(records: list[dict]) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = [a.strip() for a in os.environ.get("ALERT_TO", "").split(",") if a.strip()]
    if not (user and pw and to):
        log.info("Alerts unconfigured — skipping email (%d new records)", len(records))
        return
    if not records:
        return

    sec = [r for r in records if r.get("is_securities")]
    rows = []
    for r in sorted(records, key=lambda x: (not x.get("is_securities"), x["disclosed"])):
        flag = "🟢 " if r.get("is_securities") else ""
        extra = ""
        ch = r.get("changes") or {}
        if ch.get("added") or ch.get("removed"):
            extra = (f"<br><small>▲ {html.escape(', '.join(ch.get('added', [])[:6]))}"
                     f" &nbsp; ▼ {html.escape(', '.join(ch.get('removed', [])[:6]))}</small>")
        rows.append(
            f"<li>{flag}<b>{html.escape(r['person'])}</b> — "
            f"<a href='{html.escape(r.get('url', ''))}'>{html.escape(r['doc'])}</a> "
            f"({html.escape(r.get('type', ''))}, disclosed {r['disclosed']}){extra}</li>")

    subject = (f"Blind Trust: {len(records)} new filing{'s' if len(records) != 1 else ''}"
               + (f", {len(sec)} securities" if sec else ""))
    body = (f"<p>{len(records)} new public-registry filing(s) since the last run.</p>"
            f"<ul>{''.join(rows)}</ul>"
            f"<p><small>Events are disclosure states, not confirmed trades. "
            f"Source: CIEC Public Registry.</small></p>")
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as srv:
            srv.login(user, pw)
            srv.sendmail(user, to, msg.as_string())
        log.info("Alert email sent to %s", to)
    except Exception as exc:  # noqa: BLE001
        log.error("Alert email failed: %s", exc)
