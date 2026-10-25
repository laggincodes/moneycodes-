"""
Base discovery provider interfaces and data models.
"""
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class FieldProvenance(BaseModel):
    source: str
    retrieved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_url: Optional[str] = None


class DiscoveredBusiness(BaseModel):
    provider_id: str  # e.g. "osm:node:123456"
    name: str
    category: str
    address: Optional[str] = None
    lat: float
    lon: float
    phone: Optional[str] = None
    website: Optional[str] = None
    opening_hours: Optional[str] = None
    source_url: str
    provenance: Dict[str, FieldProvenance] = Field(default_factory=dict)
    raw_data: Dict[str, Any] = Field(default_factory=dict)

    def normalize_identity_key(self) -> str:
        """Normalized key for deduplication based on clean name and rounded coordinates."""
        clean_name = "".join(c.lower() for c in self.name if c.isalnum())
        # Round coords to ~10 meters (4 decimals)
        rounded_lat = round(self.lat, 4)
        rounded_lon = round(self.lon, 4)
        return f"{clean_name}@{rounded_lat},{rounded_lon}"


class DiscoveryCoverageReport(BaseModel):
    total_found: int
    returned_count: int
    source: str
    disclaimer: str = (
        "OpenStreetMap is a community-contributed dataset. Coverage varies by region "
        "and is not exhaustive. Absence of a listing does not guarantee a business does not exist, "
        "and absence of a website tag indicates no website was recorded in OSM."
    )
    categories_searched: List[str]
    search_center: Dict[str, float]
    search_radius_km: float


class DiscoveryResult(BaseModel):
    businesses: List[DiscoveredBusiness]
    coverage: DiscoveryCoverageReport
    error_message: Optional[str] = None


class DiscoveryProvider(ABC):
    """Abstract interface for business listing discovery providers."""

    @abstractmethod
    async def discover(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        categories: List[str],
        max_leads: int = 50,
    ) -> DiscoveryResult:
        """
        Discover businesses within a radius of given coordinates.
        """
        pass
