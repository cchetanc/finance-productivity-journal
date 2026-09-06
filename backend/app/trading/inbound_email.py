import logging
import re
from typing import Optional

from ..gmail_spending import _get_gmail_client, send_gmail
from .services import get_engine
from .algo_selector import choose_execution_plan
from .broker_base import OrderSide

logger = logging.getLogger("trading.inbound_email")

async def poll_inbound_trades(uid: str):
    """
    Polls the authenticated user's Gmail for unread trade commands.
    Expected format in email subject or body: BUY 10 RELIANCE
    """
    client = _get_gmail_client(uid)
    if not client:
        logger.warning(f"No Gmail client available for user {uid}")
        return

    try:
        # Search for unread emails with "TRADE" in the subject
        results = client.users().messages().list(userId="me", q="subject:TRADE is:unread").execute()
        messages = results.get("messages", [])
        
        if not messages:
            logger.info(f"No new inbound trade emails for user {uid}")
            return

        engine = await get_engine(uid)

        for msg_info in messages:
            msg_id = msg_info["id"]
            thread_id = msg_info.get("threadId", msg_id)
            
            # Fetch the full message
            msg = client.users().messages().get(userId="me", id=msg_id, format="full").execute()
            
            # Parse subject and body to find trade command
            headers = msg.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "")
            
            # Extract plain text body if available
            body = _extract_body_text(msg.get("payload", {}))
            
            # Basic parsing: look for "BUY 10 TCS" or "SELL 50 RELIANCE"
            command_text = f"{subject} {body}".upper()
            match = re.search(r"\b(BUY|SELL)\s+(\d+)\s+([A-Z0-9.-]+)\b", command_text)
            
            if not match:
                logger.warning(f"Could not parse trade command from email {msg_id}: {command_text[:100]}")
                send_gmail(
                    uid=uid,
                    to="cchetanc@gmail.com",
                    subject="Re: " + subject,
                    html_body="<p>Failed to parse trade command. Use format: BUY 10 RELIANCE</p>",
                    thread_id=thread_id
                )
                _mark_as_read(client, msg_id)
                continue

            side_str, qty_str, symbol = match.groups()
            side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
            quantity = int(qty_str)

            logger.info(f"Parsed inbound trade: {side.value} {quantity} {symbol}")

            # Smart Routing
            plan = await choose_execution_plan(engine.broker, symbol, "NSE", side, quantity)
            
            response_body = f"<p>Received command: {side.value} {quantity} {symbol}</p>"
            
            try:
                if plan["type"] == "algo":
                    algo_exec = await engine.start(uid, plan["algo_type"], plan["params"])
                    response_body += f"<p>Smart routing selected <b>{plan['algo_type'].value}</b> strategy.</p>"
                    response_body += f"<p>Execution started with ID: {algo_exec.execution_id}</p>"
                else:
                    from .services import place_simple_order
                    res = await place_simple_order(
                        uid, symbol=symbol, exchange="NSE", side=side, quantity=quantity,
                        order_type=plan["order_type"], limit_price=plan.get("limit_price")
                    )
                    if res.get("ok"):
                        response_body += "<p>Order placed successfully via simple execution.</p>"
                    else:
                        response_body += f"<p>Failed to place order: {res.get('error')}</p>"
                        if res.get("insufficient_funds"):
                            response_body += f"<br>Required: {res.get('required')}, Available: {res.get('available')}"
            except Exception as e:
                response_body += f"<p>Error executing trade: {e}</p>"
                logger.error(f"Inbound trade execution failed: {e}")

            # Send reply
            send_gmail(
                uid=uid,
                to="cchetanc@gmail.com",
                subject="Re: " + subject,
                html_body=response_body,
                thread_id=thread_id
            )
            
            # Mark as read so we don't process it again
            _mark_as_read(client, msg_id)
            
    except Exception as e:
        logger.error(f"Error polling inbound trades for user {uid}: {e}")


def _extract_body_text(payload: dict) -> str:
    parts = payload.get("parts", [])
    if not parts:
        data = payload.get("body", {}).get("data")
        if data:
            import base64
            return base64.urlsafe_b64decode(data).decode("utf-8")
        return ""
        
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                import base64
                return base64.urlsafe_b64decode(data).decode("utf-8")
        elif part.get("parts"):
            return _extract_body_text(part)
    return ""


def _mark_as_read(client, msg_id: str):
    client.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()
