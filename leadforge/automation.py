"""Hands-off outreach: follow-up sequences, a background scheduler, inbox
reply/bounce detection, audit-report tokens and webhooks.

The scheduler runs inside the web app (one asyncio task):
  every minute   -> turn due sequence steps into queued emails, send queued emails (AUTO_SEND)
  every 5 minutes -> read the inbox (IMAP) for replies and bounces
A reply, bounce, unsubscribe or status change stops a lead's sequences.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr

import httpx

from . import db
from .config import settings

STOP_STATUSES = {"replied", "interested", "won", "lost", "do_not_contact"}
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)
BOUNCE_SUBJECT = re.compile(r"undeliver|delivery status|delivery fail|returned mail|failure notice|could not be delivered",
                            re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")


def _utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ webhooks

def fire_webhook(event: str, payload: dict) -> None:
    """POST {event, data} to WEBHOOK_URL in the background (Zapier, n8n, Make, Slack...)."""
    if not settings.webhook_url:
        return

    async def _post() -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(settings.webhook_url, json={"event": event, "data": payload, "at": _utc()})
        except httpx.HTTPError:
            pass

    try:
        asyncio.get_running_loop().create_task(_post())
    except RuntimeError:  # no loop (CLI) -> send synchronously
        asyncio.run(_post())


def lead_summary(lead: dict) -> dict:
    keys = ("id", "name", "category", "website", "emails", "phones", "city", "country", "status", "score")
    return {k: lead.get(k) for k in keys}


# ------------------------------------------------------------------ audit report links

def ensure_report_token(lead_id: int) -> str:
    row = db.fetch_one("SELECT report_token FROM leads WHERE id = ?", (lead_id,))
    if row and row["report_token"]:
        return row["report_token"]
    token = secrets.token_urlsafe(9)
    db.execute("UPDATE leads SET report_token = ? WHERE id = ?", (token, lead_id))
    return token


def report_url(lead_id: int) -> str:
    return f"{settings.public_base_url}/r/{ensure_report_token(lead_id)}"


def record_report_view(lead: dict, user_agent: str = "") -> None:
    # Ignore link-preview bots (Gmail/Outlook/Slack scanners) so views mean a human looked.
    if re.search(r"bot|crawler|spider|preview|scanner|facebookexternalhit|slackbot|googleimageproxy", user_agent, re.I):
        return
    db.execute("UPDATE leads SET report_views = report_views + 1 WHERE id = ?", (lead["id"],))
    db.log_event(lead["id"], "report_view", user_agent[:200])
    if not lead.get("report_views"):
        # First human view: the lead is warm, bump it up the list.
        db.execute("UPDATE leads SET score = MIN(100, score + 15) WHERE id = ?", (lead["id"],))
        fire_webhook("report.viewed", lead_summary(lead))


# ------------------------------------------------------------------ sequences

def enroll(lead_ids: list[int], sequence_id: int) -> dict:
    seq = db.fetch_one("SELECT * FROM sequences WHERE id = ?", (sequence_id,))
    if not seq or not seq["steps"]:
        raise ValueError("Sequence not found or has no steps")
    first_delay = int(seq["steps"][0].get("delay_days", 0))
    stats = {"enrolled": 0, "skipped": 0}
    for lead_id in lead_ids:
        lead = db.fetch_one("SELECT * FROM leads WHERE id = ?", (lead_id,))
        ok = (lead and lead["emails"] and lead["status"] not in STOP_STATUSES
              and lead["email_status"] != "invalid" and not db.is_suppressed(lead["emails"][0]))
        if not ok:
            stats["skipped"] += 1
            continue
        next_at = _utc(datetime.now(timezone.utc) + timedelta(days=first_delay))
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO enrollments (lead_id, sequence_id, next_at, created_at) VALUES (?, ?, ?, ?)",
                (lead_id, sequence_id, next_at, _utc()))
        stats["enrolled" if cur.rowcount else "skipped"] += 1
    return stats


def stop_sequences(lead_id: int, reason: str) -> None:
    db.execute("UPDATE enrollments SET status='stopped', stop_reason=? WHERE lead_id=? AND status='active'",
               (reason, lead_id))
    db.execute("UPDATE messages SET status='skipped', error=? WHERE lead_id=? AND status='queued' AND enrollment_id IS NOT NULL",
               (f"sequence stopped: {reason}", lead_id))


def process_due_steps(now: datetime | None = None) -> int:
    """Create queued emails for every enrollment whose next step is due."""
    from .outreach import context_for, render  # avoid import cycle

    now = now or datetime.now(timezone.utc)
    due = db.fetch_all(
        "SELECT e.*, s.steps, c.services FROM enrollments e JOIN sequences s ON s.id = e.sequence_id "
        "JOIN leads l ON l.id = e.lead_id JOIN campaigns c ON c.id = l.campaign_id "
        "WHERE e.status = 'active' AND e.next_at <= ?", (_utc(now),))
    created = 0
    for enr in due:
        steps = enr["steps"] if isinstance(enr["steps"], list) else json.loads(enr["steps"])
        lead = db.fetch_one("SELECT * FROM leads WHERE id = ?", (enr["lead_id"],))
        reason = ""
        if not lead or not lead["emails"]:
            reason = "no email"
        elif lead["status"] in STOP_STATUSES:
            reason = f"lead {lead['status']}"
        elif db.is_suppressed(lead["emails"][0]) or lead["email_status"] == "invalid":
            reason = "unsubscribed or bounced"
        if reason:
            db.execute("UPDATE enrollments SET status='stopped', stop_reason=? WHERE id=?", (reason, enr["id"]))
            continue
        if enr["step"] >= len(steps):
            db.execute("UPDATE enrollments SET status='done' WHERE id=?", (enr["id"],))
            continue
        step = steps[enr["step"]]
        tpl = db.fetch_one("SELECT * FROM templates WHERE id = ?", (step.get("template_id"),))
        if tpl:
            ctx = context_for(lead, enr["services"])
            db.execute(
                "INSERT INTO messages (lead_id, channel, recipient, subject, body, status, created_at, enrollment_id) "
                "VALUES (?, 'email', ?, ?, ?, 'queued', ?, ?)",
                (lead["id"], lead["emails"][0], render(tpl["subject"], ctx), render(tpl["body"], ctx), db.now(), enr["id"]))
            created += 1
        nxt = enr["step"] + 1
        if nxt >= len(steps):
            db.execute("UPDATE enrollments SET step=?, status='done' WHERE id=?", (nxt, enr["id"]))
        else:
            next_at = _utc(now + timedelta(days=int(steps[nxt].get("delay_days", 1))))
            db.execute("UPDATE enrollments SET step=?, next_at=? WHERE id=?", (nxt, next_at, enr["id"]))
    return created


# ------------------------------------------------------------------ status changes

def set_status(lead_id: int, status: str, reason: str = "") -> None:
    lead = db.fetch_one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead or lead["status"] == status:
        return
    db.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    db.log_event(lead_id, "status", f"{lead['status']} -> {status} {reason}".strip())
    if status in STOP_STATUSES:
        stop_sequences(lead_id, status)
    fire_webhook("lead.status_changed", {**lead_summary(lead), "status": status, "previous": lead["status"]})


# ------------------------------------------------------------------ inbox (IMAP)

def _decode(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body_text(msg: email.message.Message, limit: int = 4000) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/plain":
            try:
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")[:limit]
            except Exception:
                continue
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "text" or part.get_content_type() == "message/delivery-status":
            payload = part.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", "replace")[:limit]
    return ""


def handle_incoming(msg: email.message.Message) -> str:
    """Classify one inbox message. Returns 'reply', 'bounce' or ''."""
    msg_id = (msg.get("Message-ID") or "").strip()
    if msg_id and db.fetch_one("SELECT 1 AS x FROM inbox_seen WHERE message_id = ?", (msg_id,)):
        return ""
    if msg_id:
        db.execute("INSERT OR IGNORE INTO inbox_seen (message_id, created_at) VALUES (?, ?)", (msg_id, db.now()))

    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = _decode(msg.get("Subject"))
    body = _body_text(msg)

    if BOUNCE_FROM.search(msg.get("From", "")) or BOUNCE_SUBJECT.search(subject):
        for addr in dict.fromkeys(a.lower() for a in EMAIL_RE.findall(body)):
            sent = db.fetch_one("SELECT * FROM messages WHERE recipient = ? AND status = 'sent' ORDER BY id DESC LIMIT 1",
                                (addr,))
            if sent:
                db.execute("UPDATE messages SET status='failed', error='bounced' WHERE id=?", (sent["id"],))
                db.execute("UPDATE leads SET email_status='invalid' WHERE id=?", (sent["lead_id"],))
                db.suppress(addr, "bounced")
                stop_sequences(sent["lead_id"], "bounced")
                db.log_event(sent["lead_id"], "bounce", addr)
                return "bounce"
        return ""

    # A reply: the sender is someone we emailed, or the message threads onto one of ours.
    refs = f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}"
    sent = db.fetch_one("SELECT * FROM messages WHERE recipient = ? AND status = 'sent' ORDER BY id DESC LIMIT 1",
                        (sender,))
    if not sent:
        for mid in re.findall(r"<[^>]+>", refs):
            sent = db.fetch_one("SELECT * FROM messages WHERE message_id = ?", (mid,))
            if sent:
                break
    if not sent:
        return ""
    lead_id = sent["lead_id"]
    db.execute("INSERT INTO messages (lead_id, channel, recipient, subject, body, status, created_at, sent_at) "
               "VALUES (?, 'email', ?, ?, ?, 'received', ?, ?)",
               (lead_id, sender, subject, body[:4000], db.now(), db.now()))
    db.log_event(lead_id, "reply", subject)
    if re.search(r"\b(unsubscribe|remove me|stop emailing|not interested)\b", body[:500], re.I):
        db.suppress(sender, "asked to stop (reply)")
        set_status(lead_id, "do_not_contact", "asked to stop")
    else:
        lead = db.fetch_one("SELECT status FROM leads WHERE id = ?", (lead_id,))
        if lead and lead["status"] in ("new", "contacted"):
            set_status(lead_id, "replied", "email reply")
        stop_sequences(lead_id, "replied")
    lead = db.fetch_one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    fire_webhook("lead.replied", {**lead_summary(lead), "subject": subject, "snippet": body[:500]})
    return "reply"


def check_inbox(days: int = 14) -> dict:
    """Scan recent inbox mail for replies and bounces (blocking; run in a thread)."""
    stats = {"replies": 0, "bounces": 0}
    if not settings.imap_configured:
        return stats
    box = (imaplib.IMAP4_SSL if settings.imap_use_ssl else imaplib.IMAP4)(settings.imap_host, settings.imap_port)
    try:
        box.login(settings.imap_user, settings.imap_password)
        box.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = box.search(None, "SINCE", since)
        for num in (data[0].split() if data and data[0] else [])[-500:]:
            _, parts = box.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if not raw:
                continue
            kind = handle_incoming(email.message_from_bytes(raw))
            if kind == "reply":
                stats["replies"] += 1
            elif kind == "bounce":
                stats["bounces"] += 1
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return stats


# ------------------------------------------------------------------ scheduler

_last_inbox_check = datetime.min.replace(tzinfo=timezone.utc)


async def tick() -> dict:
    """One scheduler pass. Safe to call any time (also from the 'Run automation now' button)."""
    global _last_inbox_check
    from .outreach import send_queued

    out = {"queued": process_due_steps(), "sent": 0, "replies": 0, "bounces": 0}
    if settings.auto_send and settings.smtp_configured:
        res = await send_queued()
        out["sent"] = res.get("sent", 0)
    now = datetime.now(timezone.utc)
    if settings.imap_configured and now - _last_inbox_check >= timedelta(minutes=5):
        _last_inbox_check = now
        try:
            res = await asyncio.to_thread(check_inbox)
            out.update(res)
        except Exception as exc:
            db.log_event(None, "inbox_error", str(exc))
    return out


async def scheduler_loop(interval: int = 60) -> None:
    while True:
        try:
            await tick()
        except Exception as exc:  # keep the loop alive no matter what
            db.log_event(None, "scheduler_error", str(exc))
        await asyncio.sleep(interval)
