from fastapi import APIRouter, HTTPException, Query
from ..amadeus_client import get_hotel_availability
from pydantic import BaseModel
from typing import List

router = APIRouter(prefix="/api/v1/hotels", tags=["Hotels"])

class HotelAvailabilityResponse(BaseModel):
    hotel_id: str
    name: str | None
    latitude: float | None
    longitude: float | None
    lowest_rate: str | None
    currency: str | None
    available_rooms: int
    is_available: bool

@router.get("/availability", response_model=List[HotelAvailabilityResponse])
def check_availability(
    latitude: float = Query(..., description="Latitude of the location"),
    longitude: float = Query(..., description="Longitude of the location"),
    radius: int = Query(5, description="Search radius in kilometers"),
    check_in_date: str = Query(..., description="Check-in date in YYYY-MM-DD format"),
    check_out_date: str = Query(..., description="Check-out date in YYYY-MM-DD format"),
    adults: int = Query(1, description="Number of adult guests")
):
    """
    Checks global hotel room availability by location using Amadeus APIs.
    Orchestrates a multi-step workflow: geocoding to hotel IDs, then fetching offers.
    Caches responses for 3-5 minutes.
    """
    try:
        results = get_hotel_availability(
            lat=latitude,
            lon=longitude,
            radius=radius,
            check_in=check_in_date,
            check_out=check_out_date,
            adults=adults
        )
        return results
    except ValueError as ve:
        raise HTTPException(status_code=500, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch hotel availability: {str(e)}")
