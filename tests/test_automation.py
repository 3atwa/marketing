import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

os.environ.setdefault("LEADFORGE_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from leadforge import automation, db, pipeline  # noqa: E402
from leadforge.audit import audit_site, detect_tech  # noqa: E402
from leadforge.config import settings  # noqa: E402
from leadforge.main import app  # noqa: E402


def setup_lead(email="owner@salon.test", name="Salon"):
    db.init_db()
    cid = db.create_campaign("auto", "booking app", "hair salon", "Dubai", ["osm"], 10)
    lid = db.upsert_lead(cid, {"name": name, "category": "hair salon", "emails": [email], "dedupe_key": email})
    return cid, lid


def seq_id():
    return db.fetch_one("SELECT id FROM sequences ORDER BY id LIMIT 1")["id"]


def test_sequence_runs_steps_and_stops_on_reply(monkeypatch):
    monkeypatch.setattr(automation.settings, "public_base_url", "https://leads.example")
    _, lid = setup_lead("owner@seq.test", "Seq Salon")
    assert automation.enroll([lid], seq_id())["enrolled"] == 1
    assert automation.enroll([lid], seq_id())["enrolled"] == 0  # no double enrollment

    assert automation.process_due_steps() >= 1
    first = db.fetch_one("SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lid,))
    assert first["status"] == "queued" and first["enrollment_id"]
    assert "https://leads.example/r/" in first["body"]  # audit report link rendered

    # Step 2 isn't due yet; three days later it is.
    assert automation.process_due_steps() == 0
    later = datetime.now(timezone.utc) + timedelta(days=3, minutes=1)
    assert automation.process_due_steps(later) == 1

    # Mark the first email sent, then the lead replies -> sequence stops, queued follow-up skipped.
    db.execute("UPDATE messages SET status='sent', message_id='<abc@me>' WHERE id=?", (first["id"],))
    reply = EmailMessage()
    reply["From"] = "Owner <owner@seq.test>"
    reply["Subject"] = "Re: I checked Seq Salon's website"
    reply["Message-ID"] = "<reply1@salon>"
    reply["In-Reply-To"] = "<abc@me>"
    reply.set_content("Sounds interesting, can you call me tomorrow?")
    assert automation.handle_incoming(reply) == "reply"
    assert automation.handle_incoming(reply) == ""  # same message isn't processed twice
    lead = db.fetch_one("SELECT * FROM leads WHERE id=?", (lid,))
    assert lead["status"] == "replied"
    enr = db.fetch_one("SELECT * FROM enrollments WHERE lead_id=?", (lid,))
    assert enr["status"] == "stopped"
    statuses = [m["status"] for m in db.fetch_all("SELECT status FROM messages WHERE lead_id=? ORDER BY id", (lid,))]
    assert statuses == ["sent", "skipped", "received"]


def test_bounce_marks_invalid_and_suppresses():
    _, lid = setup_lead("gone@bounce.test", "Bounce Co")
    db.execute("INSERT INTO messages (lead_id, channel, recipient, subject, body, status, created_at) "
               "VALUES (?, 'email', 'gone@bounce.test', 's', 'b', 'sent', ?)", (lid, db.now()))
    dsn = EmailMessage()
    dsn["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    dsn["Subject"] = "Delivery Status Notification (Failure)"
    dsn["Message-ID"] = "<dsn1@google>"
    dsn.set_content("Your message wasn't delivered to gone@bounce.test because the address couldn't be found.")
    assert automation.handle_incoming(dsn) == "bounce"
    assert db.is_suppressed("gone@bounce.test")
    assert db.fetch_one("SELECT email_status FROM leads WHERE id=?", (lid,))["email_status"] == "invalid"
    # invalid leads can't be enrolled
    assert automation.enroll([lid], seq_id())["enrolled"] == 0


def test_unsubscribe_reply_sets_do_not_contact():
    _, lid = setup_lead("stop@me.test", "Stop Co")
    db.execute("INSERT INTO messages (lead_id, channel, recipient, subject, body, status, created_at) "
               "VALUES (?, 'email', 'stop@me.test', 's', 'b', 'sent', ?)", (lid, db.now()))
    m = EmailMessage()
    m["From"] = "stop@me.test"
    m["Subject"] = "Re: hi"
    m["Message-ID"] = "<stop1@me>"
    m.set_content("Please remove me from your list.")
    automation.handle_incoming(m)
    assert db.is_suppressed("stop@me.test")
    assert db.fetch_one("SELECT status FROM leads WHERE id=?", (lid,))["status"] == "do_not_contact"


def test_report_page_tracks_humans_not_bots():
    _, lid = setup_lead("view@r.test", "Report Salon")
    pages = {"https://r.test/": '<html><head><title>Report Salon Dubai</title><meta name="viewport" content="x">'
                                '<script src="https://www.googletagmanager.com/gtag/js"></script></head>'
                                '<body><h1>Hair</h1><p>Report Salon is a family-run hair salon in Dubai Marina offering cuts, colour and '
                                'bridal styling since 2012.</p><a href="https://wa.me/971500000000">wa</a> wp-content</body></html>'}
    tech = detect_tech(pages["https://r.test/"])
    assert {"Google Analytics", "WhatsApp chat button", "WordPress"} <= set(tech)
    audit = audit_site(pages, "https://r.test/", 900, {"instagram": "x"}, tech)
    db.save_enrichment(lid, ["view@r.test"], [], {}, [], "", 50, tech=tech, audit=audit)
    token = automation.ensure_report_token(lid)
    with TestClient(app) as client:
        r = client.get(f"/r/{token}", headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
        assert r.status_code == 200 and "Report Salon" in r.text and "Online booking" in r.text
        client.get(f"/r/{token}", headers={"User-Agent": "Googlebot/2.1"})
        client.get(f"/r/{token}?preview=1", headers={"User-Agent": "Mozilla/5.0"})
        assert client.get("/r/nope").status_code == 404
    assert db.fetch_one("SELECT report_views FROM leads WHERE id=?", (lid,))["report_views"] == 1
    checks = {c["key"]: c["ok"] for c in audit["checks"]}
    assert checks["https"] and checks["mobile"] and checks["chat"] and checks["analytics"] and not checks["booking"]


def test_csv_import_and_pages(monkeypatch):
    async def no_crawl(cid):
        return None

    monkeypatch.setattr(pipeline, "enrich_campaign", no_crawl)
    db.init_db()
    csv_text = ("Company;Website;E-mail;Phone;City\n"
                "Glow Spa;glowspa.test;hello@glowspa.test;+971 4 123 4567;Dubai\n"
                "Glow Spa;glowspa.test;;;Dubai\n"
                ";;;;\n")
    with TestClient(app) as client:
        r = client.post("/import", data={"name": "Imported", "services": "booking app", "enrich": "1"},
                        files={"file": ("list.csv", csv_text, "text/csv")}, follow_redirects=False)
        assert r.status_code == 303
        cid = int(r.headers["location"].rsplit("/", 1)[1])
        leads = db.fetch_all("SELECT * FROM leads WHERE campaign_id=?", (cid,))
        assert len(leads) == 1  # duplicate merged, empty row skipped
        assert leads[0]["website"] == "https://glowspa.test" and leads[0]["emails"] == ["hello@glowspa.test"]
        lid = leads[0]["id"]
        for path in ["/", "/pipeline", f"/pipeline?campaign={cid}", "/sequences", "/import", "/settings",
                     f"/campaigns/{cid}", f"/leads/{lid}"]:
            assert client.get(path).status_code == 200, path
        assert client.post(f"/leads/{lid}/status", json={"status": "interested"}).json() == {"ok": True}
        assert db.fetch_one("SELECT status FROM leads WHERE id=?", (lid,))["status"] == "interested"
        # bulk enroll from the campaign page skips interested leads
        r = client.post(f"/campaigns/{cid}/bulk", data={"lead_ids": [str(lid)], "action": "enroll",
                                                        "sequence_id": str(seq_id())}, follow_redirects=False)
        assert "Enrolled+0" in r.headers["location"]


def test_tick_without_smtp_or_imap(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "imap_host", "")
    out = asyncio.run(automation.tick())
    assert set(out) >= {"queued", "sent", "replies", "bounces"} and out["sent"] == 0


def test_parked_and_js_only_sites_are_not_graded():
    from leadforge.enrich import client_redirect

    stub = '<html><head><script>window.onload=function(){window.location.href="/lander"}</script></head></html>'
    assert client_redirect(stub, "https://medicure.test/") == "https://medicure.test/lander"
    assert client_redirect('<meta http-equiv="refresh" content="0; url=https://x.test/home">', "https://x.test/") == \
        "https://x.test/home"
    parked = '<script>window._trfd.push({ap:"parking"})</script><script src="https://img1.wsimg.com/parking-lander/x.js">'
    assert audit_site({"u": parked}, "https://medicure.test/lander", 100, {}, [])["parked"]
    spa = '<html><head><title>App</title></head><body><div id="root"></div><script src="/main.js"></script></body></html>'
    res = audit_site({"u": spa}, "https://spa.test/", 100, {}, [])
    assert res["incomplete"] and res["checks"] == []
