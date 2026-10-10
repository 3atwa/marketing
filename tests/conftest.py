import pytest


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Tests never hit DNS-over-HTTPS; every domain counts as accepting mail."""
    from leadforge import enrich

    async def fake_status(email):
        return "invalid" if email.endswith("@dead.invalid") else "valid"

    monkeypatch.setattr(enrich, "email_status", fake_status)
