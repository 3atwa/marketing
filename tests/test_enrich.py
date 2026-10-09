from bs4 import BeautifulSoup

from leadforge.enrich import detect_signals, extract_emails, extract_jsonld, extract_phones, extract_socials, score_lead
from leadforge.utils import dedupe_key, domain_of, is_directory, pluralize

HTML = """
<html><head><meta name="viewport" content="width=device-width">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Dentist","name":"Smile Co","telephone":"+1 512-555-0134",
 "email":"mailto:hello@smileco.com",
 "address":{"@type":"PostalAddress","streetAddress":"12 Main St","addressLocality":"Austin","postalCode":"78701"}}
</script></head>
<body>
 <a href="mailto:info@smileco.com?subject=hi">Email us</a>
 <p>Bookings: frontdesk [at] smileco [dot] com. Logo: logo@2x.png</p>
 <a href="tel:+15125550199">Call</a>
 <a href="https://www.facebook.com/smileco/">fb</a>
 <a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
 <a href="https://instagram.com/smile.co">ig</a>
 <a href="https://wa.me/15125550199">wa</a>
 <footer>&copy; 2017 Smile Co</footer>
</body></html>
"""


def test_extract_emails_handles_mailto_text_and_obfuscation():
    soup = BeautifulSoup(HTML, "lxml")
    emails = extract_emails(HTML, soup.get_text(" "))
    assert "info@smileco.com" in emails
    assert "frontdesk@smileco.com" in emails
    assert not any("png" in e for e in emails)


def test_extract_jsonld():
    info = extract_jsonld(BeautifulSoup(HTML, "lxml"))
    assert info["phones"] == ["+1 512-555-0134"]
    assert info["emails"] == ["hello@smileco.com"]
    assert info["address"].startswith("12 Main St, Austin")


def test_extract_phones_tel_and_text():
    phones = extract_phones("Call us at (512) 555-0123 today", ["tel:+15125550199"], "US")
    assert "+1 512-555-0199" in phones
    assert "+1 512-555-0123" in phones
    # Without a region only international-format numbers are trusted
    assert extract_phones("call 555 0123", [], None) == []


def test_extract_socials_skips_share_links():
    s = extract_socials(HTML)
    assert s["facebook"] == "https://www.facebook.com/smileco"
    assert s["instagram"] == "https://instagram.com/smile.co"
    assert s["whatsapp"].startswith("https://wa.me/1512")


def test_signals():
    signals = detect_signals({"http://smileco.com": HTML}, "http://smileco.com")
    assert "No HTTPS on website" in signals
    assert "No online booking/ordering found" in signals
    assert any(s.startswith("Website looks outdated") for s in signals)
    assert "Website not mobile-friendly" not in signals


def test_score_prefers_domain_email():
    base = {"website": "https://smileco.com", "phones": ["+1"], "socials": {}, "signals": []}
    a = score_lead({**base, "emails": ["info@smileco.com"]})
    b = score_lead({**base, "emails": ["someone@gmail.com"]})
    assert a > b > score_lead({**base, "emails": []})


def test_utils():
    assert domain_of("https://www.Example.com/path") == "example.com"
    assert is_directory("https://m.facebook.com/x") and is_directory("https://www.yelp.com/biz/x")
    assert not is_directory("https://smileco.com")
    assert dedupe_key("Smile Co", "https://www.smileco.com/") == "d:smileco.com"
    assert dedupe_key("Smile Co", "https://facebook.com/smile", "12 Main St") == "n:smile-co|12-main-st"
    assert pluralize("dentist") == "dentists" and pluralize("agency") == "agencies"
