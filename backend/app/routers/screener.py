import os
from fastapi import APIRouter, HTTPException, Header
from typing import Optional
from ..screener_data import (
    get_stocks_page, get_stock_detail, get_sectors, get_refresh_status, run_refresh_batch, run_full_refresh,
)

router = APIRouter(prefix="/api/screener", tags=["Equity Screener"])

# Shared-secret gate for the refresh endpoint — set REFRESH_ADMIN_KEY in the
# Cloud Run service's environment and point Cloud Scheduler's HTTP job at
# this endpoint with the same value in the X-Admin-Key header. Left
# unenforced (open) if the env var isn't set, so local/dev usage isn't
# blocked — set it before exposing this publicly.
_ADMIN_KEY = os.environ.get("REFRESH_ADMIN_KEY")


def _check_admin(x_admin_key: Optional[str]):
    if _ADMIN_KEY and x_admin_key != _ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key.")


@router.get("/stocks")
def list_stocks(search: str = "", sector: str = "", exchange: str = "",
                 sort_by: str = "market_cap", descending: bool = True,
                 page: int = 1, page_size: int = 50):
    """
    Paginated, filterable read of the cached NSE+BSE fundamentals table.
    Never calls yfinance directly — always reads the Firestore cache built
    by /refresh, so this stays fast regardless of universe size.
    """
    if page_size > 200:
        page_size = 200
    return get_stocks_page(search=search, sector=sector, exchange=exchange,
                            sort_by=sort_by, descending=descending, page=page, page_size=page_size)


@router.get("/sectors")
def list_sectors():
    return {"sectors": get_sectors()}


@router.get("/stocks/{yf_symbol}")
def stock_detail(yf_symbol: str):
    """yf_symbol e.g. 'RELIANCE.NS' or '500325.BO' — includes last 4
    quarters of revenue/net income, fetched live (cheap single-ticker call,
    unlike the bulk fundamentals refresh)."""
    data = get_stock_detail(yf_symbol)
    if not data:
        raise HTTPException(status_code=404, detail="Stock not found in cache yet — try again after the next refresh.")
    return data


@router.get("/status")
def refresh_status():
    """Lets the frontend show data freshness ('last updated', coverage
    progress) instead of pretending this is live data."""
    return get_refresh_status()


@router.post("/refresh")
def refresh(batch_size: int = 150, full: bool = False, x_admin_key: Optional[str] = Header(default=None)):
    """
    full=False (default): processes one bounded batch starting from the
    saved cursor — good for a frequent small-batch schedule or the manual
    'Populate now' button.

    full=True: processes the ENTIRE NSE+BSE universe in this one call.
    This is what should be scheduled nightly (e.g. midnight IST) so the
    screener is always fully populated without anyone triggering anything
    by hand — see run_full_refresh()'s docstring for the Cloud Run timeout
    setting this requires.
    """
    _check_admin(x_admin_key)
    if full:
        return run_full_refresh()
    return run_refresh_batch(batch_size=batch_size)