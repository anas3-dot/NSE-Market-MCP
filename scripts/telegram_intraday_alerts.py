#!/usr/bin/env python3
"""NSE intraday snapshots -> personal Telegram alerts. Runs from GitHub Actions."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from nse_mcp import call_tool  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = ROOT / "state" / "telegram-alert-state.json"
LEVELS = (1.0, 2.0, 3.0, -1.0, -2.0, -3.0)


def unpack(result):
    if result.get("is_error"):
        raise RuntimeError("NSE MCP returned an error")
    values = []
    for item in result.get("content", []):
        raw = item.get("text") if isinstance(item, dict) else None
        if raw:
            try:
                values.append(json.loads(raw))
            except json.JSONDecodeError:
                values.append(raw)
    return values[0] if len(values) == 1 else values


async def nse(tool, args):
    return unpack(await call_tool("cmmkt", tool, args))


async def fetch_all():
    return await asyncio.gather(
        nse("cm_get_equity_stocks", {"limit": 500, "symbolFilter": ""}),
        nse("cm_get_index_quote", {"indexName": "NIFTY 50"}),
        nse("nse_get_market_movers", {"indexName": "", "limit": 10}),
    )


def objects(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from objects(v)
    elif isinstance(value, list):
        for v in value:
            yield from objects(v)


def number(row, names):
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        try:
            return float(str(value).replace(",", "").replace("%", "").strip())
        except (ValueError, TypeError):
            continue
    return None


def stocks_from(payload):
    found, seen = [], set()
    for row in objects(payload):
        symbol = next((str(row[k]).strip() for k in
                       ("symbol", "tradingSymbol", "securitySymbol", "symbolName")
                       if row.get(k)), None)
        if not symbol or symbol in seen:
            continue
        ltp = number(row, ("lastPrice", "ltp", "last", "price", "close"))
        pct = number(row, ("pChange", "percChange", "changePercent", "percentChange"))
        if pct is None:
            change = number(row, ("change",))
            prev = number(row, ("previousClose", "prevClose", "previous_close"))
            if change is not None and prev:
                pct = change / prev * 100
        if pct is None or ltp is None:
            continue
        seen.add(symbol)
        found.append({"symbol": symbol, "ltp": ltp, "pct": pct})
    return found


def nifty_from(payload):
    for row in objects(payload):
        if str(row.get("indexName", "")).upper() == "NIFTY 50":
            return {
                "last": number(row, ("last", "lastPrice", "ltp", "value")),
                "pct": number(row, ("percChange", "pChange", "changePercent")),
            }
    return {}


def load_state(day):
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if state.get("date") != day:
        state = {"date": day, "previous_pct": {}, "nifty_alerted": False}
    state.setdefault("previous_pct", {})
    return state


def crossed(previous, current, level):
    if previous is None:
        return current >= level if level > 0 else current <= level
    return previous < level <= current if level > 0 else previous > level >= current


def telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub secret.")
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": message, "disable_web_page_preview": "true"
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError("Telegram API did not accept the message.")


def main():
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 60 + 30):
        print("Outside NSE cash-market hours; no scan performed.")
        return

    day = now.strftime("%Y-%m-%d")
    equity_data, nifty_data, movers_data = asyncio.run(fetch_all())
    stocks = stocks_from(equity_data)
    nifty = nifty_from(nifty_data)
    if not stocks and nifty.get("pct") is None:
        raise RuntimeError("No usable NSE data received; no Telegram alert sent.")

    state = load_state(day)
    previous = state["previous_pct"]
    messages = []

    for stock in stocks:
        symbol, pct = stock["symbol"], stock["pct"]
        old = previous.get(symbol)
        for level in LEVELS:
            if crossed(old, pct, level):
                arrow = "▲" if level > 0 else "▼"
                messages.append(f"{arrow} {symbol} crossed {level:+g}% | Now {pct:+.2f}% | ₹{stock['ltp']:.2f}")
        previous[symbol] = round(pct, 4)

    if nifty.get("pct") is not None and abs(nifty["pct"]) >= 0.5 and not state.get("nifty_alerted"):
        messages.append(f"📊 NIFTY 50: {nifty.get('last') or 0:,.2f} ({nifty['pct']:+.2f}%)")
        state["nifty_alerted"] = True

    state["previous_pct"] = previous
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    if messages:
        stamp = now.strftime("%d %b %Y, %I:%M %p IST")
        telegram("📈 NSE STOCK ALERTS | " + stamp + "\n\n" + "\n".join(messages[:45]))
        print(f"Telegram sent: {min(len(messages), 45)} alert(s).")
    else:
        print("Scan completed. No new threshold crossings.")

    print(f"Usable equity rows: {len(stocks)}")
    print(f"NIFTY 50 snapshot: {nifty}")
    print("Top gainers/losers payload retrieved for validation; unusual-volume alerts are not enabled yet.")
    print("NSE data may be delayed; check the exchange timestamp before acting.")


if __name__ == "__main__":
    main()
