import asyncio
import json
import logging
import yfinance as yf
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from google.cloud import firestore

app = Server("finance-mcp-server")

def get_firestore_client():
    try:
        return firestore.Client()
    except Exception as e:
        logging.warning(f"Firestore unavailable: {e}")
        return None

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_market_data",
            description="Retrieve equity and commodity price and fundamentals using a ticker symbol.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol, e.g. AAPL, GC=F"}
                },
                "required": ["ticker"]
            }
        ),
        types.Tool(
            name="get_fund_data",
            description="Retrieve mutual fund or ETF NAV, category, and expense ratio.",
            inputSchema={
                "type": "object",
                "properties": {
                    "fund_id": {"type": "string", "description": "Fund ticker, e.g. SPY, VOO"}
                },
                "required": ["fund_id"]
            }
        ),
        types.Tool(
            name="get_macro_indicators",
            description="Retrieve interest rates, inflation, and index levels.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="save_advisory_session",
            description="Save an advisory session to Firestore.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User identifier"},
                    "query": {"type": "string", "description": "The user's query"},
                    "response": {"type": "string", "description": "The final response provided"}
                },
                "required": ["user_id", "query", "response"]
            }
        ),
        types.Tool(
            name="get_user_portfolio",
            description="Retrieve a user's portfolio and risk tolerance from Firestore.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User identifier"}
                },
                "required": ["user_id"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    db = get_firestore_client()

    if name == "get_market_data":
        ticker = arguments["ticker"].strip().upper()
        try:
            t = yf.Ticker(ticker)
            info = t.info
            data = {
                "symbol": info.get("symbol", ticker),
                "shortName": info.get("shortName"),
                "currentPrice": info.get("currentPrice"),
                "previousClose": info.get("previousClose"),
                "marketCap": info.get("marketCap"),
                "trailingPE": info.get("trailingPE"),
                "sector": info.get("sector")
            }
            return [types.TextContent(type="text", text=json.dumps(data))]
        except Exception as e:
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elif name == "get_fund_data":
        fund_id = arguments["fund_id"].strip().upper()
        try:
            t = yf.Ticker(fund_id)
            info = t.info
            data = {
                "symbol": info.get("symbol", fund_id),
                "shortName": info.get("shortName"),
                "navPrice": info.get("navPrice"),
                "previousClose": info.get("previousClose"),
                "ytdReturn": info.get("ytdReturn"),
                "expenseRatio": info.get("fundFamily"),
                "category": info.get("category")
            }
            return [types.TextContent(type="text", text=json.dumps(data))]
        except Exception as e:
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elif name == "get_macro_indicators":
        # Hardcode some macro tickers as an example using yfinance
        try:
            tickers = {"10Y_Treasury": "^TNX", "13W_Treasury": "^IRX", "S&P500": "^GSPC", "Gold": "GC=F"}
            data = {}
            for key, t in tickers.items():
                info = yf.Ticker(t).info
                data[key] = info.get("previousClose", info.get("regularMarketPreviousClose"))
            return [types.TextContent(type="text", text=json.dumps({"macro_indicators": data}))]
        except Exception as e:
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elif name == "save_advisory_session":
        if not db:
            return [types.TextContent(type="text", text=json.dumps({"error": "Firestore not available"}))]
        user_id = arguments["user_id"]
        record = {
            "user_id": user_id,
            "query": arguments["query"],
            "response": arguments["response"],
            "timestamp": datetime.utcnow().isoformat()
        }
        try:
            db.collection("advisory_sessions").add(record)
            return [types.TextContent(type="text", text=json.dumps({"status": "saved"}))]
        except Exception as e:
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    elif name == "get_user_portfolio":
        if not db:
            return [types.TextContent(type="text", text=json.dumps({"error": "Firestore not available"}))]
        user_id = arguments["user_id"]
        try:
            doc = db.collection("user_profiles").document(user_id).get()
            if doc.exists:
                return [types.TextContent(type="text", text=json.dumps(doc.to_dict()))]
            else:
                return [types.TextContent(type="text", text=json.dumps({"status": "no_portfolio_found"}))]
        except Exception as e:
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    raise ValueError(f"Unknown tool: {name}")

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, write_stream,
            app.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())
