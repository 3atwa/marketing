"""Web metasearch via the open-source `ddgs` library (no API key).

Finds business websites for "<target> in <location>". Individual search
engines often rate-limit or return nothing, so several backends are tried in
turn. Directory/listing sites (Yelp, Yellow Pages, WhatClinic, ...) are
skipped because they are not the business itself; the enricher then visits
each website to pull emails, phones and socials.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from ..utils import domain_of, is_directory, slug

# Engines that answered reliably in testing come first.
BACKENDS = ["duckduckgo", "bing", "yandex", "startpage", "brave", "google", "mojeek"]

# Words in result titles that signal a listicle/directory rather than a business.
LISTICLE = re.compile(
    r"\b(top\s+\d+|the\s+\d*\s*best|\d+\+?\s+best|best\s+[\w ]{0,30}\s+(?:in|near)\b|list of|near me|directory|"
    r"reviews? of|find a|compare|\d+\s+\w+s\s+in)\b|^\w+(?:\s+\w+)?s\s+(?:in|near)\s",
    re.I,
)
# Domains that host listings of many businesses (heuristic, in addition to utils.DIRECTORY_DOMAINS).
DIRECTORY_HINTS = re.compile(
    r"yellow|pages|directory|listing|whatclinic|clinicbooking|starofservice|cylex|hotfrog|infobel|kompass|"
    r"tupalo|find-?us-?here|brownbook|2findlocal|n49|yably|zaubee|top-?rated|bestof|threebestrated|"
    r"justdial|sulekha|dubizzle|olx|gumtree|craigslist|tripadvisor|yelp|foursquare|wanderlog|"
    r"healthengine|doctoranytime|practo|dentaly|whatsclinic|bookimed|medigo|placidway|dentaldepartures",
    re.I,
)


GENERIC_TITLES = re.compile(
    r"^(home(\s*page)?|welcome|contact(\s*us)?|about(\s*us)?|services|index|main|startseite|accueil|inicio|"
    r"book (now|online)|appointments?|our team|team|location|locations|faq)$",
    re.I,
)


def clean_title(title: str) -> str:
    """'Home | St Pauls Dental & Implantology' -> 'St Pauls Dental & Implantology'."""
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:·•»]\s+|\s*[|•»]\s*|:\s+", title or "") if p.strip()]
    for part in parts:
        part = part.strip(" |-–—:·•»")
        if not GENERIC_TITLES.match(part) and len(part) > 2:
            return part[:120]
    return ""


def name_from_domain(domain: str) -> str:
    return domain.split(".")[0].replace("-", " ").title()


def looks_like_listing(url: str, title: str, target: str) -> bool:
    if is_directory(url) or DIRECTORY_HINTS.search(domain_of(url)) or LISTICLE.search(title or ""):
        return True
    # "salonsindubai.ae", "dentists-in-austin.com": sites about many businesses of this type
    word = slug(target).split("-")[-1]
    compact = domain_of(url).replace("-", "")
    if word and re.search(rf"{re.escape(word)}s?(in|near|of)[a-z]", compact):
        return True
    # Business sites show up as their homepage or a page one level deep
    # (/contact, /new-clinic). Aggregators show deep listing pages such as
    # /dentists/malta/valletta/clinic-name or /dir/... .
    path = urlparse(url).path.lower()
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 3:
        return True
    t = slug(target).rstrip("s")
    if re.search(r"/(dir|directory|search|category|categories|listings?|places|biz|business|companies)/", path + "/"):
        return True
    return len(segments) == 2 and bool(t) and any(seg.startswith(t) for seg in segments)


def _run(query: str, limit: int, backend: str) -> list[dict]:
    from ddgs import DDGS  # imported lazily: heavy import

    return list(DDGS(timeout=15).text(query, max_results=limit, backend=backend) or [])


async def search(target: str, location: str, limit: int = 40, log=print) -> list[dict]:
    queries = [f"{target} in {location}", f"{target} {location}", f"{target} near {location} official website"]
    seen: set[str] = set()
    leads: list[dict] = []
    working = 0
    for q in queries:
        for backend in BACKENDS:
            try:
                results = await asyncio.to_thread(_run, q, max(limit, 20), backend)
            except Exception as exc:  # ddgs raises on rate limits / no results
                log(f"Web search [{backend}] '{q}': {str(exc)[:80]}")
                continue
            working += 1
            for r in results:
                url = r.get("href") or r.get("url") or ""
                title = r.get("title") or ""
                d = domain_of(url)
                if not url.startswith("http") or not d or d in seen or looks_like_listing(url, title, target):
                    continue
                seen.add(d)
                leads.append({
                    "name": clean_title(title) or name_from_domain(d),
                    "category": target,
                    "website": f"{urlparse(url).scheme}://{urlparse(url).netloc}",
                    "city": location.split(",")[0].strip(),
                    "source": "web",
                })
                if len(leads) >= limit:
                    return leads
            if working >= 2:  # two engines per query is plenty; move on to the next query
                working = 0
                break
    return leads
