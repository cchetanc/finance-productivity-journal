from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
import logging
from datetime import datetime

from ..secrets import access_secret_version
from ..reports import generate_pre_market_report, generate_eod_report
from ..trading.inbound_email import poll_inbound_trades

router = APIRouter(prefix="/api", tags=["Reports", "Inbound Trades"])
logger = logging.getLogger("reports.router")

def verify_scheduler_secret(x_scheduler_secret: str = Header(...)):
    expected = access_secret_version("SCHEDULER_SHARED_SECRET")
    if not expected or x_scheduler_secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

def get_uid_for_target_email(email: str) -> str:
    import firebase_admin
    from firebase_admin import auth
    try:
        user = auth.get_user_by_email(email, app=firebase_admin.get_app("frontend_auth"))
        return user.uid
    except Exception as e:
        logger.error(f"Failed to resolve uid for {email}: {e}")
        raise HTTPException(status_code=500, detail="Could not resolve target user")

def is_trading_day() -> bool:
    from pytz import timezone
    IST = timezone("Asia/Kolkata")
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    # Define a small list of known NSE holidays for 2026 if necessary, but this is a stub.
    holidays = ["2026-01-26", "2026-05-01", "2026-08-15", "2026-10-02", "2026-12-25"]
    if now.strftime("%Y-%m-%d") in holidays:
        return False
    return True

@router.post("/reports/pre-market", dependencies=[Depends(verify_scheduler_secret)])
async def trigger_pre_market():
    if not is_trading_day():
        return {"status": "skipped", "reason": "Not a trading day"}
    
    email = "cchetanc@gmail.com"
    uid = get_uid_for_target_email(email)
    await generate_pre_market_report(uid, email)
    return {"status": "success"}

@router.post("/reports/eod", dependencies=[Depends(verify_scheduler_secret)])
async def trigger_eod():
    if not is_trading_day():
        return {"status": "skipped", "reason": "Not a trading day"}
        
    email = "cchetanc@gmail.com"
    uid = get_uid_for_target_email(email)
    await generate_eod_report(uid, email)
    return {"status": "success"}

@router.post("/trading/poll-inbound-trades", dependencies=[Depends(verify_scheduler_secret)])
async def trigger_inbound_polling():
    email = "cchetanc@gmail.com"
    uid = get_uid_for_target_email(email)
    await poll_inbound_trades(uid)
    return {"status": "success"}
