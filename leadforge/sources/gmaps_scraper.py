"""Adapter for gosom/google-maps-scraper (MIT) running in web/API mode.

Run it next to LeadForge (see docker-compose.yml) and set GMAPS_SCRAPER_URL,
e.g. http://gmaps:8080. We create a job, poll until it finishes, then parse
the CSV it produces. Note: scraping Google Maps may conflict with Google's
Terms of Service - you are responsible for how you use it.
https://github.com/gosom/google-maps-scraper
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time

from ..config import settings
from ..utils import http_client

POLL_SECONDS = 15
MAX_WAIT_SECONDS = 30 * 60


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_csv(text: str, target: str) -> list[dict]:
    leads = []
    for row in csv.DictReader(io.StringIO(text)):
        emails = row.get("emails") or ""
        try:
            parsed = json.loads(emails) if emails.startswith("[") else emails.split(",")
        except json.JSONDecodeError:
            parsed = emails.split(",")
        complete = {}
        try:
            complete = json.loads(row.get("complete_address") or "{}") or {}
        except json.JSONDecodeError:
            pass
        leads.append({
            "name": row.get("title", ""),
            "category": row.get("category") or target,
            "website": row.get("website", ""),
            "phones": [row["phone"]] if row.get("phone") else [],
            "emails": [e.strip().lower() for e in parsed if e and "@" in e],
            "address": row.get("address", ""),
            "city": complete.get("city", ""),
            "country": complete.get("country", ""),
            "lat": _float(row.get("latitude")),
            "lon": _float(row.get("longitude")),
            "rating": _float(row.get("review_rating")),
            "review_count": _int(row.get("review_count")),
            "source": "gmaps_scraper",
        })
    return leads


async def search(target: str, location: str, limit: int = 40, log=print) -> list[dict]:
    base = settings.gmaps_scraper_url
    if not base:
        log("gmaps scraper: GMAPS_SCRAPER_URL not set, skipping")
        return []
    payload = {
        "name": f"leadforge {target} {location}"[:80],
        "keywords": [f"{target} in {location}"],
        "lang": "en",
        "depth": max(1, min(10, limit // 20 + 1)),
        "email": True,
        "max_time": 600,  # seconds; the API requires >= 180
        "zoom": 15,
    }
    async with http_client() as client:
        r = await client.post(f"{base}/api/v1/jobs", json=payload)
        if r.status_code >= 300:
            log(f"gmaps scraper: job create failed {r.status_code}: {r.text[:200]}")
            return []
        job_id = r.json()["id"]
        log(f"gmaps scraper: job {job_id} started (takes several minutes)")
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            job = (await client.get(f"{base}/api/v1/jobs/{job_id}")).json()
            status = job.get("Status") or job.get("status")
            if status == "ok":
                break
            if status == "failed":
                log("gmaps scraper: job failed")
                return []
        else:
            log("gmaps scraper: timed out waiting for job")
            return []
        csv_resp = await client.get(f"{base}/api/v1/jobs/{job_id}/download", timeout=120)
        csv_resp.raise_for_status()
    return parse_csv(csv_resp.text, target)[:limit]
