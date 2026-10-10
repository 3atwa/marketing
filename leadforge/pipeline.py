"""Campaign runner: discover -> dedupe -> enrich -> score -> store."""
from __future__ import annotations

import asyncio
import traceback

from . import db
from .enrich import enrich_many
from .sources import SOURCES, osm
from .utils import dedupe_key, is_directory

_running: dict[int, asyncio.Task] = {}


def split_list(text: str) -> list[str]:
    import re

    return [p.strip() for p in re.split(r"[,\n;]+", text or "") if p.strip()]


async def location_country(location: str) -> str:
    """ISO country code for a location, so local phone numbers can be parsed."""
    try:
        place = await osm.geocode(location)
    except Exception:
        return ""
    return ((place or {}).get("address", {}).get("country_code") or "").upper()


async def run_campaign(campaign_id: int) -> None:
    camp = db.fetch_one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not camp:
        return

    def log(msg: str) -> None:
        db.append_log(campaign_id, msg)

    db.update_campaign(campaign_id, status="running")
    try:
        targets = split_list(camp["targets"])
        locations = [l.strip() for l in (camp["locations"] or "").splitlines() if l.strip()]
        sources = [s for s in camp["sources"] if s in SOURCES and SOURCES[s]["available"]()]
        if not (targets and locations and sources):
            raise ValueError("Campaign needs at least one business type, one location and one available source")

        found = 0
        for location in locations:
            country = await location_country(location)
            for target in targets:
                for src in sources:
                    log(f"Searching {src}: '{target}' in '{location}'")
                    try:
                        raw = await SOURCES[src]["search"](target, location, camp["max_per_query"], log)
                    except Exception as exc:
                        log(f"{src} failed: {exc}")
                        continue
                    for lead in raw:
                        lead["country"] = lead.get("country") or country
                        if lead.get("website") and is_directory(lead["website"]):
                            lead["website"] = ""
                        lead["dedupe_key"] = dedupe_key(lead.get("name", ""), lead.get("website", ""), lead.get("address", ""))
                        db.upsert_lead(campaign_id, lead)
                    found += len(raw)
                    log(f"{src}: {len(raw)} results for '{target}' in '{location}'")
                    if src == "osm":
                        await asyncio.sleep(1)  # Nominatim fair-use: max 1 req/s

        pending = db.fetch_all("SELECT * FROM leads WHERE campaign_id = ? AND enriched_at IS NULL", (campaign_id,))
        log(f"Discovery done ({found} raw results, {len(pending)} unique new leads). Crawling websites...")
        await enrich_many(pending, log)
        for lead in pending:
            db.save_lead_enrichment(lead)

        stats = db.fetch_one(
            "SELECT COUNT(*) AS total, SUM(emails != '[]') AS with_email, SUM(phones != '[]') AS with_phone "
            "FROM leads WHERE campaign_id = ?", (campaign_id,))
        log(f"Done: {stats['total']} leads, {stats['with_email'] or 0} with email, {stats['with_phone'] or 0} with phone")
        db.update_campaign(campaign_id, status="done")
    except Exception as exc:
        log(f"FAILED: {exc}\n{traceback.format_exc(limit=3)}")
        db.update_campaign(campaign_id, status="failed")
    finally:
        _running.pop(campaign_id, None)


def start_campaign(campaign_id: int) -> bool:
    if campaign_id in _running and not _running[campaign_id].done():
        return False
    db.update_campaign(campaign_id, status="running", progress="Queued")
    _running[campaign_id] = asyncio.get_running_loop().create_task(run_campaign(campaign_id))
    return True


async def re_enrich(lead_ids: list[int]) -> None:
    leads = [db.fetch_one("SELECT * FROM leads WHERE id = ?", (i,)) for i in lead_ids]
    leads = [l for l in leads if l]
    await enrich_many(leads)
    for lead in leads:
        db.save_lead_enrichment(lead)


# Accepted header names for CSV import (case-insensitive).
IMPORT_COLUMNS = {
    "name": ("name", "business", "business name", "company", "company name", "title"),
    "website": ("website", "url", "site", "web", "domain"),
    "emails": ("email", "emails", "e-mail", "email address"),
    "phones": ("phone", "phones", "telephone", "tel", "mobile", "phone number", "whatsapp"),
    "address": ("address", "street", "full address"),
    "city": ("city", "town"),
    "country": ("country", "country code"),
    "category": ("category", "type", "industry", "niche"),
}


def import_rows(campaign_id: int, rows: list[dict]) -> int:
    """Insert rows from an uploaded CSV, mapping common header names."""
    import re

    count = 0
    for row in rows:
        lower = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
        lead: dict = {"source": "import"}
        for field, names in IMPORT_COLUMNS.items():
            value = next((lower[n] for n in names if lower.get(n)), "")
            if field in ("emails", "phones"):
                lead[field] = [x.strip() for x in re.split(r"[;,|]", value) if x.strip()]
            else:
                lead[field] = value
        if lead["emails"]:
            lead["emails"] = [e.lower() for e in lead["emails"] if "@" in e]
        if len(lead["country"]) > 2:
            lead["country"] = ""  # we only use ISO codes for phone parsing
        if lead["website"] and not lead["website"].startswith("http"):
            lead["website"] = "https://" + lead["website"]
        if not (lead["name"] or lead["website"] or lead["emails"]):
            continue
        lead["name"] = lead["name"] or (lead["website"] or lead["emails"][0]).split("//")[-1].split("@")[-1]
        lead["dedupe_key"] = dedupe_key(lead["name"], lead["website"], lead["address"])
        db.upsert_lead(campaign_id, lead)
        count += 1
    return count


async def enrich_campaign(campaign_id: int) -> None:
    pending = db.fetch_all("SELECT * FROM leads WHERE campaign_id = ? AND enriched_at IS NULL", (campaign_id,))
    db.append_log(campaign_id, f"Enriching {len(pending)} imported leads...")
    await enrich_many(pending, lambda m: db.append_log(campaign_id, m))
    for lead in pending:
        db.save_lead_enrichment(lead)
    db.append_log(campaign_id, "Enrichment done")
