"""
Discovery helper endpoints: Geocoding and categories list.
"""
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from app.discovery.geocoding import NominatimGeocoder, GeocodingError
from app.discovery.overpass import CATEGORY_LABELS

router = APIRouter(prefix="/api/discovery", tags=["Discovery Helpers"])
geocoder = NominatimGeocoder()


@router.get("/geocode")
async def geocode_location(q: str = Query(..., min_length=2)):
    try:
        result = await geocoder.geocode(q)
        return {
            "lat": result["lat"],
            "lon": result["lon"],
            "display_name": result["display_name"],
        }
    except GeocodingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Geocoding service error: {str(exc)}")


@router.get("/categories")
async def get_categories() -> List[Dict[str, str]]:
    return [
        {"id": key, "label": label}
        for key, label in CATEGORY_LABELS.items()
    ]
