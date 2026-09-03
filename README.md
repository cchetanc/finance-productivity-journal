# finance-productivity-journal
# Changes made

## 1. "Always Consulting the CFA desk" — UI bug, not a routing bug
`backend/app/agents.py`'s `Orchestrator` already routed movie/leisure questions
correctly to `CINEMA`/`LEISURE`, never `EQUITY`. The actual problem was
`frontend/app.py` hardcoding `st.spinner("Consulting the CFA desk...")` for
every single query.

Fixed:
- Spinner text is now generic ("Routing your question to the right specialist...").
- `Orchestrator.process_query_async` now returns `route_meta["agent_labels"]`
  (e.g. `["Cinema Desk"]`, `["Leisure Concierge"]`) — see `AGENT_DISPLAY_NAMES`
  in `agents.py`.
- The frontend renders that label above each assistant bubble, so you can see
  which desk actually answered.

## 2. Daily Productivity Assistant creativity
`leisure_agent`'s instruction in `agents.py` told it to give "ONLY what was
asked... no elaboration" — that's why it read flat. Added explicit tone
guidance (vivid, specific language; varied openers; personality) while
keeping the no-hallucinated-showtimes/no-invented-seat-count rules intact.
Also replaced the single fixed proactive greeting with a rotating pool
(`_WEEKEND_GREETINGS` / `_WEEKDAY_GREETINGS` in `frontend/app.py`).

## 3. BSE/NSE company analytics (tables + charts)
Already mostly built: `get_stock_snapshot` / `get_peer_comparison` tools
already render an inline SVG price chart, key-metrics card, and a peer
comparison table (see `frontend/app.py` around the stock-snapshot card).
Tightened `equity_agent`'s instruction so phrasings like "how does X listed
on BSE/NSE look" reliably trigger `get_stock_snapshot`.

## 4. Movie tickets — cost & availability
No public API exposes real seat/ticket counts (BookMyShow etc. don't have
one, and scraping their site would violate ToS) — the existing code already
correctly refuses to invent that number rather than guessing. Extended the
showtimes table format to include a **Ticket Price** column, populated only
from live web-search context when a real price is found, otherwise "—".

## 5. Gmail UPI spending analysis (new)
New files:
- `backend/app/gmail_spending.py` — OAuth flow (Gmail `readonly` scope),
  message parsing for UPI/bank debit alerts, monthly aggregation. Stores
  data per-user under `/users/{uid}/integrations/gmail` and
  `/users/{uid}/upi_transactions` in Firestore (same per-tenant pattern as
  the rest of the app).
- `backend/app/routers/gmail.py` — `/api/gmail/{status,auth-url,
  oauth-callback,sync,disconnect,spending-summary}` endpoints.
- New `spending_agent` in `agents.py`, routed via a new `SPENDING` domain in
  the router prompt, wired to a new `get_upi_spending_summary` tool in
  `tools_impls.py`.
- `uid` now flows: Firebase ID token (already sent by the frontend for every
  other authenticated call) → optionally verified in `/api/market/voice` →
  `CFAMultiAgentBot.process_query(uid=...)` → `Orchestrator.process_query_async
  (uid=...)` → a `contextvars.ContextVar` scoped to that request's asyncio
  Task. The uid is **never** an LLM-controllable function-call argument —
  the model can't choose whose Gmail gets read.
- Frontend: a "🔗 Connect Gmail for spending insights" expander inside the
  assistant panel (connect / refresh / disconnect), and the voice call now
  sends the user's Firebase ID token.

### Setup required (not code — GCP config) before Gmail spending works:
1. Enable the **Gmail API** in the same GCP project.
2. Create an **OAuth 2.0 Client ID** (type: Web application) under
   "APIs & Services > Credentials", and add your deployed backend's
   `/api/gmail/oauth-callback` URL as an authorized redirect URI.
3. Store the client id/secret as Secret Manager secrets
   `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` (same pattern as
   every other secret in `secrets.py`).
4. Set `GMAIL_OAUTH_REDIRECT_URI` and `FRONTEND_URL` env vars on the backend
   if they differ from the localhost defaults in `routers/gmail.py`.
5. `pip install -r backend/requirements.txt` — added `google-auth`,
   `google-auth-oauthlib`, `google-api-python-client`.
6. Deploy Firestore security rules (`config/firestore.rules`) that keep
   `/users/{uid}/integrations/gmail` and `/users/{uid}/upi_transactions`
   readable/writable only by that uid (or backend service account) — this
   was not modified here and should be checked against the new paths.

### Known limitations to be upfront about
- UPI/bank email parsing is regex-based and tuned for common phrasings
  ("debited", "paid to", "Rs./INR <amount>"). It will miss banks/apps with
  very different wording — extend `_SENDER_HINTS` / the regexes in
  `gmail_spending.py` as you see real emails it's missing.
- It only sees spending that generates an email alert — cash and
  non-notified payments won't show up. `spending_agent`'s instruction
  already tells it to disclose this as a floor, not a complete picture.
- The optional-auth pattern on `/api/market/voice` (`_resolve_uid_optional`)
  is deliberately lenient so the rest of the chat keeps working without
  login; only `get_upi_spending_summary` actually needs a real uid.