"""Visit a business website and pull out contact details + sales signals.

Crawls the homepage plus a handful of likely contact pages (contact, about,
impressum, ...), honours robots.txt, and extracts:
  * emails   - mailto links, plain text, light de-obfuscation ("name [at] site [dot] com")
  * phones   - tel: links, schema.org JSON-LD, and numbers found in page text
  * socials  - Facebook, Instagram, LinkedIn, X/Twitter, TikTok, YouTube, WhatsApp
  * address  - from JSON-LD when present
  * signals  - hints that the business could use your app (no online booking, no mobile layout, ...)
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import phonenumbers
from bs4 import BeautifulSoup

from .config import settings
from .utils import domain_of, http_client, normalize_url

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(\{]\s*(?:at|@)\s*[\]\)\}]\s*([A-Za-z0-9\-]+(?:\s*[\[\(\{]\s*(?:dot|\.)\s*[\]\)\}]\s*[A-Za-z0-9\-]+)+)",
    re.I,
)
BAD_EMAIL_PARTS = (
    "example.", "domain.com", "yourdomain", "email.com", "sentry", "wixpress", "@2x", ".png", ".jpg",
    ".jpeg", ".gif", ".svg", ".webp", "godaddy", "localhost", "u003e", "noreply", "no-reply",
    # placeholder addresses left in website themes
    "@site.com", "@website.com", "@yoursite", "@mysite.", "@company.com", "@yourcompany", "@domain.", "@email.",
    "@test.", "johndoe", "janedoe", "username@", "your@", "youremail", "yourname@",
)
# Inboxes that won't reach a decision maker; kept, but ranked last.
BACKOFFICE_INBOXES = {
    "privacy", "dataprotection", "gdpr", "dpo", "legal", "careers", "jobs", "recruitment", "hr", "abuse",
    "webmaster", "postmaster", "accounts", "billing", "invoices", "press", "media", "complaints", "unsubscribe",
}
CONTACT_HINTS = re.compile(
    r"contact|kontakt|contacto|contato|contatti|about|impressum|imprint|legal|team|location|"
    r"visit|reach|get-in-touch|nous-joindre|a-propos|ueber-uns|uber-uns|chi-siamo|quienes-somos|"
    r"اتصل",  # Arabic "ittasil"
    re.I,
)
SOCIAL_PATTERNS = {
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\?)[^\s\"'<>?#]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/(?!p/|explore/)[A-Za-z0-9_.]+", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/[^\s\"'<>?#]+", re.I),
    "twitter": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share|home)[A-Za-z0-9_]{1,15}\b", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9_.]+", re.I),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|user/|@)[^\s\"'<>?#]+", re.I),
    "whatsapp": re.compile(r"https?://(?:wa\.me|api\.whatsapp\.com/send)[^\s\"'<>]*", re.I),
}
BOOKING_HINTS = re.compile(
    r"calendly|acuityscheduling|booksy|fresha|setmore|simplybook|square\.site/book|squareup\.com/appointments|"
    r"opentable|resy\.com|zocdoc|doctolib|vagaro|mindbody|schedulicity|treatwell|book\s+(?:now|online|an?\s+appointment)|"
    r"online\s+booking|reserve\s+(?:now|a\s+table)",
    re.I,
)
# "Dentist in Austin, TX" is an SEO title, not a business name.
SEO_TITLE = re.compile(r"\b(in|near)\s+[A-Z]|^(best|top)\b", re.I)
APP_HINTS = re.compile(r"apps\.apple\.com|play\.google\.com/store/apps", re.I)
CHAT_HINTS = re.compile(r"intercom|tawk\.to|crisp\.chat|drift\.com|livechat|zendesk|hubspot.*chat|tidio", re.I)
ECOMMERCE_HINTS = re.compile(r"shopify|woocommerce|add[\s-]to[\s-]cart|checkout", re.I)


def _valid_email(e: str) -> bool:
    e = e.lower()
    if any(b in e for b in BAD_EMAIL_PARTS):
        return False
    local, _, dom = e.partition("@")
    return 0 < len(local) <= 64 and "." in dom and not dom.startswith(".")


def extract_emails(html: str, text: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r'mailto:([^"\'?<>\s]+)', html, re.I):
        found.append(unquote(m.group(1)))
    found += EMAIL_RE.findall(text)
    for user, dom in OBFUSCATED_RE.findall(text):
        dom = re.sub(r"\s*[\[\(\{]\s*(?:dot|\.)\s*[\]\)\}]\s*", ".", dom, flags=re.I)
        found.append(f"{user}@{dom}")
    out = []
    for e in found:
        e = e.strip(" .,;:").lower()
        if EMAIL_RE.fullmatch(e) and _valid_email(e):
            out.append(e)
    return list(dict.fromkeys(out))


def extract_phones(text: str, tel_links: list[str], region: str | None) -> list[str]:
    region = (region or "").upper() or None
    out: list[str] = []

    def add(raw: str) -> None:
        try:
            num = phonenumbers.parse(raw, region)
        except phonenumbers.NumberParseException:
            return
        if phonenumbers.is_valid_number(num):
            out.append(phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL))

    for t in tel_links:
        add(re.sub(r"^tel:", "", t, flags=re.I).strip())
    if region:
        for match in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
            out.append(phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
    else:
        # Without a country we only trust numbers written in international form.
        for raw in re.findall(r"\+\d[\d\s().\-]{7,18}\d", text):
            add(raw)
    return list(dict.fromkeys(out))[:5]


def normalize_phones(phones: list[str], region: str | None) -> list[str]:
    """Format numbers internationally (+971 4 320 3855) so tel:/WhatsApp links work."""
    out = []
    for raw in phones:
        try:
            num = phonenumbers.parse(raw, (region or "").upper() or None)
            if phonenumbers.is_possible_number(num):
                out.append(phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
                continue
        except phonenumbers.NumberParseException:
            pass
        out.append(raw.strip())
    return list(dict.fromkeys(out))


def extract_socials(html: str) -> dict[str, str]:
    socials = {}
    for name, pat in SOCIAL_PATTERNS.items():
        m = pat.search(html)
        if m:
            socials[name] = m.group(0).rstrip("/\\")
    return socials


def extract_jsonld(soup: BeautifulSoup) -> dict:
    """Pull telephone/email/address from schema.org LocalBusiness-like blocks."""
    info: dict = {"phones": [], "emails": [], "address": ""}

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            if node.get("telephone"):
                info["phones"].append(str(node["telephone"]))
            if node.get("email"):
                info["emails"].append(str(node["email"]).replace("mailto:", ""))
            addr = node.get("address")
            if isinstance(addr, dict) and not info["address"]:
                info["address"] = ", ".join(str(addr[k]) for k in (
                    "streetAddress", "addressLocality", "postalCode", "addressCountry") if addr.get(k) and isinstance(addr.get(k), str))
            elif isinstance(addr, str) and not info["address"]:
                info["address"] = addr
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return info


def detect_signals(pages: dict[str, str], final_url: str) -> list[str]:
    """Things a sales pitch for an app/automation can reference."""
    html = "\n".join(pages.values())
    signals = []
    if final_url.startswith("http://"):
        signals.append("No HTTPS on website")
    home = next(iter(pages.values()), "")
    if home and 'name="viewport"' not in home.lower() and "name='viewport'" not in home.lower():
        signals.append("Website not mobile-friendly")
    if not BOOKING_HINTS.search(html):
        signals.append("No online booking/ordering found")
    if not APP_HINTS.search(html):
        signals.append("No mobile app linked")
    if not CHAT_HINTS.search(html):
        signals.append("No live chat")
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2}|19\d{2})", html, re.I)]
    if years and max(years) < datetime.now().year - 2:
        signals.append(f"Website looks outdated (© {max(years)})")
    if ECOMMERCE_HINTS.search(html):
        signals.append("Sells online")
    return signals


def site_name(soup: BeautifulSoup) -> str:
    """Business name from og:site_name, schema.org name, or the page title."""
    from .sources.websearch import clean_title

    og = soup.find("meta", property="og:site_name")
    if og and og.get("content", "").strip(" |-–—:·•»"):
        return og["content"].strip(" |-–—:·•»")[:120]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return clean_title(title)


class Crawler:
    def __init__(self) -> None:
        self._robots: dict[str, RobotFileParser | None] = {}

    async def _allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        if not settings.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                r = await client.get(origin + "/robots.txt", timeout=8)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt => everything allowed
            except httpx.HTTPError:
                rp = None
            self._robots[origin] = rp
        rp = self._robots[origin]
        return rp is None or rp.can_fetch(settings.user_agent, url)

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response | None:
        if not await self._allowed(client, url):
            return None
        try:
            r = await client.get(url)
        except httpx.HTTPError:
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" not in ctype:
            return None
        return r

    async def crawl(self, website: str, region: str | None = None) -> dict:
        """Return {emails, phones, socials, address, signals, error}."""
        result = {"emails": [], "phones": [], "socials": {}, "address": "", "signals": [], "site_name": "", "error": ""}
        url = normalize_url(website)
        if not url:
            result["error"] = "no website"
            return result
        async with http_client() as client:
            home = await self._get(client, url)
            if home is None and url.startswith("http://"):
                home = await self._get(client, "https://" + url[len("http://"):])
            if home is None:
                result["error"] = "website unreachable or blocked by robots.txt"
                return result
            final_url = str(home.url)
            site = domain_of(final_url)
            pages: dict[str, str] = {final_url: home.text}

            soup = BeautifulSoup(home.text, "lxml")
            result["site_name"] = site_name(soup)
            candidates = []
            for a in soup.find_all("a", href=True):
                href = urljoin(final_url, a["href"])
                if domain_of(href) != site or href in pages:
                    continue
                label = f"{a['href']} {a.get_text(' ', strip=True)}"
                if CONTACT_HINTS.search(label):
                    candidates.append(href.split("#")[0])
            for href in list(dict.fromkeys(candidates))[: settings.crawl_max_pages - 1]:
                r = await self._get(client, href)
                if r is not None:
                    pages[href] = r.text

        emails, phones, tel_links, texts = [], [], [], []
        address = ""
        for html in pages.values():
            s = BeautifulSoup(html, "lxml")
            ld = extract_jsonld(s)
            emails += ld["emails"]
            phones += ld["phones"]
            address = address or ld["address"]
            tel_links += [a["href"] for a in s.find_all("a", href=re.compile(r"^tel:", re.I))]
            for t in s(["script", "style", "noscript"]):
                t.decompose()
            text = s.get_text(" ", strip=True)
            texts.append(text)
            emails += extract_emails(html, text)

        all_text = "\n".join(texts)
        # Prefer emails on the business's own domain, then the rest.
        uniq = list(dict.fromkeys(e.lower() for e in emails if _valid_email(e)))
        uniq.sort(key=lambda e: (e.split("@")[0] in BACKOFFICE_INBOXES,
                                 not e.endswith("@" + site) and not e.endswith("." + site)))
        result["emails"] = uniq[:5]
        result["phones"] = extract_phones(all_text, tel_links + phones, region)
        result["socials"] = extract_socials("\n".join(pages.values()))
        result["address"] = address
        result["signals"] = detect_signals(pages, final_url)
        return result


def score_lead(lead: dict) -> int:
    """0-100: how reachable (and how promising) a lead is."""
    score = 0
    emails = lead.get("emails") or []
    site = domain_of(lead.get("website") or "")
    if emails:
        score += 35
        if site and any(e.endswith("@" + site) for e in emails):
            score += 10
    if lead.get("phones"):
        score += 20
    if lead.get("website"):
        score += 10
    score += min(10, 4 * len(lead.get("socials") or {}))
    if lead.get("address"):
        score += 5
    signals = lead.get("signals") or []
    if "No online booking/ordering found" in signals:
        score += 5
    if any(s.startswith(("Website not mobile", "Website looks outdated", "No HTTPS")) for s in signals):
        score += 5
    return min(score, 100)


async def enrich_many(leads: list[dict], log=print) -> list[dict]:
    """Crawl websites concurrently. Mutates and returns the given lead dicts."""
    crawler = Crawler()
    sem = asyncio.Semaphore(settings.crawl_concurrency)
    done = 0

    async def one(lead: dict) -> None:
        nonlocal done
        if lead.get("website"):
            async with sem:
                try:
                    found = await asyncio.wait_for(crawler.crawl(lead["website"], lead.get("country")), 90)
                except Exception as exc:  # never let one site kill the batch
                    found = {"emails": [], "phones": [], "socials": {}, "address": "", "signals": [], "site_name": "",
                             "error": str(exc)}
            lead["emails"] = list(dict.fromkeys([*(lead.get("emails") or []), *found["emails"]]))
            lead["phones"] = list(dict.fromkeys([*(lead.get("phones") or []), *found["phones"]]))
            lead["socials"] = {**found["socials"], **(lead.get("socials") or {})}
            lead["signals"] = found["signals"]
            lead["address"] = lead.get("address") or found["address"]
            if found["site_name"] and (not lead.get("name") or (
                    lead.get("source") == "web" and not SEO_TITLE.search(found["site_name"]))):
                lead["name"] = found["site_name"]  # a search-result title is a weaker name than the site's own
        else:
            lead["signals"] = ["No website"]
        lead["phones"] = normalize_phones(lead.get("phones") or [], lead.get("country"))
        lead["score"] = score_lead(lead)
        done += 1
        if done % 10 == 0 or done == len(leads):
            log(f"Enriched {done}/{len(leads)} leads")

    await asyncio.gather(*(one(l) for l in leads))
    return leads
