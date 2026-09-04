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
import textwrap

import requests
import streamlit as st

from auth_helper import auth_headers, get_id_token, login_widget

ALGO_TYPES = ["ICEBERG", "TWAP", "VWAP", "MOMENTUM_SNIPER"]

# Each strategy gets a one-line "what it is" (kept for the inline caption under the
# selector) plus a fuller "what" / "when to use" pair (shown in the reference panel
# below, and usable anywhere a longer explanation is needed).
ALGO_DESCRIPTIONS = {
    "ICEBERG": "Splits the order into small repeated clips so only a fraction is ever visible on the book.",
    "TWAP": "Equal-sized slices spread evenly across a time window.",
    "VWAP": "Slices weighted to follow the market's typical intraday volume curve.",
    "MOMENTUM_SNIPER": "Waits for price to cross a breakout trigger, then fires the full size fast with a stop-loss.",
}

ALGO_DETAILS = {
    "ICEBERG": {
        "label": "Iceberg",
        "what": (
            "Only a small \"tip\" of the total order — one clip at a time — is ever shown on the "
            "order book. As each visible clip fills, the next one is released, so the market never "
            "sees your full size at once (like an iceberg, where most of it sits below the surface)."
        ),
        "when": (
            "Use it for a large order in a stock with modest liquidity, where revealing the full "
            "quantity up front would tip off other traders and move the price against you before "
            "you're filled. Not needed for small orders or already-deep, highly liquid names."
        ),
    },
    "TWAP": {
        "label": "TWAP — Time-Weighted Average Price",
        "what": (
            "Breaks the order into equal-sized slices and fires them at even intervals across a "
            "fixed time window, regardless of how much volume is trading at any given moment. "
            "The goal is to average in/out steadily over that window."
        ),
        "when": (
            "Use it when you want a simple, predictable execution schedule and volume is fairly "
            "steady through the day (or you specifically don't want to chase volume spikes). Less "
            "ideal in a stock with a very lopsided volume curve, since VWAP will track the market "
            "better in that case."
        ),
    },
    "VWAP": {
        "label": "VWAP — Volume-Weighted Average Price",
        "what": (
            "Slices the order to follow the stock's typical intraday volume curve — bigger clips "
            "during high-volume periods (like the open and close), smaller clips during the quiet "
            "midday lull — so your execution blends in with the market's natural rhythm."
        ),
        "when": (
            "Use it for larger orders where the goal is to get filled close to the day's true "
            "volume-weighted average price with minimal market impact — the standard choice for "
            "institutional-style execution. Preferred over TWAP whenever the stock's volume is "
            "concentrated around the open/close rather than flat through the day."
        ),
    },
    "MOMENTUM_SNIPER": {
        "label": "Momentum Sniper",
        "what": (
            "Sits and waits until the price crosses a breakout trigger you set, then immediately "
            "fires the entire order at once (not sliced) along with an attached stop-loss, rather "
            "than executing gradually like the other three strategies."
        ),
        "when": (
            "Use it for a breakout/momentum trade where you want to enter fast the instant a level "
            "is crossed and speed matters more than minimizing market impact. Not a fit for large "
            "orders in illiquid names, where firing the full size at once can cause slippage — "
            "Iceberg or VWAP are the safer choice there."
        ),
    },
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

    if not login_widget(backend_url):
        return

    st.session_state.setdefault("tt_symbol", "RELIANCE-EQ")
    st.session_state.setdefault("tt_symbol_label", "RELIANCE · Reliance Industries Ltd.")
    st.session_state.setdefault("tt_exchange", "NSE")
    st.session_state.setdefault("tt_search_box", "RELIANCE")

    st.caption("Angel One")

    @st.cache_data(ttl=20)
    def fetch_broker_config():
        try:
            r = requests.get(f"{backend_url}/api/trading/config/angel-one", headers=auth_headers(), timeout=15)
            return r.json().get("configured", False) if r.status_code == 200 else False
        except Exception:
            return False

    is_configured = fetch_broker_config()

    if not is_configured:
        st.warning("⚠️ Trade Terminal requires Angel One configuration.")
        st.info("The trade terminal connects securely to your Angel One account. Please provide your API credentials below. These are encrypted and stored in your profile.")
        with st.form("angel_one_config_form"):
            api_key = st.text_input("API Key (from SmartAPI)", type="password")
            client_code = st.text_input("Client ID")
            pin = st.text_input("Secret PIN", type="password")
            totp_secret = st.text_input("TOTP Secret", type="password")
            if st.form_submit_button("Save Credentials"):
                if not all([api_key, client_code, pin, totp_secret]):
                    st.error("All fields are required.")
                else:
                    try:
                        r = requests.post(
                            f"{backend_url}/api/trading/config/angel-one",
                            headers=auth_headers(),
                            json={
                                "api_key": api_key.strip(),
                                "client_code": client_code.strip(),
                                "pin": pin.strip(),
                                "totp_secret": totp_secret.strip()
                            },
                            timeout=15
                        )
                        if r.status_code == 200:
                            st.success("Credentials saved securely.")
                            fetch_broker_config.clear()
                            st.rerun()
                        else:
                            st.error(f"Failed to save credentials: {r.text}")
                    except Exception as e:
                        st.error(f"Error connecting to backend: {e}")
        return

    live_toggle = st.checkbox("⚠️ Go LIVE — place real orders through Angel One (unchecked = paper trade)", value=False, key="tt_live_toggle")
    mode = "LIVE" if live_toggle else "PAPER"
    if live_toggle:
        st.warning("LIVE mode places real orders with real money through your Angel One account.")

    # ── Wallet balance ──────────────────────────────────────────────────
    # Available cash for the CURRENT mode — the paper simulator's virtual
    # balance, or (mode=LIVE) the real Angel One account's available margin,
    # read live via SmartAPI's rmsLimit(). Short TTL (not zero) so switching
    # between PAPER/LIVE or refreshing after a fill shows an up-to-date
    # number without hammering the broker on every rerun.
    @st.cache_data(ttl=15)
    def fetch_funds(_mode: str):
        try:
            r = requests.get(f"{backend_url}/api/trading/funds", params={"mode": _mode},
                              headers=auth_headers(), timeout=15)
            return r.json() if r.status_code == 200 else {"ok": False, "error": r.text}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    wc1, wc2 = st.columns([5, 1])
    with wc1:
        _funds = fetch_funds(mode)
        if _funds.get("ok"):
            _net_html = (
                f' <span style="color:#a99872;font-size:11.5px;">(net ₹{_funds["net"]:,.2f})</span>'
                if _funds.get("net") is not None else ""
            )
            st.markdown(
                f'<div style="padding:10px 14px;border:1px solid #332b1f;border-radius:8px;'
                f'background:#1a1610;font-size:13px;color:#e8ddc7;">'
                f'💰 <span style="color:#a99872;">{html.escape(_funds.get("broker") or mode)} balance:</span> '
                f'<span style="font-weight:800;color:#d3a94a;">₹{_funds.get("available_cash", 0):,.2f}</span> available'
                f'{_net_html}'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.warning(f"⚠️ Couldn't fetch {mode} wallet balance: {_funds.get('error', 'unknown error')}")
    with wc2:
        if st.button("↺", key="tt_refresh_funds", help="Refresh balance"):
            fetch_funds.clear()
            st.rerun()

    with st.container(border=True):
        row = st.columns([2.6, 1, 1.1, 1, 1])

        from st_keyup import st_keyup
        
        if "tt_search_version" not in st.session_state:
            st.session_state["tt_search_version"] = 0
            
        with row[0]:
            search_query = st_keyup(
                "Symbol", value=st.session_state.get("tt_search_value", ""), 
                key=f"tt_search_box_{st.session_state['tt_search_version']}", 
                label_visibility="collapsed", debounce=300, placeholder="Search e.g. RELIANCE"
            )
            
        if search_query and search_query != st.session_state.get("tt_last_picked"):
            # Import dynamically to avoid circular import if any
            from ui_helpers import predictive_search
            _picked = predictive_search(backend_url, "equity", search_query, key_prefix="tt")
            if _picked:
                st.session_state["tt_search_value"] = _picked[0]
                st.session_state["tt_last_picked"] = _picked[0]
                
                # Try to parse symbol and exchange from sub
                sym = _picked[0]
                exch = "NSE"
                if len(_picked) > 1 and "·" in _picked[1]:
                    parts = _picked[1].split("·")
                    if len(parts) > 1:
                        exch = parts[1].strip()
                        
                trading_symbol = f"{sym}-EQ" if exch == "NSE" else sym
                st.session_state["tt_symbol"] = trading_symbol
                st.session_state["tt_symbol_label"] = f"{sym} · {exch}"
                st.session_state["tt_exchange"] = exch
                
                st.session_state["tt_search_version"] += 1
                st.rerun()

        with row[1]:
            exchange = st.selectbox(
                "Exchange", ["NSE", "BSE"], label_visibility="collapsed",
                index=["NSE", "BSE"].index(st.session_state["tt_exchange"]),
                key="tt_exchange_select",
            )
            st.session_state["tt_exchange"] = exchange
            
        with row[2]:
            qty_val = st.number_input("Shares", min_value=1, value=10, step=1, label_visibility="collapsed", key="tt_qty_input")
        
        with row[3]:
            amt_val = st.number_input("Amount (₹)", min_value=0.0, value=0.0, step=1000.0, label_visibility="collapsed", key="tt_amt_input", help="If > 0, overrides Shares by calculating quantity from live price")

        st.caption(f"Selected: **{st.session_state['tt_symbol']}** ({st.session_state['tt_exchange']}) — {st.session_state['tt_symbol_label']}")

        @st.cache_data(ttl=60)
        def fetch_snapshot(ticker: str):
            try:
                base_ticker = ticker.split("-")[0]
                if st.session_state['tt_exchange'] == "NSE":
                    base_ticker += ".NS"
                elif st.session_state['tt_exchange'] == "BSE":
                    base_ticker += ".BO"
                r = requests.get(f"{backend_url}/api/screener/snapshot", params={"ticker": base_ticker}, timeout=10)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            return None

        snapshot = fetch_snapshot(st.session_state['tt_symbol'])
        if snapshot and not snapshot.get("error"):
            with st.container(border=True):
                st.markdown('<p class="tt-section-label" style="margin-bottom:8px;">Live Fundamentals</p>', unsafe_allow_html=True)
                mcap = snapshot.get("marketCap")
                mcap_str = f"₹{round(mcap/1e7)} Cr" if mcap else "—"
                pe = snapshot.get("trailingPE")
                pe_str = f"{round(pe, 2)}" if pe else "—"
                price = snapshot.get("currentPrice") or "—"
                chg = snapshot.get("dayChangePct")
                chg_str = f"{chg}%" if chg is not None else "—"
                chg_color = "#8fae64" if chg and chg >= 0 else "#c16b57"
                
                # textwrap.dedent() strips the leading indentation this triple-quoted string picks up from
                # Python's own code formatting. Without it, Streamlit's markdown renderer treats the indented
                # lines as a Markdown "indented code block" and prints the raw <div> tags as visible text
                # instead of rendering them as HTML. (Safe to dedent the whole f-string here, unlike the chat
                # card in app.py, because every interpolated value above — price, pe_str, mcap_str, etc. — is
                # a single-line value, so it can never introduce a stray zero-indent line.)
                html_block = textwrap.dedent(f"""\
                <div style="display:flex;gap:20px;font-size:12px;color:#a99872;flex-wrap:wrap;">
                    <div>Price: <span style="color:#e8ddc7;font-weight:600;">₹{price}</span> <span style="color:{chg_color};font-size:11px;">({chg_str})</span></div>
                    <div>P/E: <span style="color:#e8ddc7;font-weight:600;">{pe_str}</span></div>
                    <div>Mkt Cap: <span style="color:#e8ddc7;font-weight:600;">{mcap_str}</span></div>
                    <div>52w High: <span style="color:#e8ddc7;font-weight:600;">₹{snapshot.get('week52High') or '—'}</span></div>
                    <div>52w Low: <span style="color:#e8ddc7;font-weight:600;">₹{snapshot.get('week52Low') or '—'}</span></div>
                </div>
                """)
                st.markdown(html_block, unsafe_allow_html=True)

        st.write("")
        
        calculated_qty = int(qty_val)
        if amt_val > 0 and isinstance(price, (int, float)) and price > 0:
            calculated_qty = int(amt_val // float(price))
            if calculated_qty < 1: calculated_qty = 1
            st.info(f"Amount ₹{amt_val} implies {calculated_qty} shares at live price ₹{price}.")
            
        auto = st.checkbox("Auto — use an execution algorithm instead of a plain limit order", key="tt_auto")

        # Reference panel for what each strategy is and when to use it — kept visible regardless of
        # whether "Auto" is ticked, since someone deciding whether to turn Auto on at all needs this too.
        with st.expander("ℹ️ What are Iceberg / TWAP / VWAP / Momentum Sniper — and when to use each", expanded=False):
            for _algo in ALGO_TYPES:
                _d = ALGO_DETAILS[_algo]
                st.markdown(f"**{_d['label']}**")
                st.markdown(f"- What it is: {_d['what']}")
                st.markdown(f"- When to use it: {_d['when']}")
                if _algo != ALGO_TYPES[-1]:
                    st.markdown("---")

        algo_type = None
        algo_params = {}
        limit_price = 0.0

        if auto:
            algo_type = st.selectbox("Strategy", ALGO_TYPES, key="tt_algo")

            _detail = ALGO_DETAILS[algo_type]
            st.markdown(
                textwrap.dedent(f"""\
                <div style="background:#1a1610;border:1px solid #332b1f;border-left:3px solid #d3a94a;
                            border-radius:6px;padding:10px 14px;margin:4px 0 12px 0;font-size:12.5px;
                            line-height:1.55;color:#c9bd9e;">
                    <div style="font-weight:700;color:#e8ddc7;margin-bottom:4px;">{html.escape(_detail['label'])}</div>
                    <div><span style="color:#a99872;font-weight:600;">What it is: </span>{html.escape(_detail['what'])}</div>
                    <div style="margin-top:4px;"><span style="color:#a99872;font-weight:600;">When to use it: </span>{html.escape(_detail['when'])}</div>
                </div>
                """),
                unsafe_allow_html=True,
            )

            pc = st.columns(3)
            if algo_type == "ICEBERG":
                algo_params["clip_size"] = pc[0].number_input("Clip size", min_value=1, value=max(1, calculated_qty // 5), step=1)
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
                "total_quantity": calculated_qty,
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
                "quantity": calculated_qty,
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
                fetch_funds.clear()
                st.cache_data.clear()
                st.rerun()
            else:
                # The backend's InsufficientFundsError message always contains "Insufficient balance"
                # (see broker_base.py) — flagged as its own distinct warning here rather than folded
                # into the generic error text, so it's unmistakable rather than easy to miss.
                _detail = r.text
                try:
                    _detail = r.json().get("detail", r.text)
                except Exception:
                    pass
                if "insufficient balance" in str(_detail).lower():
                    st.warning(f"⚠️ Order not placed — insufficient balance. {_detail}")
                else:
                    st.error(f"{r.status_code}: {_detail}")
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