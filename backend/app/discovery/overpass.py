"""
OpenStreetMap Overpass API Discovery Provider with fallback mirrors and deduplication.
"""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional, Any, Set
import httpx

from app.discovery.base import (
    DiscoveryProvider,
    DiscoveryResult,
    DiscoveryCoverageReport,
    DiscoveredBusiness,
    FieldProvenance,
)

logger = logging.getLogger(__name__)

CATEGORY_OSM_FILTERS = {
    "cafes": [
        'node["amenity"="cafe"]',
        'way["amenity"="cafe"]',
    ],
    "restaurants": [
        'node["amenity"~"restaurant|fast_food"]',
        'way["amenity"~"restaurant|fast_food"]',
    ],
    "shops": [
        'node["shop"]',
        'way["shop"]',
    ],
    "salons": [
        'node["shop"~"hairdresser|beauty|barber"]',
        'way["shop"~"hairdresser|beauty|barber"]',
    ],
    "gyms": [
        'node["leisure"="fitness_centre"]',
        'way["leisure"="fitness_centre"]',
        'node["sport"~"fitness|gym"]',
    ],
    "coaching": [
        'node["amenity"~"college|language_school|tutoring|training"]',
        'way["amenity"~"college|language_school|tutoring|training"]',
    ],
    "clinics": [
        'node["amenity"~"clinic|doctors|dentist|pharmacy"]',
        'way["amenity"~"clinic|doctors|dentist|pharmacy"]',
        'node["healthcare"]',
        'way["healthcare"]',
    ],
    "other": [
        'node["craft"]',
        'way["craft"]',
        'node["office"]',
        'way["office"]',
    ],
}

CATEGORY_LABELS = {
    "cafes": "Café",
    "restaurants": "Restaurant / Food",
    "shops": "Retail Shop",
    "salons": "Salon & Beauty",
    "gyms": "Gym & Fitness",
    "coaching": "Coaching & Education",
    "clinics": "Clinic & Healthcare",
    "other": "Local Services & Office",
}

OVERPASS_ENDPOINTS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


class OverpassDiscoveryProvider(DiscoveryProvider):
    """
    Overpass API business discovery provider with retry, mirror failover,
    deduplication, and field-level provenance tracking.
    """

    def __init__(
        self,
        user_agent: str = "WebsiteLeadHunter/1.0 (contact@websiteleadhunter.local)",
        timeout: float = 12.0,
    ):
        self.user_agent = user_agent
        self.timeout = timeout

    def _build_overpass_query(
        self,
        lat: float,
        lon: float,
        radius_m: int,
        categories: List[str],
    ) -> str:
        """Constructs an optimized Overpass QL query."""
        filters = []
        for cat in categories:
            cat_key = cat.lower()
            if cat_key in CATEGORY_OSM_FILTERS:
                filters.extend(CATEGORY_OSM_FILTERS[cat_key])

        if not filters:
            # Default to all main categories
            for cat_filters in CATEGORY_OSM_FILTERS.values():
                filters.extend(cat_filters)

        queries_body = ""
        for f in filters:
            queries_body += f"  {f}(around:{radius_m},{lat},{lon});\n"

        return f"""[out:json][timeout:25];
(
{queries_body});
out center tags;
"""

    def _detect_category_label(self, tags: Dict[str, str], requested_cats: List[str]) -> str:
        amenity = tags.get("amenity", "")
        shop = tags.get("shop", "")
        leisure = tags.get("leisure", "")
        healthcare = tags.get("healthcare", "")

        if shop in ("hairdresser", "beauty", "barber"):
            return "Salon & Beauty"
        if amenity == "cafe":
            return "Café"
        if amenity in ("restaurant", "fast_food"):
            return "Restaurant / Food"
        if leisure == "fitness_centre" or "gym" in tags.get("sport", ""):
            return "Gym & Fitness"
        if healthcare or amenity in ("clinic", "doctors", "dentist", "pharmacy"):
            return "Clinic & Healthcare"
        if amenity in ("college", "language_school", "tutoring", "training"):
            return "Coaching & Education"
        if shop:
            return f"Shop ({shop.replace('_', ' ').capitalize()})"
        if tags.get("craft"):
            return f"Craft ({tags['craft'].capitalize()})"
        if tags.get("office"):
            return f"Office ({tags['office'].capitalize()})"
        return "Local Business"

    def _format_address(self, tags: Dict[str, str]) -> Optional[str]:
        parts = []
        housenumber = tags.get("addr:housenumber")
        street = tags.get("addr:street")
        if housenumber and street:
            parts.append(f"{housenumber} {street}")
        elif street:
            parts.append(street)

        suburb = tags.get("addr:suburb") or tags.get("addr:neighbourhood")
        if suburb:
            parts.append(suburb)

        city = tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village")
        if city:
            parts.append(city)

        postcode = tags.get("addr:postcode")
        if postcode:
            parts.append(postcode)

        if not parts:
            # If no address tags, check place or landmark
            place = tags.get("addr:place") or tags.get("place")
            return place if place else None

        return ", ".join(parts)

    def _clean_phone(self, phone_str: Optional[str]) -> Optional[str]:
        if not phone_str:
            return None
        cleaned = phone_str.strip()
        # If separated by semicolon or slash, take first primary number
        if ";" in cleaned:
            cleaned = cleaned.split(";")[0].strip()
        elif "/" in cleaned:
            cleaned = cleaned.split("/")[0].strip()
        return cleaned if len(cleaned) >= 5 else None

    def _clean_website(self, url_str: Optional[str]) -> Optional[str]:
        if not url_str:
            return None
        cleaned = url_str.strip()
        if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
            cleaned = f"https://{cleaned}"
        return cleaned

    async def discover(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        categories: List[str],
        max_leads: int = 50,
    ) -> DiscoveryResult:
        radius_m = int(radius_km * 1000)
        query = self._build_overpass_query(lat, lon, radius_m, categories)

        last_error: Optional[str] = None
        data = None

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

        # Attempt querying Overpass endpoints with failover
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for endpoint in OVERPASS_ENDPOINTS:
                try:
                    logger.info(f"Querying Overpass at {endpoint}...")
                    response = await client.post(endpoint, data={"data": query}, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        break
                    elif response.status_code == 429:
                        last_error = f"Rate limit (429) hit on {endpoint}"
                        await asyncio.sleep(1.0)
                    else:
                        last_error = f"Server error {response.status_code} on {endpoint}: {response.text[:120]}"
                except httpx.RequestError as exc:
                    last_error = f"Connection error connecting to {endpoint}: {str(exc)}"

        if data is None:
            return DiscoveryResult(
                businesses=[],
                coverage=DiscoveryCoverageReport(
                    total_found=0,
                    returned_count=0,
                    source="OpenStreetMap Overpass API",
                    categories_searched=categories,
                    search_center={"lat": lat, "lon": lon},
                    search_radius_km=radius_km,
                ),
                error_message=f"All Overpass mirrors failed. Last error: {last_error}",
            )

        elements = data.get("elements", [])
        total_raw_found = len(elements)

        seen_provider_ids: Set[str] = set()
        seen_identity_keys: Set[str] = set()
        discovered_businesses: List[DiscoveredBusiness] = []

        now_iso = datetime.now(timezone.utc).isoformat()

        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("name:en") or tags.get("brand")
            # Skip unnamed POIs - businesses need a name to be actionable leads
            if not name or len(name.strip()) < 2:
                continue

            name = name.strip()
            osm_type = el.get("type", "node")
            osm_id = el.get("id")
            provider_id = f"osm:{osm_type}:{osm_id}"

            if provider_id in seen_provider_ids:
                continue
            seen_provider_ids.add(provider_id)

            if osm_type == "node":
                el_lat = float(el.get("lat", 0.0))
                el_lon = float(el.get("lon", 0.0))
            else:
                center = el.get("center", {})
                el_lat = float(center.get("lat", 0.0))
                el_lon = float(center.get("lon", 0.0))

            if el_lat == 0.0 and el_lon == 0.0:
                continue

            source_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
            cat_label = self._detect_category_label(tags, categories)
            address = self._format_address(tags)
            raw_phone = tags.get("phone") or tags.get("contact:phone") or tags.get("mobile") or tags.get("contact:mobile")
            phone = self._clean_phone(raw_phone)
            raw_website = tags.get("website") or tags.get("contact:website") or tags.get("url")
            website = self._clean_website(raw_website)
            opening_hours = tags.get("opening_hours")

            # Field-level provenance
            provenance: Dict[str, FieldProvenance] = {
                "name": FieldProvenance(source="OpenStreetMap", retrieved_at=now_iso, source_url=source_url),
                "category": FieldProvenance(source="OpenStreetMap", retrieved_at=now_iso, source_url=source_url),
                "coordinates": FieldProvenance(source="OpenStreetMap", retrieved_at=now_iso, source_url=source_url),
            }
            if address:
                provenance["address"] = FieldProvenance(source="OpenStreetMap", retrieved_at=now_iso, source_url=source_url)
            if phone:
                provenance["phone"] = FieldProvenance(source="OpenStreetMap", retrieved_at=now_iso, source_url=source_url)
            if website:
                provenance["website"] = FieldProvenance(source="OpenStreetMap", retrieved_at=now_iso, source_url=source_url)
            if opening_hours:
                provenance["opening_hours"] = FieldProvenance(source="OpenStreetMap", retrieved_at=now_iso, source_url=source_url)

            biz = DiscoveredBusiness(
                provider_id=provider_id,
                name=name,
                category=cat_label,
                address=address,
                lat=el_lat,
                lon=el_lon,
                phone=phone,
                website=website,
                opening_hours=opening_hours,
                source_url=source_url,
                provenance=provenance,
                raw_data=tags,
            )

            identity_key = biz.normalize_identity_key()
            if identity_key in seen_identity_keys:
                continue
            seen_identity_keys.add(identity_key)

            discovered_businesses.append(biz)
            if len(discovered_businesses) >= max_leads:
                break

        coverage = DiscoveryCoverageReport(
            total_found=total_raw_found,
            returned_count=len(discovered_businesses),
            source="OpenStreetMap Overpass API",
            categories_searched=categories,
            search_center={"lat": lat, "lon": lon},
            search_radius_km=radius_km,
        )

        return DiscoveryResult(
            businesses=discovered_businesses,
            coverage=coverage,
            error_message=None,
        )
