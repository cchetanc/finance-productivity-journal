# Backend Architecture

The backend of the Finance Intelligence Journal is a robust, agentic system built to handle complex financial queries, analyze market data, and provide personalized insights.

## Core Technologies
- **Framework:** FastAPI (Python)
- **AI/LLMs:** Google Gemini models (via Langchain/Google GenAI SDK)
- **Database:** Firebase Firestore (NoSQL)
- **Deployment:** Google Cloud Run

## Architectural Components

1. **API Layer (Routers):**
   - The system exposes RESTful endpoints using FastAPI routers (e.g., `/api/market/voice`, `/api/portfolio`).
   - These endpoints act as the entry point for the frontend to request data, trigger agents, or fetch historical information.

2. **Agentic Layer:**
   - A suite of specialized AI agents built with Langchain and Gemini.
   - **Agents Router/Orchestrator:** Determines which specific agent should handle a given user query.
   - Specialized agents include:
     - **Market Agent:** Fetches and analyzes live stock market data.
     - **Trade Agent:** Analyzes trade orders, calculates risk (R-Multiple, Win Rate), and provides journaling insights.
     - **Daily Assistant:** A multi-turn conversational agent for general productivity and financial planning.
     - **Chart Agent:** Generates code to render technical charts.

3. **Data Access Layer:**
   - Interacts with Firebase Firestore to persist user data, trade journals, and chat histories.
   - Enables multi-turn conversations by retrieving previous interactions (`users/{uid}/daily_chat/{date_str}`) and passing them as context to the LLM.

4. **Integration Layer:**
   - Connects to external APIs (like Google Finance, Yahoo Finance, or custom broker APIs) to fetch real-time market data and execute trades.

## Multi-Turn Interaction Flow
1. The backend receives a user query along with a `history` payload containing previous turns.
2. The query is routed to the appropriate agent.
3. The agent processes the query with the historical context.
4. The response is returned to the frontend and simultaneously logged to Firestore for persistence.
