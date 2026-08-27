from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum

class NewsSentiment(str, Enum):
    GREEN = "Green"      # Bullish
    NEUTRAL = "Neutral"  # Neutral
    RED = "Red"          # Bearish

class NewsEntry(BaseModel):
    id: str
    headline: str
    summary: str
    sentiment: NewsSentiment
    target_sectors: List[str] = Field(default_factory=list, description="Sectors impacted by this news")
    target_tickers: List[str] = Field(default_factory=list, description="Specific stock tickers impacted by this news")
    timestamp: str

class MarketMoodScore(BaseModel):
    systemic_score: float = Field(..., ge=0, le=100, description="Overall systemic market mood score (0-100)")
    macro_risk_flags: List[str] = Field(default_factory=list, description="Macro risk warnings")
    timestamp: str
