"""Outreach: render templates, queue emails, send via SMTP, WhatsApp/call links.

Compliance built in (you are still responsible for following the law where
you and your leads are - CAN-SPAM, GDPR/PECR, CASL, etc.):
  * every email gets your postal address + a one-click unsubscribe link
  * List-Unsubscribe headers are set
  * unsubscribed / do-not-contact leads are never emailed again
  * a daily send cap and a delay between sends keep volume human-scale
WhatsApp is click-to-chat only (opens wa.me with a pre-filled message) because
automated WhatsApp messaging to people who did not opt in breaks its ToS.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import quote

from jinja2.sandbox import SandboxedEnvironment

from . import ai, db
from .config import settings
from .utils import pluralize

_env = SandboxedEnvironment(autoescape=False)

SIGNAL_LINES = {
    "No online booking/ordering found": "I noticed customers can't book or order online on your website yet.",
    "Website not mobile-friendly": "I noticed your website is hard to use on a phone, which is where most customers find you.",
    "No mobile app linked": "",
    "No live chat": "",
    "No HTTPS on website": "I noticed your website shows a 'not secure' warning in browsers.",
    "No website": "I couldn't find a website for you, which means customers searching online may miss you.",
}


def signal_line(signals: list[str]) -> str:
    for s in signals or []:
        if s.startswith("Website looks outdated"):
            return "It looks like your website hasn't been updated in a while."
        line = SIGNAL_LINES.get(s)
        if line:
            return line
    return ""


def context_for(lead: dict, services: str) -> dict:
    return {
        "business_name": lead.get("name") or "there",
        "category": lead.get("category") or "business",
        "category_plural": pluralize(lead.get("category") or "business"),
        "city": lead.get("city") or "",
        "website": lead.get("website") or "",
        "rating": lead.get("rating") or "",
        "services": services or "custom apps and automation",
        "signal_line": signal_line(lead.get("signals") or []),
        "my_name": settings.from_name,
        "my_company": settings.sender_company,
    }


def render(template: str, ctx: dict) -> str:
    out = _env.from_string(template).render(**ctx)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", out)).strip()


def unsubscribe_token(email: str) -> str:
    return hmac.new(settings.secret_key.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:24]


def unsubscribe_url(email: str) -> str:
    return f"{settings.public_base_url}/unsubscribe?e={quote(email)}&t={unsubscribe_token(email)}"


def footer(email: str) -> str:
    parts = ["\n\n--"]
    if settings.sender_company or settings.sender_address:
        parts.append(", ".join(filter(None, [settings.sender_company, settings.sender_address])))
    parts.append(f"Not interested? Unsubscribe with one click: {unsubscribe_url(email)}")
    return "\n".join(parts)


def whatsapp_link(phone: str, text: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"https://wa.me/{digits}?text={quote(text)}" if len(digits) >= 8 else ""


def tel_link(phone: str) -> str:
    return "tel:" + re.sub(r"[^\d+]", "", phone or "")


def mailto_link(email: str, subject: str, body: str) -> str:
    return f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"


async def create_drafts(lead_ids: list[int], template_id: int, services: str, use_ai: bool) -> dict:
    tpl = db.fetch_one("SELECT * FROM templates WHERE id = ?", (template_id,))
    if not tpl:
        raise ValueError("Template not found")
    stats = {"drafted": 0, "skipped": 0}
    sender = " / ".join(filter(None, [settings.from_name, settings.sender_company]))
    for lead_id in lead_ids:
        lead = db.fetch_one("SELECT * FROM leads WHERE id = ?", (lead_id,))
        if not lead or lead["status"] == "do_not_contact" or not lead["emails"]:
            stats["skipped"] += 1
            continue
        email = lead["emails"][0]
        if db.is_suppressed(email):
            stats["skipped"] += 1
            continue
        ctx = context_for(lead, services)
        subject, body = render(tpl["subject"], ctx), render(tpl["body"], ctx)
        if use_ai:
            better = await ai.personalize(lead, services, sender, subject, body)
            if better:
                subject, body = better
        db.execute(
            "INSERT INTO messages (lead_id, channel, recipient, subject, body, status, created_at) "
            "VALUES (?, 'email', ?, ?, ?, 'draft', ?)",
            (lead_id, email, subject, body, db.now()),
        )
        stats["drafted"] += 1
    return stats


def sent_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    row = db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE channel='email' AND status='sent' AND sent_at >= ?",
                       (today,))
    return row["n"] if row else 0


def _send_one(msg_row: dict) -> None:
    email = msg_row["recipient"]
    msg = EmailMessage()
    msg["From"] = formataddr((settings.from_name or settings.sender_company, settings.from_email))
    msg["To"] = email
    msg["Subject"] = msg_row["subject"]
    msg["Message-ID"] = make_msgid(domain=settings.from_email.split("@")[-1])
    msg["List-Unsubscribe"] = f"<{unsubscribe_url(email)}>, <mailto:{settings.from_email}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(msg_row["body"] + footer(email))

    if settings.smtp_use_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl.create_default_context(), timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        server.starttls(context=ssl.create_default_context())
    with server:
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


_sending = False


async def send_queued(log=print) -> dict:
    """Send queued emails respecting the daily cap and delay. Safe to call repeatedly."""
    global _sending
    if _sending:
        return {"sent": 0, "failed": 0, "note": "already sending"}
    if not settings.smtp_configured:
        return {"sent": 0, "failed": 0, "note": "SMTP not configured - use the mailto links instead"}
    _sending = True
    stats = {"sent": 0, "failed": 0, "note": ""}
    try:
        while True:
            if sent_today() >= settings.daily_send_limit:
                stats["note"] = f"Daily limit of {settings.daily_send_limit} reached; the rest stay queued"
                break
            row = db.fetch_one("SELECT * FROM messages WHERE channel='email' AND status='queued' ORDER BY id LIMIT 1")
            if not row:
                break
            lead = db.fetch_one("SELECT status FROM leads WHERE id = ?", (row["lead_id"],))
            if db.is_suppressed(row["recipient"]) or (lead and lead["status"] == "do_not_contact"):
                db.execute("UPDATE messages SET status='skipped', error='unsubscribed' WHERE id=?", (row["id"],))
                continue
            try:
                await asyncio.to_thread(_send_one, row)
                db.execute("UPDATE messages SET status='sent', sent_at=? WHERE id=?", (db.now(), row["id"]))
                db.execute("UPDATE leads SET status='contacted' WHERE id=? AND status='new'", (row["lead_id"],))
                stats["sent"] += 1
            except Exception as exc:
                db.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(exc)[:500], row["id"]))
                stats["failed"] += 1
            await asyncio.sleep(settings.send_delay_seconds)
    finally:
        _sending = False
    return stats
