#!/usr/bin/env python3
"""NSE intraday snapshots -> personal Telegram alerts. Runs from GitHub Actions."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
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

TIMESTAMP_KEYS = (
    "timestamp", "timeStamp", "lastUpdateTime", "lastUpdatedTime",
    "lastUpdate", "updatedAt", "updateTime", "tradeTime",
    "tradedTime", "dataTime", "asOf", "asOfTime", "dateTime",
    "exchangeTimestamp", "marketTime",
)


def unpack(result):
    """Read either MCP structuredContent or its text content blocks."""
    if result.get("is_error"):
        raise RuntimeError("NSE MCP returned an error")

    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)) and structured:
        return structured
    if isinstance(structured, str) and structured.strip() not in ("", "None"):
        try:
            return json.loads(structured)
        except json.JSONDecodeError:
            pass

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


def data_timestamp(payload):
    for row in objects(payload):
        for key in TIMESTAMP_KEYS:
            value = row.get(key)
            if value not in (None, ""):
                text = str(value).strip()
                if text:
                    return text
    return None


def format_data_time(value):
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1] + " UTC"
    return text


def stocks_from(payload):
    found, seen = [], set()
    symbol_keys = (
        "symbol", "Symbol", "tradingSymbol", "securitySymbol",
        "symbolName", "ticker", "identifier",
    )
    ltp_keys = (
        "lastPrice", "last_price", "lastTradedPrice", "ltp",
        "LTP", "last", "price", "close", "closePrice",
    )
    pct_keys = (
        "pChange", "pchange", "perChange", "percChange",
        "changePercent", "percentChange", "percentageChange",
        "change_percent", "%change",
    )
    for row in objects(payload):
        symbol = next((str(row[k]).strip() for k in symbol_keys
                       if row.get(k) not in (None, "")), None)
        if not symbol or symbol in seen:
            continue
        ltp = number(row, ltp_keys)
        pct = number(row, pct_keys)
        if pct is None:
            change = number(row, ("change", "netChange", "changeValue"))
            prev = number(row, ("previousClose", "prevClose", "previous_close", "previousClosingPrice"))
            if change is not None and prev:
                pct = change / prev * 100
        if pct is None or ltp is None:
            continue
        seen.add(symbol)
        found.append({"symbol": symbol, "ltp": ltp, "pct": pct})
    return found


def nifty_from(payload):
    for row in objects(payload):
        name = str(row.get("indexName", row.get("index", row.get("name", "")))).upper()
        if name == "NIFTY 50":
            return {
                "last": number(row, ("last", "lastPrice", "ltp", "value", "currentValue")),
                "pct": number(row, ("percChange", "pChange", "changePercent", "percentChange")),
            }
    # Some MCP responses return the requested index quote as a single object
    # without repeating the index name.
    for row in objects(payload):
        last = number(row, ("last", "lastPrice", "ltp", "value", "currentValue"))
        pct = number(row, ("percChange", "pChange", "changePercent", "percentChange"))
        if last is not None and pct is not None:
            return {"last": last, "pct": pct}
    return {}



def fetch_watchlist():
    url = os.environ.get("WATCHLIST_URL", "").strip()
    key = os.environ.get("WATCHLIST_ACCESS_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing WATCHLIST_URL or WATCHLIST_ACCESS_KEY GitHub secret.")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Show the endpoint's response without ever logging request headers or secrets.
        body = exc.read(500).decode("utf-8", errors="replace").replace("\\n", " ")
        raise RuntimeError(
            f"Watchlist endpoint returned HTTP {exc.code}; response: {body[:500]}"
        ) from None
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    return {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}


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
    if os.environ.get("TELEGRAM_TEST_ONLY", "false").lower() == "true":
        telegram("✅ Telegram connection successful! Your NSE stock-alert bot is connected.")
        print("Telegram test message sent successfully.")
        return

    now = datetime.now(IST)
    trial_scan = os.environ.get("TELEGRAM_TRIAL_SCAN", "false").lower() == "true"
    if not trial_scan and (now.weekday() >= 5 or not (9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 60 + 30)):
        print("Outside NSE cash-market hours; no scan performed.")
        return

    day = now.strftime("%Y-%m-%d")
    equity_data, nifty_data, movers_data = asyncio.run(fetch_all())
    watchlist = fetch_watchlist()
    all_stocks = stocks_from(equity_data)
    stocks = [stock for stock in all_stocks if stock["symbol"].upper() in watchlist]
    print(f"DEBUG watchlist symbols: {sorted(watchlist)}")
    sample_symbols = [stock["symbol"] for stock in all_stocks[:20]]
    print(f"DEBUG sample NSE symbols: {sample_symbols}")
    nifty = nifty_from(nifty_data)
    source_time = format_data_time(
        data_timestamp(equity_data) or data_timestamp(nifty_data) or data_timestamp(movers_data)
    )

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
        heading = "🧪 NSE TRIAL SNAPSHOT (latest available data)" if trial_scan else "📈 NSE STOCK ALERTS"
        timestamp_line = (
            f"🕒 Stock data updated: {source_time}" if source_time
            else "🕒 Stock data updated: timestamp not provided by NSE/MCP"
        )
        telegram(heading + "\n" + timestamp_line + "\n\n" + "\n".join(messages[:45]))
        print(f"Telegram sent: {min(len(messages), 45)} alert(s).")
    else:
        print("Scan completed. No new threshold crossings.")

    print(f"Usable equity rows: {len(all_stocks)}")
    print(f"Watchlist symbols: {len(watchlist)}; matched: {len(stocks)}")
    print(f"NIFTY 50 snapshot: {nifty}")
    print("Only watchlist stocks are checked for price alerts. Unusual-volume alerts are not enabled yet.")
    print(f"Source data timestamp: {source_time}" if source_time else
          "NSE/MCP did not provide a source timestamp in the returned payload.")


if __name__ == "__main__":
    main()
