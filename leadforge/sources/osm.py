"""OpenStreetMap: Nominatim to geocode the location, Overpass to list businesses.

Free and legal to use (ODbL data, attribution required). Coverage of phone and
website tags is good in Europe and decent in most big cities elsewhere.
Usage policy: https://operations.osmfoundation.org/policies/nominatim/
"""
from __future__ import annotations

import asyncio
import re

import httpx

from ..config import settings
from ..utils import http_client

# Common business types -> OSM tag filters. Anything not listed falls back to
# a tag-value and name match, which still works for most English terms.
TAG_MAP: dict[str, list[str]] = {
    "dentist": ['["amenity"="dentist"]', '["healthcare"="dentist"]'],
    "doctor": ['["amenity"="doctors"]', '["healthcare"="doctor"]'],
    "clinic": ['["amenity"="clinic"]', '["healthcare"="clinic"]'],
    "pharmacy": ['["amenity"="pharmacy"]'],
    "physiotherapist": ['["healthcare"="physiotherapist"]'],
    "veterinarian": ['["amenity"="veterinary"]'],
    "vet": ['["amenity"="veterinary"]'],
    "restaurant": ['["amenity"="restaurant"]'],
    "cafe": ['["amenity"="cafe"]'],
    "coffee shop": ['["amenity"="cafe"]'],
    "bar": ['["amenity"="bar"]', '["amenity"="pub"]'],
    "fast food": ['["amenity"="fast_food"]'],
    "bakery": ['["shop"="bakery"]'],
    "hair salon": ['["shop"="hairdresser"]'],
    "hairdresser": ['["shop"="hairdresser"]'],
    "barber": ['["shop"="hairdresser"]["hairdresser"~"barber",i]', '["shop"="barber"]'],
    "beauty salon": ['["shop"="beauty"]'],
    "nail salon": ['["shop"="beauty"]["beauty"~"nails",i]'],
    "spa": ['["leisure"="spa"]', '["shop"="beauty"]["beauty"~"spa",i]'],
    "massage": ['["shop"="massage"]'],
    "gym": ['["leisure"="fitness_centre"]'],
    "fitness": ['["leisure"="fitness_centre"]'],
    "yoga studio": ['["leisure"="fitness_centre"]["sport"~"yoga",i]', '["sport"="yoga"]'],
    "hotel": ['["tourism"="hotel"]'],
    "guest house": ['["tourism"="guest_house"]'],
    "real estate": ['["office"="estate_agent"]', '["shop"="estate_agent"]'],
    "real estate agency": ['["office"="estate_agent"]', '["shop"="estate_agent"]'],
    "lawyer": ['["office"="lawyer"]'],
    "law firm": ['["office"="lawyer"]'],
    "accountant": ['["office"="accountant"]'],
    "insurance": ['["office"="insurance"]'],
    "car repair": ['["shop"="car_repair"]'],
    "mechanic": ['["shop"="car_repair"]'],
    "car dealer": ['["shop"="car"]'],
    "car wash": ['["amenity"="car_wash"]'],
    "driving school": ['["amenity"="driving_school"]'],
    "school": ['["amenity"="school"]'],
    "kindergarten": ['["amenity"="kindergarten"]'],
    "language school": ['["amenity"="language_school"]'],
    "florist": ['["shop"="florist"]'],
    "clothing store": ['["shop"="clothes"]'],
    "boutique": ['["shop"="boutique"]', '["shop"="clothes"]'],
    "jewelry": ['["shop"="jewelry"]'],
    "optician": ['["shop"="optician"]'],
    "furniture store": ['["shop"="furniture"]'],
    "electronics store": ['["shop"="electronics"]'],
    "supermarket": ['["shop"="supermarket"]'],
    "pet store": ['["shop"="pet"]'],
    "plumber": ['["craft"="plumber"]'],
    "electrician": ['["craft"="electrician"]'],
    "carpenter": ['["craft"="carpenter"]'],
    "photographer": ['["craft"="photographer"]', '["shop"="photo"]'],
    "travel agency": ['["shop"="travel_agency"]', '["office"="travel_agent"]'],
    "coworking": ['["amenity"="coworking_space"]', '["office"="coworking"]'],
    "marketing agency": ['["office"="advertising_agency"]'],
    "it company": ['["office"="it"]'],
    "software company": ['["office"="it"]', '["office"="software"]'],
    "architect": ['["office"="architect"]'],
    "tattoo": ['["shop"="tattoo"]'],
    "laundry": ['["shop"="laundry"]', '["shop"="dry_cleaning"]'],
}

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

BUSINESS_KEYS ="^(shop|amenity|office|craft|healthcare|leisure|tourism)$"


def _filters_for(target: str) -> list[str]:
    t = target.strip().lower()
    if t in TAG_MAP:
        return TAG_MAP[t]
    if t.endswith("s") and t[:-1] in TAG_MAP:
        return TAG_MAP[t[:-1]]
    safe = re.sub(r"[^\w\s-]", "", t)
    value = safe.replace(" ", "_")
    return [
        f'[~"{BUSINESS_KEYS}"~"^{value}$",i]',
        f'["name"~"{safe}",i][~"{BUSINESS_KEYS}"~"."]',
    ]


async def geocode(location: str) -> dict | None:
    async with http_client() as client:
        for attempt in range(3):
            try:
                r = await client.get(
                    f"{settings.nominatim_url}/search",
                    params={"q": location, "format": "jsonv2", "limit": 1, "addressdetails": 1},
                )
                r.raise_for_status()
                data = r.json()
                return data[0] if data else None
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
    return None


def _bbox_clause(place: dict) -> tuple[str, str]:
    s, n, w, e = place["boundingbox"]
    return "", f"({s},{w},{n},{e})"


def _area_clause(place: dict) -> tuple[str, str]:
    """Return (preamble, filter-suffix) restricting the query to the place."""
    if place.get("osm_type") == "relation":
        return f"area(id:{3600000000 + int(place['osm_id'])})->.a;", "(area.a)"
    if place.get("osm_type") == "way":
        return f"area(id:{2400000000 + int(place['osm_id'])})->.a;", "(area.a)"
    s, n, w, e = place["boundingbox"]
    return "", f"({s},{w},{n},{e})"


def _address(tags: dict) -> str:
    parts = [
        " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])),
        tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village"),
        tags.get("addr:postcode"),
        tags.get("addr:country"),
    ]
    return ", ".join(p for p in parts if p) or tags.get("addr:full", "")


def overpass_endpoints() -> list[str]:
    """Configured server first, then public mirrors (they are often overloaded)."""
    return list(dict.fromkeys([settings.overpass_url, *OVERPASS_MIRRORS]))


async def run_overpass(query: str, log=print, rounds: int = 3) -> list[dict]:
    """POST a query, rotating through mirrors with backoff until one answers."""
    last_error = ""
    async with http_client() as client:
        for attempt in range(rounds):
            for url in overpass_endpoints():
                try:
                    r = await client.post(url, data={"data": query}, timeout=120)
                    if r.status_code == 200:
                        data = r.json()
                        # Overpass reports query timeouts as a 200 with a "remark".
                        if "elements" in data and not (data.get("remark") or "").startswith("runtime error"):
                            return data["elements"]
                        last_error = data.get("remark", "bad response")
                    else:
                        last_error = f"HTTP {r.status_code}"
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = f"{type(exc).__name__} {exc}".strip()
                log(f"OSM: {url.split('/')[2]} failed ({last_error}), trying next server")
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError(f"all Overpass servers failed: {last_error}")


async def search(target: str, location: str, limit: int = 40, log=print) -> list[dict]:
    place = await geocode(location)
    if not place:
        log(f"OSM: could not geocode '{location}'")
        return []
    def build(clause: tuple[str, str]) -> str:
        preamble, area = clause
        body = "".join(f"nwr{f}{area};" for f in _filters_for(target))
        return f"[out:json][timeout:90];{preamble}({body});out center tags {max(limit * 3, 50)};"

    area_clause = _area_clause(place)
    elements = await run_overpass(build(area_clause), log)
    if not elements and area_clause[0]:
        # Some mirrors have no area index and silently return nothing; retry with the bounding box.
        elements = await run_overpass(build(_bbox_clause(place)), log)

    addr = place.get("address", {})
    city = addr.get("city") or addr.get("town") or addr.get("village") or location.split(",")[0].strip()
    country = (addr.get("country_code") or "").upper()

    leads = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        phones = [p.strip() for key in ("phone", "contact:phone", "contact:mobile")
                  for p in re.split(r"[;,]", tags.get(key, "")) if p.strip()]
        emails = [e.strip().lower() for key in ("email", "contact:email")
                  for e in re.split(r"[;,]", tags.get(key, "")) if e.strip()]
        socials = {k: tags[f"contact:{k}"] for k in ("facebook", "instagram", "linkedin", "twitter")
                   if tags.get(f"contact:{k}")}
        center = el.get("center") or {}
        leads.append({
            "name": name,
            "category": target,
            "website": tags.get("website") or tags.get("contact:website") or tags.get("url") or "",
            "emails": emails,
            "phones": phones,
            "socials": socials,
            "address": _address(tags),
            "city": tags.get("addr:city") or city,
            "country": country,
            "lat": el.get("lat") or center.get("lat"),
            "lon": el.get("lon") or center.get("lon"),
            "source": "osm",
        })
    # Prefer entries that already carry contact data.
    leads.sort(key=lambda l: (not l["website"], not l["phones"], not l["emails"]))
    return leads[:limit]
