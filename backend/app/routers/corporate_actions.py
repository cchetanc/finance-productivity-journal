from fastapi import APIRouter
from ..market_data import get_dividend_announcements, get_corporate_action_announcements, get_results_calendar

router = APIRouter(prefix="/api/corporate-actions", tags=["Corporate Actions"])


@router.get("/dividends")
def dividends(limit: int = 20):
    """
    Dividend/record-date/ex-date announcement headlines. Sourced via news
    search RSS (see market_data.py docstring) — real, clickable links, but
    not a structured ex-date/record-date table the way a paid corporate-
    actions feed would give you.
    """
    return {"headlines": get_dividend_announcements(limit=limit)}


@router.get("/announcements")
def announcements(limit: int = 20):
    """Bonus issues, stock splits, buybacks."""
    return {"headlines": get_corporate_action_announcements(limit=limit)}


@router.get("/results-calendar")
def results_calendar(limit: int = 20):
    """Quarterly-results announcement-date headlines."""
    return {"headlines": get_results_calendar(limit=limit)}