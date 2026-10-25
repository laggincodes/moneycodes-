"""
Automated tests for Discovery: Geocoding, Overpass provider, deduplication, and provenance.
"""
import pytest
from app.discovery.geocoding import NominatimGeocoder, GeocodingError
from app.discovery.overpass import OverpassDiscoveryProvider
from app.discovery.base import DiscoveredBusiness, FieldProvenance


@pytest.mark.asyncio
async def test_geocoding_live():
    """Verify live geocoding of a known location."""
    geocoder = NominatimGeocoder(user_agent="WebsiteLeadHunterTest/1.0 (test@websiteleadhunter.local)")
    result = await geocoder.geocode("Connaught Place, New Delhi")
    
    assert "lat" in result
    assert "lon" in result
    assert "display_name" in result
    assert isinstance(result["lat"], float)
    assert isinstance(result["lon"], float)
    assert 28.6 < result["lat"] < 28.7
    assert 77.2 < result["lon"] < 77.3


@pytest.mark.asyncio
async def test_geocoding_invalid_query():
    """Verify appropriate error on invalid location."""
    geocoder = NominatimGeocoder(user_agent="WebsiteLeadHunterTest/1.0 (test@websiteleadhunter.local)")
    with pytest.raises(GeocodingError):
        await geocoder.geocode("xyzqwertyuiopnonexistentplacename123456789")


def test_business_deduplication_key():
    """Verify business identity key normalization."""
    biz1 = DiscoveredBusiness(
        provider_id="osm:node:101",
        name="The Coffee Club!",
        category="Café",
        lat=28.63241,
        lon=77.21981,
        source_url="https://www.openstreetmap.org/node/101",
    )
    biz2 = DiscoveredBusiness(
        provider_id="osm:way:202",
        name="The Coffee Club",
        category="Café",
        lat=28.63242,
        lon=77.21982,
        source_url="https://www.openstreetmap.org/way/202",
    )
    
    # Both round to same 4 decimals and normalized alphanumeric name
    assert biz1.normalize_identity_key() == biz2.normalize_identity_key()


@pytest.mark.asyncio
async def test_live_small_overpass_discovery():
    """
    Milestone B Verification Gate:
    Run a real, small query against Overpass API.
    Verify that businesses are returned with genuine fields and provenance,
    or a precise network/error message is provided without fake results.
    """
    provider = OverpassDiscoveryProvider(
        user_agent="WebsiteLeadHunterTest/1.0 (test@websiteleadhunter.local)",
        timeout=25.0,
    )
    # Connaught Place coordinates: ~28.6315, 77.2167
    lat, lon = 28.6315, 77.2167
    radius_km = 0.4
    categories = ["cafes", "restaurants"]
    max_leads = 4

    result = await provider.discover(
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        categories=categories,
        max_leads=max_leads,
    )

    if result.error_message:
        # If upstream Overpass server is down/rate-limited, verify it failed gracefully with exact reason
        print(f"Overpass upstream notice: {result.error_message}")
        assert "All Overpass mirrors failed" in result.error_message
        assert len(result.businesses) == 0
    else:
        assert result.coverage.total_found >= 0
        assert len(result.businesses) <= max_leads
        print(f"Found {len(result.businesses)} businesses (Total in area: {result.coverage.total_found})")
        
        for biz in result.businesses:
            assert biz.name
            assert biz.category
            assert biz.lat != 0.0
            assert biz.lon != 0.0
            assert biz.source_url.startswith("https://www.openstreetmap.org/")
            assert "name" in biz.provenance
            assert biz.provenance["name"].source == "OpenStreetMap"
            assert biz.provenance["name"].source_url == biz.source_url
            print(f"- {biz.name} ({biz.category}): phone={biz.phone}, website={biz.website}")
