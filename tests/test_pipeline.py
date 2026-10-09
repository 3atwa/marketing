"""End-to-end: fake source -> real crawler against a local website -> DB."""
import asyncio
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("LEADFORGE_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from leadforge import db, pipeline  # noqa: E402
from leadforge.sources import SOURCES  # noqa: E402
from leadforge.sources.osm import _filters_for  # noqa: E402

PAGES = {
    "/robots.txt": "User-agent: *\nDisallow: /private\n",
    "/": '<html><head><meta name="viewport" content="width=device-width"></head><body>'
         '<a href="/contact-us">Contact</a> <a href="/private/team">Team</a>'
         '<a href="https://instagram.com/bestgym">ig</a> &copy; 2026</body></html>',
    "/contact-us": "<html><body>Write to owner@127.0.0.1.nip.io or call +44 20 7946 0958"
                   '<a href="tel:+442079460000">tel</a></body></html>',
    "/private/team": "<html><body>secret@hidden.com</body></html>",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


def test_full_pipeline(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_port}"

    async def fake_search(target, location, limit, log):
        return [
            {"name": "Best Gym", "category": target, "website": site, "city": location, "country": "GB", "source": "fake"},
            {"name": "Best Gym Ltd", "category": target, "website": site + "/", "source": "fake"},  # duplicate
            {"name": "No Site Gym", "category": target, "phones": ["+44 20 7946 0123"], "source": "fake"},
            {"name": "Yelp listing", "category": target, "website": "https://www.yelp.com/biz/x", "source": "fake"},
        ]

    monkeypatch.setitem(SOURCES, "fake", {"label": "fake", "search": fake_search, "available": lambda: True})
    db.init_db()
    cid = db.create_campaign("e2e", "gym app", "gym", "London", ["fake"], 10)
    asyncio.run(pipeline.run_campaign(cid))
    server.shutdown()

    camp = db.fetch_one("SELECT * FROM campaigns WHERE id=?", (cid,))
    assert camp["status"] == "done", camp["log"]
    leads = {l["name"]: l for l in db.fetch_all("SELECT * FROM leads WHERE campaign_id=?", (cid,))}
    assert len(leads) == 3  # duplicate merged by domain
    gym = leads["Best Gym"]
    assert gym["emails"] == ["owner@127.0.0.1.nip.io"]
    assert "secret@hidden.com" not in gym["emails"]  # robots.txt respected
    assert "+44 20 7946 0000" in gym["phones"] and "+44 20 7946 0958" in gym["phones"]
    assert gym["socials"]["instagram"] == "https://instagram.com/bestgym"
    assert "No HTTPS on website" in gym["signals"]
    assert gym["score"] >= 60
    assert leads["No Site Gym"]["signals"] == ["No website"]
    assert leads["Yelp listing"]["website"] == ""  # directories are not a business's website


def test_osm_filters():
    assert _filters_for("Dentists") == ['["amenity"="dentist"]', '["healthcare"="dentist"]']
    fallback = _filters_for("escape room")
    assert '"^escape_room$"' in fallback[0] and '"escape room"' in fallback[1]
