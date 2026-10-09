"""Lead discovery sources.

Every source exposes ``async def search(target, location, limit, log) -> list[dict]``
returning raw leads with any of: name, category, website, emails, phones,
socials, address, city, country, lat, lon, rating, review_count, source.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from ..config import settings
from . import gmaps_scraper, google_places, osm, websearch

Search = Callable[..., Awaitable[list[dict]]]

SOURCES: dict[str, dict] = {
    "osm": {
        "label": "OpenStreetMap (free, no key)",
        "search": osm.search,
        "available": lambda: True,
    },
    "web": {
        "label": "Web search (DuckDuckGo/Bing/Brave via ddgs)",
        "search": websearch.search,
        "available": lambda: True,
    },
    "google_places": {
        "label": "Google Places API (needs GOOGLE_PLACES_API_KEY)",
        "search": google_places.search,
        "available": lambda: bool(settings.google_places_api_key),
    },
    "gmaps_scraper": {
        "label": "Google Maps via gosom/google-maps-scraper (needs GMAPS_SCRAPER_URL)",
        "search": gmaps_scraper.search,
        "available": lambda: bool(settings.gmaps_scraper_url),
    },
}


def available_sources() -> list[tuple[str, str, bool]]:
    return [(key, s["label"], s["available"]()) for key, s in SOURCES.items()]
