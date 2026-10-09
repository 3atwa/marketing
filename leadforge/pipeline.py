"""Campaign runner: discover -> dedupe -> enrich -> score -> store."""
from __future__ import annotations

import asyncio
import traceback

from . import db
from .enrich import enrich_many
from .sources import SOURCES
from .utils import dedupe_key, is_directory

_running: dict[int, asyncio.Task] = {}


def split_list(text: str) -> list[str]:
    import re

    return [p.strip() for p in re.split(r"[,\n;]+", text or "") if p.strip()]


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
            for target in targets:
                for src in sources:
                    log(f"Searching {src}: '{target}' in '{location}'")
                    try:
                        raw = await SOURCES[src]["search"](target, location, camp["max_per_query"], log)
                    except Exception as exc:
                        log(f"{src} failed: {exc}")
                        continue
                    for lead in raw:
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
            db.save_enrichment(lead["id"], lead["emails"], lead["phones"], lead["socials"],
                               lead["signals"], lead.get("address", ""), lead["score"])

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
        db.save_enrichment(lead["id"], lead["emails"], lead["phones"], lead["socials"],
                           lead["signals"], lead.get("address", ""), lead["score"])
