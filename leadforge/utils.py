"""Small helpers shared by sources and the enricher."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

import httpx

from .config import settings

# Sites that list businesses but are not the business itself. We never treat
# them as a lead's own website.
DIRECTORY_DOMAINS = {
    "yelp.com", "yellowpages.com", "yell.com", "tripadvisor.com", "facebook.com", "instagram.com",
    "linkedin.com", "twitter.com", "x.com", "tiktok.com", "youtube.com", "pinterest.com",
    "google.com", "maps.google.com", "goo.gl", "bing.com", "foursquare.com", "bbb.org",
    "angi.com", "angieslist.com", "thumbtack.com", "houzz.com", "groupon.com", "opentable.com",
    "zocdoc.com", "healthgrades.com", "booking.com", "expedia.com", "wikipedia.org", "reddit.com",
    "mapquest.com", "manta.com", "chamberofcommerce.com", "nextdoor.com", "fresha.com", "booksy.com",
    "treatwell.com", "doctolib.fr", "jameda.de", "pagesjaunes.fr", "gelbeseiten.de", "paginegialle.it",
    "paginasamarillas.es", "trustpilot.com", "glassdoor.com", "indeed.com", "clutch.co", "upwork.com",
    "fiverr.com", "amazon.com", "ebay.com", "apple.com", "medium.com", "wix.com", "squarespace.com",
}


def http_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent, "Accept-Language": "en;q=0.9,*;q=0.5"},
        timeout=settings.http_timeout,
        follow_redirects=True,
        **kwargs,
    )


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    return url


def domain_of(url: str) -> str:
    try:
        host = urlparse(normalize_url(url)).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def is_directory(url: str) -> bool:
    d = domain_of(url)
    return any(d == x or d.endswith("." + x) for x in DIRECTORY_DOMAINS)


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def dedupe_key(name: str, website: str = "", address: str = "") -> str:
    """Same website => same business. Otherwise fall back to name + street."""
    if website and not is_directory(website):
        d = domain_of(website)
        if d:
            return "d:" + d
    street = slug(address)[:40]
    return "n:" + slug(name) + ("|" + street if street else "")


def pluralize(word: str) -> str:
    w = (word or "businesses").strip().lower()
    if not w:
        return "businesses"
    if w.endswith(("s", "x", "ch", "sh")):
        return w + "es" if not w.endswith("s") else w
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    return w + "s"
