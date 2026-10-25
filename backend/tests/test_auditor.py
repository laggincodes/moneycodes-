"""
Automated tests for WebsiteAuditor: Existing sites, missing websites, unreachable URLs, and HTML heuristics.
"""
import pytest
from app.audit.website_auditor import WebsiteAuditor


@pytest.mark.asyncio
async def test_audit_missing_website():
    """Verify auditing a business with no listed website."""
    auditor = WebsiteAuditor()
    result = await auditor.audit(
        url=None,
        business_name="Green Leaf Cafe",
        category="Café",
    )
    
    assert result.has_website_url is False
    assert result.is_reachable is False
    assert result.status_code is None
    assert len(result.findings) >= 2
    
    # Must distinguish verified vs inferred
    verified = [f for f in result.findings if f.status == "verified"]
    inferred = [f for f in result.findings if f.status == "inferred"]
    assert len(verified) >= 1
    assert len(inferred) >= 1
    assert "No official website" in verified[0].summary


@pytest.mark.asyncio
async def test_audit_unreachable_website():
    """Verify auditing an invalid/unreachable domain."""
    auditor = WebsiteAuditor(timeout=4.0)
    result = await auditor.audit(
        url="https://this-domain-definitely-does-not-exist-123456789.com",
        business_name="Ghost Bakery",
        category="Bakery",
    )
    
    assert result.has_website_url is True
    assert result.is_reachable is False
    assert any("down or unreachable" in f.summary for f in result.findings)
    assert any(f.status == "verified" for f in result.findings)


def test_audit_html_outdated_site():
    """Verify heuristic detection of missing viewport, generic title, and missing CTAs."""
    auditor = WebsiteAuditor()
    outdated_html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Home</title>
        <!-- No viewport tag -->
      </head>
      <body>
        <h1>Welcome to our shop</h1>
        <p>We sell products. Call us sometime.</p>
      </body>
    </html>
    """
    result = auditor._analyze_html(
        raw_html=outdated_html,
        url="http://oldsite.com",
        final_url="http://oldsite.com",
        status_code=200,
        is_reachable=True,
        ssl_valid=False,
        redirect_chain=["http://oldsite.com"],
        response_time_ms=120,
        business_name="Old Shop",
        category="Retail Shop",
    )
    
    assert result.has_viewport_meta is False
    assert result.has_clear_cta is False
    assert result.ssl_valid is False
    assert result.mobile_friendly_indicator is False
    assert any("generic" in f.summary.lower() for f in result.findings)
    assert any("viewport" in f.summary.lower() for f in result.findings)


def test_audit_html_modern_responsive_site():
    """Verify heuristic detection of modern responsive restaurant with menu and CTA."""
    auditor = WebsiteAuditor()
    modern_html = """
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Artisan Crust Pizza - Downtown</title>
        <meta name="description" content="Fresh woodfired pizzas in downtown. Order online or reserve a table.">
      </head>
      <body>
        <nav><a href="/menu">Food Menu & Beverages</a></nav>
        <main>
          <h2>Delicious Pizza</h2>
          <a href="/reserve" class="btn">Reserve a Table</a>
          <a href="tel:+919876543210">Call: +91 98765 43210</a>
          <a href="https://instagram.com/artisancrust">Follow on Instagram</a>
        </main>
      </body>
    </html>
    """
    result = auditor._analyze_html(
        raw_html=modern_html,
        url="https://artisancrust.com",
        final_url="https://artisancrust.com",
        status_code=200,
        is_reachable=True,
        ssl_valid=True,
        redirect_chain=["https://artisancrust.com"],
        response_time_ms=85,
        business_name="Artisan Crust Pizza",
        category="Restaurant",
    )
    
    assert result.has_viewport_meta is True
    assert result.has_title is True
    assert result.has_menu is True
    assert result.has_clear_cta is True
    assert result.has_contact_phone is True
    assert result.has_social_links is True
    assert result.mobile_friendly_indicator is True
    assert result.ssl_valid is True


@pytest.mark.asyncio
async def test_audit_live_real_site():
    """Verify live audit against a known stable domain."""
    auditor = WebsiteAuditor(timeout=8.0)
    result = await auditor.audit(
        url="https://example.com",
        business_name="Example Corp",
        category="Office",
    )
    
    assert result.has_website_url is True
    assert result.is_reachable is True
    assert result.status_code == 200
    assert result.has_title is True
    assert result.has_viewport_meta is True
    assert result.ssl_valid is True
    print(f"example.com response time: {result.response_time_ms}ms, findings count: {len(result.findings)}")
