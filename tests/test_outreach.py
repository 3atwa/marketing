import asyncio
import os
import tempfile

os.environ.setdefault("LEADFORGE_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from leadforge import db, outreach  # noqa: E402
from leadforge.config import settings  # noqa: E402


class FakeSMTP:
    sent = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_send_queued(monkeypatch):
    monkeypatch.setattr(outreach.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "from_email", "me@myapps.test")
    monkeypatch.setattr(settings, "sender_address", "1 Road, City")
    monkeypatch.setattr(settings, "send_delay_seconds", 0)
    monkeypatch.setattr(settings, "daily_send_limit", 2)
    db.init_db()
    cid = db.create_campaign("send", "app", "cafe", "Rome", ["osm"], 10)
    ids = []
    for i, email in enumerate(["a@cafe1.test", "b@cafe2.test", "blocked@cafe3.test", "c@cafe4.test"]):
        lid = db.upsert_lead(cid, {"name": f"Cafe {i}", "emails": [email], "dedupe_key": f"k{i}"})
        db.execute("INSERT INTO messages (lead_id, channel, recipient, subject, body, status, created_at) "
                   "VALUES (?, 'email', ?, 'Hi', 'Hello there', 'queued', ?)", (lid, email, db.now()))
        ids.append(lid)
    db.suppress("blocked@cafe3.test", "test")

    stats = asyncio.run(outreach.send_queued())
    assert stats["sent"] == 2 and "Daily limit" in stats["note"]
    msg = FakeSMTP.sent[0]
    body = msg.get_content()
    assert "1 Road, City" in body and "/unsubscribe?e=a%40cafe1.test&t=" in body
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert db.fetch_one("SELECT status FROM leads WHERE id=?", (ids[0],))["status"] == "contacted"

    monkeypatch.setattr(settings, "daily_send_limit", 10)
    asyncio.run(outreach.send_queued())
    statuses = {r["recipient"]: r["status"] for r in db.fetch_all("SELECT recipient, status FROM messages")}
    assert statuses["blocked@cafe3.test"] == "skipped"
    assert statuses["c@cafe4.test"] == "sent"
