"""
Nominatim Geocoding Client with caching and rate-limiting compliance.
"""
import asyncio
import time
from typing import Optional, Dict, Any, Tuple
import httpx


class GeocodingError(Exception):
    pass


class NominatimGeocoder:
    """
    OpenStreetMap Nominatim Geocoder.
    Adheres strictly to Nominatim Usage Policy:
    - Custom User-Agent header
    - Maximum 1 request per second
    - Query caching
    """

    def __init__(self, user_agent: str = "WebsiteLeadHunter/1.0 (contact@websiteleadhunter.local)"):
        self.user_agent = user_agent
        self.endpoint = "https://nominatim.openstreetmap.org/search"
        self._last_request_time = 0.0
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def geocode(self, query: str) -> Dict[str, Any]:
        """
        Geocodes a search string to coordinates and location metadata.
        Returns a dict with: lat, lon, display_name, boundingbox, importance.
        """
        clean_query = query.strip()
        if not clean_query:
            raise GeocodingError("Query cannot be empty.")

        cache_key = clean_query.lower()
        if cache_key in self._cache:
            return self._cache[cache_key]

        async with self._lock:
            # Respect 1 req/sec rate limit
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < 1.05:
                await asyncio.sleep(1.05 - elapsed)

            headers = {
                "User-Agent": self.user_agent,
                "Accept-Language": "en",
            }
            params = {
                "q": clean_query,
                "format": "jsonv2",
                "limit": 1,
                "addressdetails": 1,
            }

            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    response = await client.get(self.endpoint, params=params, headers=headers)
                    self._last_request_time = time.monotonic()

                    if response.status_code == 429:
                        raise GeocodingError("Nominatim rate limit reached. Please wait a few seconds and try again.")
                    elif response.status_code != 200:
                        raise GeocodingError(f"Nominatim returned status {response.status_code}: {response.text[:200]}")

                    data = response.json()
                    if not data or not isinstance(data, list) or len(data) == 0:
                        raise GeocodingError(f"Location not found for query: '{clean_query}'")

                    first = data[0]
                    result = {
                        "lat": float(first["lat"]),
                        "lon": float(first["lon"]),
                        "display_name": first.get("display_name", clean_query),
                        "boundingbox": first.get("boundingbox", []),
                        "type": first.get("type", "unknown"),
                    }
                    self._cache[cache_key] = result
                    return result

            except httpx.RequestError as exc:
                self._last_request_time = time.monotonic()
                raise GeocodingError(f"Network error communicating with Nominatim: {str(exc)}") from exc
