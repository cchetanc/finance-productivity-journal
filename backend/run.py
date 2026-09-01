from fastapi import FastAPI
from app import auth, database, models, gemini, sentiment
from app.routers import market, journals, screener, mutual_funds, corporate_actions, trading, hotels

app = FastAPI(title="Finance Productivity Journal API")

app.include_router(market.router)
app.include_router(journals.router)
app.include_router(screener.router)
app.include_router(mutual_funds.router)
app.include_router(corporate_actions.router)
app.include_router(trading.router)
app.include_router(hotels.router)


@app.on_event("startup")
def _warm_up_finbert():
    """
    Loads the local FinBERT model into memory once, at process startup,
    instead of on the first incoming /news/sentiment request. This keeps
    that first real request fast and surfaces a load failure immediately
    in the deploy logs rather than silently on a user-facing call.
    """
    sentiment._get_pipeline()


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "Service is running"}