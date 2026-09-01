import os
import time
import requests
from threading import Lock

# Environment Variables
AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")

# Amadeus Test Endpoints
AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
GEO_URL = "https://test.api.amadeus.com/v1/reference-data/locations/hotels/by-geocode"
OFFERS_URL = "https://test.api.amadeus.com/v3/shopping/hotel-offers"

# Token Management
_access_token = None
_token_expiry = 0
_auth_lock = Lock()

# Simple TTL Cache for Availability Responses
_availability_cache = {}
_cache_lock = Lock()
CACHE_TTL_SECONDS = 300  # 5 minutes

def get_access_token() -> str:
    global _access_token, _token_expiry
    now = time.time()
    
    with _auth_lock:
        if _access_token and now < _token_expiry:
            return _access_token

        if not AMADEUS_API_KEY or not AMADEUS_API_SECRET:
            raise ValueError("Amadeus API credentials are not configured in environment variables.")

        response = requests.post(
            AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": AMADEUS_API_KEY,
                "client_secret": AMADEUS_API_SECRET
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        _access_token = data["access_token"]
        # Amadeus typically returns expires_in = 1799 seconds (30 mins). We buffer by 60 seconds.
        _token_expiry = now + data.get("expires_in", 1800) - 60
        return _access_token

def resolve_hotels_by_location(lat: float, lon: float, radius: int) -> list[str]:
    """Resolves a list of Amadeus Hotel IDs within the specified radius (in KM)."""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "latitude": lat,
        "longitude": lon,
        "radius": radius,
        "radiusUnit": "KM"
    }
    
    response = requests.get(GEO_URL, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    
    # Extract hotelIds. Limit to max 50 to avoid payload size issues on the offers endpoint
    hotels = data.get("data", [])
    hotel_ids = [h["hotelId"] for h in hotels][:50]
    return hotel_ids

def fetch_hotel_offers(hotel_ids: list[str], check_in: str, check_out: str, adults: int) -> list[dict]:
    """Fetches live pricing and availability for a list of hotel IDs."""
    if not hotel_ids:
        return []

    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "hotelIds": ",".join(hotel_ids),
        "adults": adults,
        "checkInDate": check_in,
        "checkOutDate": check_out
    }

    # Note: Using v3/shopping/hotel-offers
    response = requests.get(OFFERS_URL, headers=headers, params=params, timeout=20)
    
    if response.status_code == 400:
        # Amadeus returns 400 if none of the hotels have offers for the given dates in the sandbox environment
        return []
        
    response.raise_for_status()
    data = response.json()
    
    return data.get("data", [])

def get_hotel_availability(lat: float, lon: float, radius: int, check_in: str, check_out: str, adults: int) -> list[dict]:
    """
    Main workflow: Geolocation -> Hotel IDs -> Offers
    Includes a 5-minute TTL cache based on request parameters.
    """
    cache_key = f"{lat}_{lon}_{radius}_{check_in}_{check_out}_{adults}"
    
    with _cache_lock:
        if cache_key in _availability_cache:
            entry = _availability_cache[cache_key]
            if time.time() < entry["expiry"]:
                return entry["data"]
            else:
                del _availability_cache[cache_key]

    # 1. Resolve Location to Hotel IDs
    hotel_ids = resolve_hotels_by_location(lat, lon, radius)
    
    # 2. Fetch Offers
    raw_offers = fetch_hotel_offers(hotel_ids, check_in, check_out, adults)
    
    # 3. Structure Output
    results = []
    for item in raw_offers:
        hotel_info = item.get("hotel", {})
        offers = item.get("offers", [])
        
        if not offers:
            continue
            
        # Find the lowest price
        lowest_offer = min(offers, key=lambda x: float(x.get("price", {}).get("total", "9999999")))
        price_info = lowest_offer.get("price", {})
        
        results.append({
            "hotel_id": hotel_info.get("hotelId"),
            "name": hotel_info.get("name"),
            "latitude": hotel_info.get("latitude"),
            "longitude": hotel_info.get("longitude"),
            "lowest_rate": price_info.get("total"),
            "currency": price_info.get("currency"),
            "available_rooms": len(offers), # Approximation based on offer count
            "is_available": True
        })

    # Update Cache
    with _cache_lock:
        _availability_cache[cache_key] = {
            "data": results,
            "expiry": time.time() + CACHE_TTL_SECONDS
        }

    return results
