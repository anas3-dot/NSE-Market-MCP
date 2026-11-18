#!/usr/bin/env python3
"""Poll Telegram messages and reply with NSE stock quotes."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from nse_mcp import call_tool

IST = ZoneInfo("Asia/Kolkata")
STATE = ROOT / "state" / "telegram-command-state.json"


def telegram(method, params=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    data = urllib.parse.urlencode(params or {}).encode() if params is not None else None
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=35) as res:
        result = json.loads(res.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed")
    return result.get("result")


async def nse(tool, args):
    result = await call_tool("cmmkt", tool, args)
    if result.get("is_error"):
        raise RuntimeError("NSE quote request failed")
    structured = result.get("structuredContent")
    if structured not in (None, "None", {}):
        return structured
    parsed = []
    for part in result.get("content", []):
        raw = part.get("text") if isinstance(part, dict) else None
        if raw:
            try:
                parsed.append(json.loads(raw))
            except json.JSONDecodeError:
                parsed.append(raw)
    return parsed[0] if len(parsed) == 1 else parsed


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def find_stock(query, payload):
    q = norm(query)
    rows = list(walk(payload))
    aliases = ("companyName", "company", "name", "securityName", "longName", "symbolName")
    symbols = ("symbol", "tradingSymbol", "securitySymbol")
    for row in rows:
        symbol = next((row.get(k) for k in symbols if row.get(k)), None)
        if not symbol:
            continue
        names = [row.get(k) for k in aliases if row.get(k)]
        if norm(symbol) == q or any(norm(name) == q for name in names):
            return str(symbol).strip(), (str(names[0]) if names else str(symbol))
    # Permit direct NSE symbols even if the list's naming schema changes.
    if re.fullmatch(r"[A-Z0-9&.-]{1,20}", query.strip().upper()):
        return query.strip().upper(), query.strip().upper()
    # Partial company-name match, only when it identifies one unique symbol.
    matches = {}
    for row in rows:
        symbol = next((row.get(k) for k in symbols if row.get(k)), None)
        if not symbol:
            continue
        names = [row.get(k) for k in aliases if row.get(k)]
        if any(q and (q in norm(name) or norm(name) in q) for name in names):
            matches[str(symbol).strip()] = str(names[0]) if names else str(symbol)
    if len(matches) == 1:
        symbol, name = next(iter(matches.items()))
        return symbol, name
    if len(matches) > 1:
        raise ValueError("I found more than one match. Please send the exact NSE symbol (for example, RELIANCE or TCS).")
    raise ValueError("I couldn't match that company in the available NSE list. Try its NSE symbol, such as RELIANCE, TCS or INFY.")


def pick_quote(payload):
    for row in walk(payload):
        symbol = next((row.get(k) for k in ("symbol", "tradingSymbol", "securitySymbol") if row.get(k)), None)
        price = next((row.get(k) for k in ("lastPrice", "last", "ltp", "price", "close") if row.get(k) is not None), None)
        if symbol or price is not None:
            return row
    return payload if isinstance(payload, dict) else {}


def fmt(value, digits=2):
    try:
        return f"{float(str(value).replace(',', '').replace('%', '').strip()):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else "—"


def quote_message(company, symbol, payload):
    row = pick_quote(payload)
    price = next((row.get(k) for k in ("lastPrice", "last", "ltp", "price", "close") if row.get(k) is not None), None)
    pct = next((row.get(k) for k in ("pChange", "percChange", "changePercent", "percentChange") if row.get(k) is not None), None)
    change = next((row.get(k) for k in ("change", "netChange") if row.get(k) is not None), None)
    prev = next((row.get(k) for k in ("previousClose", "prevClose") if row.get(k) is not None), None)
    open_price = next((row.get(k) for k in ("open", "openPrice") if row.get(k) is not None), None)
    high = next((row.get(k) for k in ("dayHigh", "high", "highPrice") if row.get(k) is not None), None)
    low = next((row.get(k) for k in ("dayLow", "low", "lowPrice") if row.get(k) is not None), None)
    volume = next((row.get(k) for k in ("totalTradedVolume", "volume", "quantityTraded") if row.get(k) is not None), None)
    stamp = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    if price is None:
        details = json.dumps(payload, ensure_ascii=False, default=str)
        return f"📊 {company} ({symbol})\n\nNSE returned data, but its quote format needs adjustment.\n{details[:2500]}\n\nChecked: {stamp}"
    trend = "▲" if pct is not None and float(str(pct).replace("%", "")) >= 0 else "▼"
    lines = [f"📊 {company}", f"NSE: {symbol}", "", f"Price: ₹{fmt(price)}"]
    if pct is not None:
        lines.append(f"Change: {trend} {fmt(pct)}%")
    if change is not None:
        lines.append(f"Change value: ₹{fmt(change)}")
    if prev is not None: lines.append(f"Previous close: ₹{fmt(prev)}")
    if open_price is not None: lines.append(f"Open: ₹{fmt(open_price)}")
    if high is not None: lines.append(f"Day high: ₹{fmt(high)}")
    if low is not None: lines.append(f"Day low: ₹{fmt(low)}")
    if volume is not None: lines.append(f"Volume: {volume}")
    lines += ["", f"Checked: {stamp}", "Source: NSE market data. May be delayed; not investment advice."]
    return "\n".join(lines)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = str(os.environ["TELEGRAM_CHAT_ID"]).strip()
    try:
        state = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        state = {"offset": 0}
    offset = int(state.get("offset", 0))
    updates = telegram("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])}) or []
    for update in updates:
        update_id = int(update.get("update_id", 0))
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if str(chat.get("id", "")) == chat_id and text:
            if text.startswith("/start") or text.startswith("/help"):
                telegram("sendMessage", {"chat_id": chat_id, "text": "👋 Send a company name or NSE symbol to get its latest available quote.\nExamples: RELIANCE, TCS, INFY, HDFC BANK"})
            else:
                query = text.lstrip("$#").strip()
                try:
                    equity = asyncio.run(nse("cm_get_equity_stocks", {"limit": 500, "symbolFilter": ""}))
                    symbol, company = find_stock(query, equity)
                    quote = asyncio.run(nse("cm_get_stock_quote", {"symbol": symbol}))
                    reply = quote_message(company, symbol, quote)
                except Exception as exc:
                    reply = f"⚠️ {str(exc)}"
                telegram("sendMessage", {"chat_id": chat_id, "text": reply[:4000], "disable_web_page_preview": "true"})
        offset = max(offset, update_id + 1)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"offset": offset}, indent=2) + "\n")


if __name__ == "__main__":
    main()
