"""Google Places API (New) Text Search - official, needs an API key.

https://developers.google.com/maps/documentation/places/web-service/text-search
"""
from __future__ import annotations

from ..config import settings
from ..utils import http_client

FIELDS = ",".join([
    "places.displayName", "places.formattedAddress", "places.internationalPhoneNumber",
    "places.nationalPhoneNumber", "places.websiteUri", "places.location", "places.primaryTypeDisplayName",
    "places.rating", "places.userRatingCount", "places.addressComponents", "places.businessStatus",
    "nextPageToken",
])


async def search(target: str, location: str, limit: int = 40, log=print) -> list[dict]:
    if not settings.google_places_api_key:
        log("Google Places: GOOGLE_PLACES_API_KEY not set, skipping")
        return []
    headers = {"X-Goog-Api-Key": settings.google_places_api_key, "X-Goog-FieldMask": FIELDS}
    body: dict = {"textQuery": f"{target} in {location}", "pageSize": 20}
    leads: list[dict] = []
    async with http_client() as client:
        while len(leads) < limit:
            r = await client.post("https://places.googleapis.com/v1/places:searchText", json=body, headers=headers)
            if r.status_code != 200:
                log(f"Google Places error {r.status_code}: {r.text[:200]}")
                break
            data = r.json()
            for p in data.get("places", []):
                if p.get("businessStatus") == "CLOSED_PERMANENTLY":
                    continue
                comps = {t: c.get("longText") for c in p.get("addressComponents", []) for t in c.get("types", [])}
                short = {t: c.get("shortText") for c in p.get("addressComponents", []) for t in c.get("types", [])}
                phone = p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber")
                leads.append({
                    "name": (p.get("displayName") or {}).get("text", ""),
                    "category": (p.get("primaryTypeDisplayName") or {}).get("text") or target,
                    "website": p.get("websiteUri", ""),
                    "phones": [phone] if phone else [],
                    "address": p.get("formattedAddress", ""),
                    "city": comps.get("locality") or comps.get("postal_town") or "",
                    "country": short.get("country", ""),
                    "lat": (p.get("location") or {}).get("latitude"),
                    "lon": (p.get("location") or {}).get("longitude"),
                    "rating": p.get("rating"),
                    "review_count": p.get("userRatingCount"),
                    "source": "google_places",
                })
            token = data.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
    return leads[:limit]
