import os
import tempfile

os.environ["LEADFORGE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from leadforge import db, outreach, pipeline  # noqa: E402
from leadforge.main import app  # noqa: E402


def make_lead(cid, **kw):
    lead = {"name": "Smile Co", "category": "dentist", "website": "https://smileco.com",
            "emails": ["info@smileco.com"], "phones": ["+1 512-555-0199"], "city": "Austin",
            "signals": ["No online booking/ordering found"], "dedupe_key": "d:smileco.com", **kw}
    return db.upsert_lead(cid, lead)


def test_dashboard_and_campaign_flow(monkeypatch):
    monkeypatch.setattr(pipeline, "start_campaign", lambda cid: True)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        r = client.post("/campaigns", data={"services": "booking app", "targets": "dentist",
                                            "locations": "Austin, TX", "sources": ["osm"]}, follow_redirects=False)
        assert r.status_code == 303
        cid = int(r.headers["location"].rsplit("/", 1)[1])
        lid = make_lead(cid)
        # duplicate merges contact data instead of creating a new row
        assert make_lead(cid, emails=["hello@smileco.com"]) == lid
        lead = db.fetch_one("SELECT * FROM leads WHERE id=?", (lid,))
        assert lead["emails"] == ["info@smileco.com", "hello@smileco.com"]

        page = client.get(f"/campaigns/{cid}")
        assert page.status_code == 200 and "Smile Co" in page.text
        assert client.get(f"/leads/{lid}").status_code == 200
        csv_text = client.get(f"/campaigns/{cid}/export.csv").text
        assert "info@smileco.com" in csv_text

        tid = db.fetch_one("SELECT id FROM templates ORDER BY id")["id"]
        r = client.post(f"/campaigns/{cid}/bulk", data={"lead_ids": [str(lid)], "template_id": str(tid),
                                                        "action": "draft", "bulk_action": ""}, follow_redirects=False)
        assert r.status_code == 303
        msg = db.fetch_one("SELECT * FROM messages WHERE lead_id=?", (lid,))
        assert msg["status"] == "draft"
        assert "Smile Co" in msg["subject"]
        assert "dentists:" in msg["body"] and "book or order online" in msg["body"]
        assert "{{" not in msg["body"]
        assert client.get("/outbox").status_code == 200


def test_unsubscribe_blocks_future_drafts():
    with TestClient(app) as client:
        cid = db.create_campaign("t", "app", "gym", "Paris", ["osm"], 10)
        lid = make_lead(cid, emails=["owner@gym.fr"], dedupe_key="d:gym.fr", name="Gym")
        bad = client.post("/unsubscribe", data={"e": "owner@gym.fr", "t": "wrong"})
        assert "Invalid" in bad.text and not db.is_suppressed("owner@gym.fr")
        ok = client.post("/unsubscribe", data={"e": "owner@gym.fr", "t": outreach.unsubscribe_token("owner@gym.fr")})
        assert "unsubscribed" in ok.text and db.is_suppressed("owner@gym.fr")
        assert db.fetch_one("SELECT status FROM leads WHERE id=?", (lid,))["status"] == "do_not_contact"


def test_links_and_render():
    assert outreach.whatsapp_link("+1 (512) 555-0199", "hi there") == "https://wa.me/15125550199?text=hi%20there"
    assert outreach.tel_link("+1 512-555-0199") == "tel:+15125550199"
    assert outreach.render("Hi {{business_name}}{{ ' in ' + city if city else '' }}", {"business_name": "X", "city": ""}) == "Hi X"
