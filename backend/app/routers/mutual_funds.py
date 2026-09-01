import os
from fastapi import APIRouter, HTTPException, Header
from typing import Optional
from ..mf_data import (
    get_funds_page, get_fund_detail, get_categories, get_refresh_status, run_refresh_batch, run_full_refresh,
    FUND_FIELD_CATALOG,
)

router = APIRouter(prefix="/api/mutual-funds", tags=["Mutual Funds"])

_ADMIN_KEY = os.environ.get("REFRESH_ADMIN_KEY")


def _check_admin(x_admin_key: Optional[str]):
    if _ADMIN_KEY and x_admin_key != _ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key.")


@router.get("")
def list_funds(search: str = "", category: str = "",
               sort_by: str = "cagr_5y", descending: bool = True,
               page: int = 1, page_size: int = 50):
    """
    Paginated, filterable read of the cached AMFI scheme universe with
    NAV-history-derived risk/return metrics. Reads Firestore only.
    """
    if page_size > 200:
        page_size = 200
    return get_funds_page(search=search, category=category,
                           sort_by=sort_by, descending=descending, page=page, page_size=page_size)


@router.get("/categories")
def list_categories():
    return {"categories": get_categories()}


@router.get("/fields")
def list_fields():
    """Documents every column on a mutual_funds row: whether it's pulled,
    calculated (with the formula), or has no free source and is
    intentionally left blank, and how often it can realistically change.
    Must stay registered before /{scheme_code} so 'fields' isn't matched
    as a scheme code."""
    return {"fields": FUND_FIELD_CATALOG}


@router.get("/status")
def refresh_status():
    return get_refresh_status()


@router.get("/{scheme_code}")
def fund_detail(scheme_code: str):
    data = get_fund_detail(scheme_code)
    if not data:
        raise HTTPException(status_code=404, detail="Scheme not found in cache yet — try again after the next refresh.")
    return data


@router.post("/refresh")
def refresh(batch_size: int = 80, full: bool = False, x_admin_key: Optional[str] = Header(default=None)):
    """
    full=False (default): one bounded batch from the saved cursor.
    full=True: processes every AMFI scheme in this one call — schedule
    nightly (midnight IST) for a fully-populated page. See
    run_full_refresh()'s docstring for timing/timeout implications.
    """
    _check_admin(x_admin_key)
    if full:
        return run_full_refresh()
    return run_refresh_batch(batch_size=batch_size)