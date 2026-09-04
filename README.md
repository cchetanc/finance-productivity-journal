# finance-productivity-journal
# 💹 Finance Intelligence Journal
### An AI-native personal finance co-pilot — market intelligence, screening, and trade execution in one place

> **Ideathon Cohort 3 Submission**
> **Team Name:** `<your team name>`
> **Team Members:** `<name 1>`, `<name 2>`, `<name 3>` — `<roles, e.g. Backend, Frontend, AI/Agents>`
> **Track / Problem Statement:** `<the track or problem statement this idea addresses>`
> **Submission Date:** `<date>`
> **Demo Video:** `<link>`  |  **Live Deployment:** `<link>`  |  **Pitch Deck:** `<link>`

---

## 1. Problem Statement

Retail investors today juggle **5–6 disconnected tools** to make a single informed decision:
a screener to shortlist stocks, a charting app to check price action, a news app for context,
a spreadsheet to track dividends/results dates, a spending tracker to know what they can even
afford to invest, and finally their broker's app to actually place the trade — with no single
place that connects *insight* to *action*.

This constant context-switching means:
- Good research rarely turns into a timely trade — by the time you've cross-checked five tabs,
  the opportunity has moved.
- There's no single "explain this to me" layer — raw data (P/E, VWAP, RMS margin) is available,
  but *what it means for me right now* isn't.
- Everyday spending and investing stay mentally (and technically) siloed, even though they're
  the same rupee.

## 2. Our Idea

**Finance Intelligence Journal** is a single web app that fuses **market research, portfolio
tooling, and a conversational AI analyst desk** into one workflow — so a user can go from
*"what's happening in the market"* → *"here's a stock/fund worth a look, and why"* →
*"place the trade"*, without leaving the page.

The centerpiece is a **multi-agent AI assistant** ("Daily Productivity Assistant") that routes
each question to the right specialist — equity analyst, quant desk, macro desk, spending
analyst, travel concierge — the same way a real research desk would hand off a query to the
right analyst, rather than one generic chatbot bluffing its way through every domain.

## 3. Key Features

| Feature | What it does |
|---|---|
| 🤖 **Multi-agent AI Desk** | Routes each query to the right specialist agent (Equity, Quant, Macro, Corporate-Actions, Spending, Cinema/Leisure, Travel) via an LLM router + synthesizer, backed by real tool calls — not hallucinated answers. |
| 📊 **Equity & Mutual Fund Screener** | Filterable, sortable screens across fundamentals (P/E, ROE, ROCE, margins, growth) and fund categories, with fast type-ahead search. |
| 📈 **Live, Interactive Charts** | Real TradingView-powered candlestick charts (not static images) embedded directly in chat and on stock pages. |
| 🧮 **Quant Desk & Breakout Screener** | On-demand quantitative reasoning (Sharpe/Sortino, volatility, VaR) *plus* a real momentum + volume-confirmation breakout screen that surfaces actual shortlisted stocks with numbers, not guesses. |
| 💰 **Trade Terminal** | Place manual or algorithmic orders (**Iceberg, TWAP, VWAP, Momentum Sniper**) in PAPER (simulated) or LIVE mode via Angel One's SmartAPI, with live wallet-balance display and pre-trade risk/insufficient-funds checks. |
| 🗣️ **Confirm-to-Trade from Chat** | The AI desk can propose a specific, data-backed trade idea and — only after explicit user confirmation — execute it through the same risk-checked trading engine as the terminal. Defaults to PAPER; never assumes real money without the user saying so. |
| 📢 **Dividends & Corporate Actions / Results Calendar** | Tracks upcoming dividends, splits, bonuses, and quarterly result dates so nothing is missed. |
| 💳 **Gmail-based Spending Insights** | Opt-in, read-only parsing of UPI/bank-debit alert emails to summarize real monthly spending — closing the loop between "what I spend" and "what I can invest." |
| 🌗 **Personal Journal & Voice** | A reflective daily journal with AI replies/summaries, and voice input for hands-free queries. |
| 🔐 **Per-user, Firebase-authenticated data model** | Every data path (trades, journals, spending, credentials) is scoped to the signed-in user via server-verified UID — never client- or model-supplied. |

## 4. Why This Is Different

- **It closes the loop.** Most finance apps stop at "here's the data." We go from insight →
  explicit human confirmation → actual order placement, inside the same conversation.
- **Real agents, not one mega-prompt.** A router classifies intent and dispatches to
  domain-specific agents with their own tools and guardrails — closer to how an actual
  research desk operates, and easier to reason about/extend than a single do-everything prompt.
- **Guardrails are load-bearing, not decorative.** The AI can *recommend* a trade but cannot
  execute one without an explicit, specific human confirmation; it defaults to simulated
  (PAPER) money; and every BUY — whether from the terminal or from chat — runs through the same
  pre-trade insufficient-funds check before it ever reaches the broker.
- **Screens are transparent, not black-box.** The "breakout" screener is explicitly presented
  as a momentum + volume-confirmation heuristic on real cached numbers — not dressed up as a
  guaranteed signal.

## 5. Architecture

```
┌──────────────────────────────┐         ┌───────────────────────────────────────┐
│         Frontend              │  HTTPS  │              Backend                    │
│  (Streamlit, multi-page)      │────────▶│           (FastAPI, Python)             │
│                                │         │                                          │
│  • Daily Productivity Assistant│        │  • Router → Specialist Agents           │
│  • Equity / MF Screener        │        │    (Equity · Quant · Macro · Spending · │
│  • Trade Terminal              │        │     Corp-Actions · Cinema · Travel)     │
│  • Dividends & Results Calendar│        │    powered by Gemini + tool-calling      │
│  • Admin panel                 │        │  • Trading Engine (Paper / Angel One)   │
└──────────────────────────────┘         │  • Screener data pipeline (Firestore)   │
                                          │  • Gmail OAuth + spending parser        │
                                          └───────────────────┬─────────────────────┘
                                                               │
                                          ┌────────────────────┴────────────────────┐
                                          │   Firebase Auth · Firestore · Secret     │
                                          │   Manager · yfinance · Angel One SmartAPI │
                                          │   · TradingView (charts) · Gmail API      │
                                          └───────────────────────────────────────────┘
```

## 6. Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | Streamlit (Python), custom components (`streamlit-keyup`), TradingView embedded widgets |
| **Backend** | FastAPI, Python, Uvicorn |
| **AI / Agents** | Google Gemini (`google-genai`) with function/tool calling, custom multi-agent router + synthesizer |
| **Data & Auth** | Firebase Authentication, Google Cloud Firestore, Google Cloud Secret Manager |
| **Market Data** | `yfinance`, cached Firestore screener pipeline |
| **Trading** | Angel One SmartAPI (`smartapi-python`), `pyotp` (TOTP login), custom paper-trading simulator, custom execution engine (Iceberg / TWAP / VWAP / Momentum Sniper algos) |
| **Integrations** | Gmail API (OAuth, read-only) for spending insights, Google Cloud Speech-to-Text / Text-to-Speech for voice |
| **Infra** | Deployed on Google Cloud Run |

## 7. How It Works (User Flow)

1. **Sign in** with Firebase Auth.
2. **Ask the Daily Productivity Assistant** anything — "how's RELIANCE looking on NSE",
   "any stocks about to break out", "what did I spend on food last month" — the router sends it
   to the right specialist, which calls real tools (live quotes, screener queries, spending
   summaries) rather than guessing.
3. **Explore deeper** via the Equity Screener, Mutual Fund Screener, Dividends & Corporate
   Actions, or Results Calendar pages.
4. **Act on it** in the Trade Terminal — place a manual order or run an execution algorithm, in
   PAPER mode to practice risk-free or LIVE mode against a real Angel One account — or simply
   tell the assistant "yes, buy 10 of it" once it's proposed a specific idea.
5. Every trade — from the terminal or from chat — is checked against your live wallet balance
   before it's sent, so you're told plainly if you're short, instead of finding out at the broker.

## 8. Setup / Run Locally

```bash
# 1. Clone
git clone <your-repo-url>
cd finance-productivity-journal

# 2. Backend
cd backend
pip install -r requirements.txt
# configure Firebase, Firestore, and Secret Manager credentials (see backend/app/trading/credentials.py
# and backend/app/secrets.py for the expected secret names)
uvicorn app.main:app --reload --port 8080

# 3. Frontend (new terminal)
cd frontend
pip install -r requirements.txt
streamlit run app.py
```

> Live/broker trading requires an Angel One SmartAPI key, client code, PIN, and TOTP secret,
> configured per-user from the Trade Terminal's "Connect Broker" panel. PAPER mode works out of
> the box with no broker credentials.

## 9. Screenshots / Demo

`<Add 3–5 screenshots or a short GIF here: the AI desk giving a stock recommendation, the
breakout screen, the Trade Terminal with the live wallet balance, the Equity Screener.>`

`<Demo video link>`

## 10. Challenges We Ran Into

- **Keeping the AI honest.** It's easy for an LLM to sound confident about numbers it invented.
  Every agent is tool-grounded — it can only cite what a real API/database call actually
  returned, and is explicitly instructed to say "I don't know" rather than fill gaps.
- **Safe autonomy.** Letting an AI *recommend* trades is useful; letting it *execute* them
  unattended is a real-money risk. We solved this with an explicit, non-bypassable
  human-confirmation gate and a PAPER-by-default execution path.
- **Real-time feel in a server-rendered app.** Streamlit reruns the whole script on most
  interactions; we used fragment-scoped reruns for autocomplete/search so it feels closer to a
  native app instead of round-tripping the whole page per keystroke.

## 11. What's Next

- Push-based proactive alerts (not just "on open") when a tracked stock crosses a watch
  threshold or a portfolio holding has a corporate action.
- Backtesting the breakout screener's historical hit rate, and surfacing that transparently.
- Deeper portfolio-level risk view (sector concentration, correlation) rather than
  per-trade-only risk checks.
- Algorithm that can build winning portfolio on its own on the horizon of shortterm or intraday knd of positions for a autonomous winnning trade and side income
## 12. Team

| Name | Role | Contact |
|---|---|---|
| `<Name>` | `<Role>` | `<email / LinkedIn>` |
| `<Name>` | `<Role>` | `<email / LinkedIn>` |
| `<Name>` | `<Role>` | `<email / LinkedIn>` |

---

*Built for Ideathon Cohort 3.