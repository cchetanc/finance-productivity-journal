from fastapi import FastAPI
from app import auth, database, models, gemini
from app.routers import market, journals

app = FastAPI(title="Finance Productivity Journal API")

app.include_router(market.router)
app.include_router(journals.router)

@app.get("/health")
def health_check():
    return {"status": "ok", "message": "Service is running"}
