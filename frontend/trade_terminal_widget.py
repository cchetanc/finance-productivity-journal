"""
Trade Execution Terminal — shared widget.

Rendered directly on the home page (frontend/app.py), just below the "Live
Market Sentiment" panel, and also available as its own page
(pages/5_trade_terminal.py) for a direct link / full-screen view. Both call
render_trade_terminal() so there's exactly one implementation to maintain.

Themed to match the home page's existing warm dark palette (#15120e page bg,
#211b13 card bg, #332b1f border, #d3a94a gold accent, #e8ddc7 text,
#a99872 muted label) instead of a separate color scheme, so it reads as part
of the same page rather than a bolted-on widget.
"""
import datetime
import html

import requests
import streamlit as st

from auth_helper import auth_headers, get_id_token, login_widget, logout

ALGO_TYPES = ["ICEBERG", "TWAP", "VWAP", "MOMENTUM_SNIPER"]
ALGO_DESCRIPTIONS = {
    "ICEBERG": "Splits the order into small repeated clips so only a fraction is ever visible on the book.",
    "TWAP": "Equal-sized slices spread evenly across a time window.",
    "VWAP": "Slices weighted to follow the market's typical intraday volume curve.",
    "MOMENTUM_SNIPER": "Waits for price to cross a breakout trigger, then fires the full size fast with a stop-loss.",
}

_STYLE = """
<style>
/* card-like bordered containers -> same look as the Live Market Sentiment /
   Live Wire cards on the home page */
[data-testid="stVerticalBlockBorderWrapper"] {
    background: #211b13 !important;
    border: 1px solid #332b1f !important;
    border-radius: 8px !important;
}
input, textarea {
    background: #171310 !important;
    color: #e8ddc7 !important;
    border: 1px solid #332b1f !important;
    border-radius: 6px !important;
}
input:focus { border-color: #d3a94a !important; box-shadow: 0 0 0 1px #d3a94a !important; }
div[data-baseweb="select"] > div {
    background: #171310 !important;
    border-color: #332b1f !important;
    color: #e8ddc7 !important;
    border-radius: 6px !important;
}
div[data-baseweb="popover"] { background: #211b13 !important; }

.tt-section-label {
    font-size: 10px; font-weight: 700; letter-spacing: 1.5px; color: #a99872;
    text-transform: uppercase; margin: 0 0 5px 0; font-family: 'JetBrains Mono', monospace;
}
.tt-buy-marker + div [data-testid="stButton"] > button {
    background: rgba(143, 174, 100, 0.12) !important;
    border: 1.5px solid #8fae64 !important;
    color: #8fae64 !important;
    font-weight: 700 !important;
    font-family: 'JetBrains Mono', monospace !important;
    padding: 0.55rem 0 !important;
}
.tt-buy-marker + div [data-testid="stButton"] > button:hover { background: rgba(143, 174, 100, 0.22) !important; }
.tt-sell-marker + div [data-testid="stButton"] > button {
    background: rgba(193, 107, 87, 0.12) !important;
    border: 1.5px solid #c16b57 !important;
    color: #c16b57 !important;
    font-weight: 700 !important;
    font-family: 'JetBrains Mono', monospace !important;
    padding: 0.55rem 0 !important;
}
.tt-sell-marker + div [data-testid="stButton"] > button:hover { background: rgba(193, 107, 87, 0.22) !important; }

table.tt-log { width: 100%; border-collapse: collapse; font-size: 12.5px; }
table.tt-log th {
    text-align: left; color: #a99872; font-family: 'JetBrains Mono', monospace;
    font-size: 9.5px; letter-spacing: .6px; text-transform: uppercase;
    border-bottom: 1px solid #332b1f; padding: 7px 9px;
}
table.tt-log td { padding: 8px 9px; border-bottom: 1px solid #2a2216; color: #e8ddc7; }
table.tt-log tr:last-child td { border-bottom: none; }
.tt-buy-tag { color: #8fae64; font-weight: 700; }
.tt-sell-tag { color: #c16b57; font-weight: 700; }
.tt-muted { color: #7d6e50; }
</style>
"""


def _clean(v):
    return None if v in (0, 0.0) else v


def render_trade_terminal(backend_url: str, show_title: bool = True):
    """Renders the full Trade Execution Terminal: search + order entry +
    BUY/SELL + today's trade log. Call once per page render."""
    st.markdown(_STYLE, unsafe_allow_html=True)

    if show_title:
        hdr, btn = st.columns([6, 1])
        with hdr:
            st.markdown('<p class="tt-section-label">⚡ Trade Execution Terminal</p>', unsafe_allow_html=True)
        with btn:
            if st.button("↺", key="tt_refresh_top"):
                st.cache_data.clear()
                st.rerun()

    if not login_widget():
        return

    st.session_state.setdefault("tt_symbol", "RELIANCE-EQ")
    st.session_state.setdefault("tt_symbol_label", "RELIANCE · Reliance Industries Ltd.")
    st.session_state.setdefault("tt_exchange", "NSE")
    st.session_state.setdefault("tt_search_box", "RELIANCE")

    top1, top2 = st.columns([4, 1])
    with top1:
        st.caption(f"Signed in as {st.session_state.get('fb_email', '')} · Angel One")
    with top2:
        if st.button("Sign out", key="tt_signout"):
            logout()
            st.rerun()

    @st.cache_data(ttl=20)
    def fetch_broker_status():
        try:
            r = requests.get(f"{backend_url}/api/trading/broker/status", timeout=15)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    status = fetch_broker_status()
    if status and status.get("secret_manager_reachable"):
        st.caption("🟢 Angel One live credentials ready.")
    elif status:
        st.caption(f"🟡 Angel One live credentials not reachable yet — LIVE orders will fail. ({status.get('detail')})")
    else:
        st.caption("⚪ Couldn't reach broker status right now — paper trading still works.")

    live_toggle = st.checkbox("⚠️ Go LIVE — place real orders through Angel One (unchecked = paper trade)", value=False, key="tt_live_toggle")
    mode = "LIVE" if live_toggle else "PAPER"
    if live_toggle:
        st.warning("LIVE mode places real orders with real money through your Angel One account.")

    with st.container(border=True):
        row = st.columns([2.6, 1, 1.1, 1, 1])

        with row[0]:
            search_query = st.text_input(
                "Symbol", key="tt_search_box", label_visibility="collapsed",
                placeholder="Search e.g. RELIANCE",
            )
        with row[1]:
            exchange = st.selectbox(
                "Exchange", ["NSE", "BSE"], label_visibility="collapsed",
                index=["NSE", "BSE"].index(st.session_state["tt_exchange"]),
                key="tt_exchange_select",
            )
            st.session_state["tt_exchange"] = exchange
        with row[2]:
            qty = st.number_input("Shares", min_value=1, value=10, step=1, label_visibility="collapsed", key="tt_qty")

        @st.cache_data(ttl=30)
        def search_symbols(q: str):
            try:
                r = requests.get(
                    f"{backend_url}/api/screener/stocks",
                    params={"search": q, "page_size": 6}, timeout=10,
                )
                if r.status_code == 200:
                    return r.json().get("results", [])
            except Exception:
                pass
            return []

        query = (search_query or "").strip()
        is_fresh_search = query and query.upper() != st.session_state["tt_symbol"].split("-")[0]
        if is_fresh_search and len(query) >= 2:
            suggestions = search_symbols(query)
            if suggestions:
                with st.container(border=True):
                    for s in suggestions:
                        sym = s.get("symbol", "")
                        exch = (s.get("exchange") or "NSE").upper()
                        name = s.get("name", "")
                        sector = s.get("sector") or "Equity"
                        c1, c2, c3 = st.columns([1.2, 3, 1])
                        c1.markdown(f"**{html.escape(sym)}**")
                        c2.caption(f"{html.escape(name)} · {html.escape(sector)}")
                        if c3.button("Select", key=f"sel_{sym}_{exch}"):
                            trading_symbol = f"{sym}-EQ" if exch == "NSE" else sym
                            st.session_state["tt_symbol"] = trading_symbol
                            st.session_state["tt_symbol_label"] = f"{sym} · {name}"
                            st.session_state["tt_exchange"] = exch
                            st.session_state["tt_search_box"] = sym
                            st.rerun()

        st.caption(f"Selected: **{st.session_state['tt_symbol']}** ({st.session_state['tt_exchange']}) — {st.session_state['tt_symbol_label']}")

        st.write("")
        auto = st.checkbox("Auto — use an execution algorithm instead of a plain limit order", key="tt_auto")

        algo_type = None
        algo_params = {}
        limit_price = 0.0

        if auto:
            algo_type = st.selectbox("Strategy", ALGO_TYPES, key="tt_algo")
            st.caption(ALGO_DESCRIPTIONS[algo_type])
            pc = st.columns(3)
            if algo_type == "ICEBERG":
                algo_params["clip_size"] = pc[0].number_input("Clip size", min_value=1, value=max(1, int(qty) // 5), step=1)
                algo_params["price_limit"] = pc[1].number_input("Price limit (0 = none)", min_value=0.0, value=0.0, step=0.05)
                algo_params["randomize_timing"] = pc[2].checkbox("Randomize timing", value=True, key="ib_rand")
            elif algo_type in ("TWAP", "VWAP"):
                algo_params["duration_minutes"] = pc[0].number_input("Duration (min)", min_value=1, value=15, step=1)
                if algo_type == "TWAP":
                    algo_params["slice_count"] = pc[1].number_input("Slices", min_value=1, value=5, step=1)
                    algo_params["randomize_timing"] = pc[2].checkbox("Randomize timing", value=True, key="twap_rand")
            elif algo_type == "MOMENTUM_SNIPER":
                algo_params["breakout_price"] = pc[0].number_input("Breakout trigger", min_value=0.0, value=0.0, step=0.05)
                algo_params["stop_loss_price"] = pc[1].number_input("Stop-loss (0 = none)", min_value=0.0, value=0.0, step=0.05)
                algo_params["watch_timeout_minutes"] = pc[2].number_input("Give up after (min)", min_value=1, value=30, step=1)
        else:
            limit_price = st.number_input("Limit price (0 = market order)", min_value=0.0, value=0.0, step=0.05, key="tt_limit_price")

        st.write("")
        btn_row = st.columns(2)
        with btn_row[0]:
            st.markdown('<span class="tt-buy-marker"></span>', unsafe_allow_html=True)
            buy_clicked = st.button("BUY", use_container_width=True, key="btn_buy")
        with btn_row[1]:
            st.markdown('<span class="tt-sell-marker"></span>', unsafe_allow_html=True)
            sell_clicked = st.button("SELL", use_container_width=True, key="btn_sell")

    def build_request(side: str):
        if auto:
            body = {
                "algo_type": algo_type,
                "symbol": st.session_state["tt_symbol"],
                "exchange": st.session_state["tt_exchange"],
                "side": side,
                "total_quantity": int(qty),
                "mode": mode,
            }
            for k, v in algo_params.items():
                body[k] = _clean(v) if k in ("price_limit", "stop_loss_price", "breakout_price") else v
            return "algo", body
        else:
            body = {
                "symbol": st.session_state["tt_symbol"],
                "exchange": st.session_state["tt_exchange"],
                "side": side,
                "quantity": int(qty),
                "order_type": "LIMIT" if limit_price > 0 else "MARKET",
                "limit_price": _clean(limit_price),
                "mode": mode,
            }
            return "manual", body

    def execute(kind: str, body: dict):
        token = get_id_token()
        if not token:
            st.error("Not signed in.")
            return
        url = f"{backend_url}/api/trading/algo/start" if kind == "algo" else f"{backend_url}/api/trading/orders"
        try:
            r = requests.post(url, json=body, headers=auth_headers(), timeout=30)
            if r.status_code == 200:
                st.success(f"{body['side']} {'algo started' if kind == 'algo' else 'order placed'} for {body.get('symbol')}.")
                st.cache_data.clear()
                st.rerun()
            else:
                st.error(f"{r.status_code}: {r.text}")
        except Exception as e:
            st.error(f"Connection failed: {e}")

    if buy_clicked or sell_clicked:
        side = "BUY" if buy_clicked else "SELL"
        kind, body = build_request(side)
        if mode == "LIVE":
            st.session_state["_tt_pending_order"] = (kind, body)
        else:
            execute(kind, body)

    pending = st.session_state.get("_tt_pending_order")
    if pending:
        kind, body = pending
        st.error(
            f"Confirm: place a LIVE {body['side']} for {body.get('quantity') or body.get('total_quantity')} "
            f"{body.get('symbol')} via {'the ' + body['algo_type'] + ' algo' if kind == 'algo' else 'a plain order'}?"
        )
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("Yes, place it", type="primary", key="tt_confirm_yes"):
                st.session_state["_tt_pending_order"] = None
                execute(kind, body)
        with cc2:
            if st.button("Cancel", key="tt_confirm_no"):
                st.session_state["_tt_pending_order"] = None
                st.rerun()

    st.write("")

    log_header_col, log_refresh_col = st.columns([5, 1])
    with log_header_col:
        st.markdown('<p class="tt-section-label">🧾 Today\'s Trade Log</p>', unsafe_allow_html=True)
    with log_refresh_col:
        if st.button("↺ Refresh", key="tt_refresh_log"):
            st.cache_data.clear()
            st.rerun()

    def _fetch_list(path: str):
        try:
            r = requests.get(f"{backend_url}{path}", headers=auth_headers(), timeout=15)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    today_str = datetime.date.today().isoformat()
    log_rows = []

    for o in _fetch_list("/api/trading/orders"):
        ts = o.get("timestamp", "")
        if ts[:10] != today_str:
            continue
        reasoning = f"Manual · {o.get('order_type', '')}"
        if o.get("limit_price"):
            reasoning += f" @ ₹{o.get('limit_price')}"
        reasoning += f" · {o.get('status', '')}"
        log_rows.append({
            "time": ts, "symbol": o.get("symbol", ""), "side": o.get("side", ""),
            "exchange": o.get("exchange", "—"),
            "broker": "Angel One" if o.get("mode") == "LIVE" else "Paper",
            "reasoning": reasoning,
        })

    for a in _fetch_list("/api/trading/algo"):
        ts = a.get("created_at", "")
        if ts[:10] != today_str:
            continue
        reasoning = (
            f"Auto · {a.get('algo_type', '')} · {a.get('status', '')} "
            f"({a.get('total_filled', 0)}/{a.get('total_quantity', 0)} filled)"
        )
        log_rows.append({
            "time": ts, "symbol": a.get("symbol", ""), "side": a.get("side", ""),
            "exchange": a.get("exchange", "—"),
            "broker": "Angel One" if a.get("mode") == "LIVE" else "Paper",
            "reasoning": reasoning,
        })

    log_rows.sort(key=lambda r: r["time"], reverse=True)

    with st.container(border=True):
        if not log_rows:
            st.markdown('<p class="tt-muted">No trade logs found for today.</p>', unsafe_allow_html=True)
        else:
            rows_html = ""
            for r in log_rows:
                time_display = html.escape(r["time"][11:19] if len(r["time"]) > 10 else r["time"])
                side_class = "tt-buy-tag" if r["side"] == "BUY" else "tt-sell-tag"
                rows_html += (
                    "<tr>"
                    f"<td class='tt-muted'>{time_display}</td>"
                    f"<td><b>{html.escape(str(r['symbol']))}</b></td>"
                    f"<td class='{side_class}'>{html.escape(str(r['side']))}</td>"
                    f"<td>{html.escape(str(r['exchange']))}</td>"
                    f"<td>{html.escape(str(r['broker']))}</td>"
                    f"<td class='tt-muted'>{html.escape(r['reasoning'])}</td>"
                    "</tr>"
                )
            st.markdown(
                "<table class='tt-log'><thead><tr>"
                "<th>Time</th><th>Symbol</th><th>Action</th><th>Exchange</th><th>Broker</th><th>Reasoning</th>"
                f"</tr></thead><tbody>{rows_html}</tbody></table>",
                unsafe_allow_html=True,
            )

    with st.expander("Positions"):
        pos_mode = st.radio("Positions from", ["PAPER", "LIVE"], horizontal=True, key="tt_pos_mode")
        if st.button("Fetch positions", key="tt_fetch_positions"):
            try:
                r = requests.get(
                    f"{backend_url}/api/trading/positions",
                    params={"mode": pos_mode}, headers=auth_headers(), timeout=20,
                )
                if r.status_code == 200:
                    st.dataframe(r.json())
                else:
                    st.error(f"{r.status_code}: {r.text}")
            except Exception as e:
                st.error(f"Connection failed: {e}")