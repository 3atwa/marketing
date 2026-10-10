"""Website audit + tech-stack detection.

Runs on the pages the crawler already downloaded (no extra requests) and
produces:
  * tech:  ["WordPress", "Google Analytics", "Calendly", ...]
  * audit: {"score": 0-100, "checks": [{"key", "label", "ok", "detail", "tip"}], "load_ms", "page_kb"}
The audit powers the shareable "free website check" report page, which makes
a much stronger cold-email hook than a generic pitch.
"""
from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

# name -> regex over the raw homepage HTML (and headers for a few)
TECH_PATTERNS: dict[str, str] = {
    # site builders / CMS
    "WordPress": r"wp-content|wp-includes|wp-json",
    "Wix": r"wix\.com|_wixCssImports|wixstatic\.com",
    "Squarespace": r"squarespace\.com|static1\.squarespace",
    "Shopify": r"cdn\.shopify\.com|myshopify\.com|Shopify\.theme",
    "Webflow": r"webflow\.(com|io)|data-wf-page",
    "Weebly": r"weebly\.com|editmysite\.com",
    "GoDaddy Website Builder": r"img1\.wsimg\.com|godaddy\.com/websites",
    "Joomla": r"/media/jui/|joomla",
    "Drupal": r"drupal-settings-json|/sites/default/files",
    "WooCommerce": r"woocommerce",
    "Elementor": r"elementor",
    "Next.js": r"__NEXT_DATA__|/_next/static",
    "React": r"data-reactroot|react-dom",
    "Framer": r"framerusercontent\.com|framer\.com",
    # marketing / analytics
    "Google Analytics": r"google-analytics\.com|gtag\(|googletagmanager\.com/gtag",
    "Google Tag Manager": r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]+",
    "Meta Pixel": r"connect\.facebook\.net/.+/fbevents\.js|fbq\(",
    "TikTok Pixel": r"analytics\.tiktok\.com",
    "Hotjar": r"hotjar\.com",
    "Mailchimp": r"mailchimp\.com|list-manage\.com",
    "HubSpot": r"hs-scripts\.com|hubspot\.com",
    # booking / ordering
    "Calendly": r"calendly\.com",
    "Fresha": r"fresha\.com",
    "Booksy": r"booksy\.com",
    "Setmore": r"setmore\.com",
    "SimplyBook.me": r"simplybook\.(me|it)",
    "Acuity Scheduling": r"acuityscheduling\.com",
    "Square": r"squareup\.com|square\.site",
    "Mindbody": r"mindbodyonline\.com|healcode",
    "OpenTable": r"opentable\.",
    "Zocdoc": r"zocdoc\.com",
    "Doctolib": r"doctolib\.",
    "Toast": r"toasttab\.com",
    "Deliveroo/Talabat/UberEats": r"deliveroo\.|talabat\.com|ubereats\.com",
    # chat / messaging
    "WhatsApp chat button": r"wa\.me/|api\.whatsapp\.com",
    "Intercom": r"intercom\.io|widget\.intercom",
    "Tawk.to": r"tawk\.to",
    "Tidio": r"tidio\.co",
    "Crisp": r"crisp\.chat",
    "Zendesk": r"zdassets\.com|zendesk\.com",
    # payments
    "Stripe": r"js\.stripe\.com",
    "PayPal": r"paypal\.com/sdk|paypalobjects",
}

BOOKING_TECH = {"Calendly", "Fresha", "Booksy", "Setmore", "SimplyBook.me", "Acuity Scheduling", "Square",
                "Mindbody", "OpenTable", "Zocdoc", "Doctolib", "Toast", "Deliveroo/Talabat/UberEats"}
CHAT_TECH = {"WhatsApp chat button", "Intercom", "Tawk.to", "Tidio", "Crisp", "Zendesk"}
ANALYTICS_TECH = {"Google Analytics", "Google Tag Manager", "Meta Pixel", "TikTok Pixel", "Hotjar"}
BOOKING_WORDS = re.compile(r"book\s+(?:now|online|an?\s+appointment)|online\s+booking|reserve\s+(?:now|a\s+table)|order\s+online",
                           re.I)


def detect_tech(html: str) -> list[str]:
    return [name for name, pat in TECH_PATTERNS.items() if re.search(pat, html, re.I)]


def _check(key: str, label: str, ok: bool, detail: str = "", tip: str = "") -> dict:
    return {"key": key, "label": label, "ok": bool(ok), "detail": detail, "tip": "" if ok else tip}


PARKED = re.compile(
    r"parking-lander|ap:\s*\"parking\"|sedoparking|parkingcrew|bodis\.com|above\.com/marketing|dan\.com|afternic|"
    r"hugedomains|this domain (?:is|may be) for sale|buy this domain|domain (?:is )?parked|parked free|"
    r"related searches|domain has expired",
    re.I,
)


def audit_site(pages: dict[str, str], final_url: str, load_ms: int, socials: dict, tech: list[str]) -> dict:
    home_html = next(iter(pages.values()), "")
    all_html = "\n".join(pages.values())
    soup = BeautifulSoup(home_html, "lxml")
    if PARKED.search(home_html):
        return {"parked": True, "score": 0, "checks": [], "load_ms": load_ms, "page_kb": 0, "url": final_url}
    for t in soup(["script", "style", "noscript"]):
        t.extract()
    visible = soup.get_text(" ", strip=True)
    if len(visible) < 80:
        # JavaScript-only app or an empty page: grading it would produce false "fails".
        soup = BeautifulSoup(home_html, "lxml")
        return {"incomplete": True, "score": None, "checks": [], "load_ms": load_ms,
                "page_kb": round(len(home_html.encode()) / 1024), "url": final_url,
                "note": "The homepage is built with JavaScript or is nearly empty, so it couldn't be analysed automatically."}
    soup = BeautifulSoup(home_html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = (desc_tag.get("content") or "").strip() if desc_tag else ""
    page_kb = round(len(home_html.encode()) / 1024)
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2}|19\d{2})", all_html, re.I)]
    this_year = datetime.now().year
    has_booking = bool(set(tech) & BOOKING_TECH) or bool(BOOKING_WORDS.search(all_html))

    checks = [
        _check("https", "Secure connection (HTTPS)", final_url.startswith("https://"),
               "Your site loads over HTTPS." if final_url.startswith("https://") else "Browsers show 'Not secure' on your site.",
               "Install an SSL certificate so visitors don't see a security warning."),
        _check("mobile", "Mobile-friendly layout", bool(soup.find("meta", attrs={"name": "viewport"})),
               "", "Most customers visit on a phone; the site doesn't adapt to small screens."),
        _check("speed", "Fast loading", load_ms < 4000, f"Homepage answered in {load_ms / 1000:.1f}s ({page_kb} KB).",
               "Slow pages lose visitors; compress images and use caching."),
        _check("booking", "Online booking / ordering", has_booking,
               "Customers can book or order online." if has_booking else "No way to book or order online was found.",
               "Let customers book 24/7 instead of calling during opening hours."),
        _check("chat", "Instant messaging (WhatsApp / live chat)", bool(set(tech) & CHAT_TECH),
               "", "A WhatsApp or chat button turns visitors into conversations."),
        _check("analytics", "Visitor analytics", bool(set(tech) & ANALYTICS_TECH),
               ", ".join(sorted(set(tech) & ANALYTICS_TECH)), "Without analytics you can't see where customers come from."),
        _check("seo_title", "Search-friendly page title", 10 <= len(title) <= 70, title[:80],
               "Give the homepage a clear title with your business name and city."),
        _check("seo_desc", "Search description", 50 <= len(desc) <= 170, desc[:160],
               "Add a meta description so Google shows a good summary."),
        _check("h1", "Clear main heading", bool(soup.find("h1")), "", "Add one clear headline saying what you do."),
        _check("social", "Social media linked", len(socials) >= 1, ", ".join(sorted(socials)),
               "Link your Instagram/Facebook so visitors can follow you."),
        _check("fresh", "Up-to-date content", not years or max(years) >= this_year - 1,
               f"Footer says © {max(years)}." if years else "",
               "An old copyright year makes the site look abandoned."),
        _check("schema", "Business info for Google (structured data)",
               "application/ld+json" in home_html, "", "Add LocalBusiness structured data for richer Google results."),
    ]
    passed = sum(c["ok"] for c in checks)
    return {"score": round(100 * passed / len(checks)), "checks": checks, "load_ms": load_ms, "page_kb": page_kb,
            "url": final_url}
