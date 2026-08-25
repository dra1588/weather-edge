"""
signal_misprice.py  —  Polymarket mispriced market scanner
Detects:
  1. Arithmetic arbitrage  — YES + NO price sum < ARITH_ARB_THRESHOLD
  2. Wide-spread markets   — bid/ask spread > SPREAD_THRESHOLD on either side
  3. Whale accumulation    — same wallet appearing on the same side recently

Drop this file in your repo root and call run() from your main bot.
Respects the same EXECUTE / DRY_RUN mode as other signal modules.
"""

import os
import time
import requests
from datetime import datetime, timezone

# ── Thresholds (tune these) ───────────────────────────────────────────────────
ARITH_ARB_THRESHOLD = 0.97      # YES + NO sum below this = arithmetic arb
SPREAD_THRESHOLD    = 0.06      # bid/ask spread > 6 % = potential info edge
EDGE_MIN_PCT        = 12.5      # minimum edge % to generate an alert (matches your other signals)
MAX_MARKETS_TO_SCAN = 200       # how many active markets to pull per run
WHALE_TRADE_LOOKBACK = 200      # recent trades to scan per market for whale pattern
WHALE_REPEAT_MIN    = 3         # same wallet ≥ this many trades on same side = whale signal
MAX_TRADE_SIZE_USD  = 50        # your existing safety cap
MAX_DAILY_USD       = 200       # your existing daily cap

# ── Polymarket endpoints ──────────────────────────────────────────────────────
CLOB_BASE   = "https://clob.polymarket.com"
DATA_BASE   = "https://data-api.polymarket.com"
GAMMA_BASE  = "https://gamma-api.polymarket.com"

HEADERS = {"User-Agent": "PolyArbBot/2.0"}


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching helpers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_active_markets(limit=MAX_MARKETS_TO_SCAN):
    """Return list of active binary markets from Gamma API."""
    try:
        r = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else r.json().get("markets", [])
    except Exception as e:
        print(f"[misprice] fetch_active_markets error: {e}")
        return []


def fetch_orderbook(token_id):
    """Return best bid/ask for a CLOB token."""
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        return best_bid, best_ask
    except Exception:
        return None, None


def fetch_recent_trades(token_id, limit=WHALE_TRADE_LOOKBACK):
    """Return recent trades for a token from the CLOB trade feed."""
    try:
        r = requests.get(
            f"{CLOB_BASE}/trades",
            params={"market": token_id, "limit": limit},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def detect_arith_arb(yes_ask, no_ask):
    """
    Arithmetic arbitrage: buy YES + buy NO.
    Profit if YES_ask + NO_ask < 1.00 (both resolve, one pays $1).
    Returns edge % or None.
    """
    if yes_ask is None or no_ask is None:
        return None
    total = yes_ask + no_ask
    if total < ARITH_ARB_THRESHOLD:
        edge = (1.0 - total) * 100
        return round(edge, 2)
    return None


def detect_spread_edge(bid, ask):
    """
    Wide spread signals thin liquidity — price is uncertain.
    If spread > SPREAD_THRESHOLD and mid is far from 0.5, the market may be mispriced.
    Returns (mid, spread_pct) or (None, None).
    """
    if bid is None or ask is None:
        return None, None
    spread = ask - bid
    mid = (bid + ask) / 2
    spread_pct = spread / mid if mid > 0 else 0
    if spread_pct > SPREAD_THRESHOLD:
        return round(mid, 4), round(spread_pct * 100, 2)
    return None, None


def detect_whale_accumulation(token_id):
    """
    Scan recent trades for a wallet appearing ≥ WHALE_REPEAT_MIN times on same side.
    Returns list of (wallet, side, count) tuples.
    """
    trades = fetch_recent_trades(token_id)
    taker_counts = {}  # {(maker_address, outcome): count}
    for t in trades:
        wallet = t.get("maker_address") or t.get("owner") or t.get("transactor")
        side   = t.get("side") or t.get("outcome")
        if wallet and side:
            key = (wallet, str(side).upper())
            taker_counts[key] = taker_counts.get(key, 0) + 1

    whales = [
        {"wallet": w, "side": s, "trade_count": c}
        for (w, s), c in taker_counts.items()
        if c >= WHALE_REPEAT_MIN
    ]
    return sorted(whales, key=lambda x: -x["trade_count"])


# ─────────────────────────────────────────────────────────────────────────────
# Main scanner
# ─────────────────────────────────────────────────────────────────────────────

def scan_markets():
    """
    Scan active markets and return a list of opportunity dicts.
    Each dict has: type, market, question, edge_pct, yes_ask, no_ask,
                   yes_token, no_token, whales, rationale
    """
    print(f"[misprice] Scanning up to {MAX_MARKETS_TO_SCAN} markets...")
    markets = fetch_active_markets()
    print(f"[misprice] Got {len(markets)} markets")

    opportunities = []

    for m in markets:
        question = m.get("question") or m.get("title") or "Unknown"
        tokens   = m.get("tokens") or m.get("clob_token_ids") or []

        # Normalise token list — Gamma returns list of dicts or list of strings
        if tokens and isinstance(tokens[0], dict):
            yes_token = next((t["token_id"] for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token  = next((t["token_id"] for t in tokens if t.get("outcome", "").upper() == "NO"),  None)
        elif len(tokens) >= 2:
            yes_token, no_token = tokens[0], tokens[1]
        else:
            continue

        if not yes_token or not no_token:
            continue

        yes_bid, yes_ask = fetch_orderbook(yes_token)
        no_bid,  no_ask  = fetch_orderbook(no_token)

        # 1 — Arithmetic arb
        arb_edge = detect_arith_arb(yes_ask, no_ask)
        if arb_edge and arb_edge >= EDGE_MIN_PCT:
            whales = detect_whale_accumulation(yes_token)
            opportunities.append({
                "type":      "ARITH_ARB",
                "market":    m.get("id") or m.get("market_slug", ""),
                "question":  question,
                "edge_pct":  arb_edge,
                "yes_ask":   yes_ask,
                "no_ask":    no_ask,
                "yes_token": yes_token,
                "no_token":  no_token,
                "whales":    whales,
                "rationale": f"YES {yes_ask:.2f} + NO {no_ask:.2f} = {yes_ask+no_ask:.3f} < 1.00",
            })
            continue  # no need to check spread on same market

        # 2 — Wide spread on YES side
        yes_mid, yes_spread = detect_spread_edge(yes_bid, yes_ask)
        if yes_mid and yes_spread:
            edge = abs(yes_mid - 0.5) * 100  # distance from fair 50c
            if edge >= EDGE_MIN_PCT:
                whales = detect_whale_accumulation(yes_token)
                opportunities.append({
                    "type":      "SPREAD_EDGE",
                    "market":    m.get("id") or m.get("market_slug", ""),
                    "question":  question,
                    "edge_pct":  round(edge, 2),
                    "yes_ask":   yes_ask,
                    "no_ask":    no_ask,
                    "yes_token": yes_token,
                    "no_token":  no_token,
                    "whales":    whales,
                    "rationale": f"YES spread {yes_spread:.1f}% (bid {yes_bid:.2f} / ask {yes_ask:.2f})",
                })

        # Small pause to avoid rate-limiting
        time.sleep(0.15)

    opportunities.sort(key=lambda x: -x["edge_pct"])
    print(f"[misprice] Found {len(opportunities)} opportunities above {EDGE_MIN_PCT}% edge")
    return opportunities


# ─────────────────────────────────────────────────────────────────────────────
# Telegram alerting
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[misprice] Telegram not configured — printing alert:\n", message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[misprice] Telegram error: {e}")


def format_alert(opp):
    emoji = "🔴" if opp["type"] == "ARITH_ARB" else "🟡"
    lines = [
        f"{emoji} <b>MISPRICE SIGNAL — {opp['type']}</b>",
        f"📋 {opp['question'][:80]}",
        f"📈 Edge: <b>{opp['edge_pct']:.1f}%</b>",
        f"💰 YES ask: {opp['yes_ask']:.3f}  |  NO ask: {opp['no_ask']:.3f}",
        f"📝 {opp['rationale']}",
    ]
    if opp["whales"]:
        top = opp["whales"][0]
        short = top["wallet"][:10] + "..." + top["wallet"][-6:] if len(top["wallet"]) > 20 else top["wallet"]
        lines.append(f"🐋 Whale: {short} ({top['trade_count']}x {top['side']})")
    lines.append(f"🔗 https://polymarket.com/event/{opp['market']}")
    lines.append(f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Optional: place trades via py-clob-client
# ─────────────────────────────────────────────────────────────────────────────

def maybe_trade(opp, client, daily_spent):
    """
    Place a trade if EXECUTE mode is on and safety limits allow.
    Returns amount spent (0 if skipped).
    """
    execute = os.environ.get("EXECUTE_TRADES", "false").lower() == "true"
    if not execute:
        print(f"[misprice] DRY RUN — would trade {opp['type']} on {opp['question'][:50]}")
        return 0

    if daily_spent >= MAX_DAILY_USD:
        print("[misprice] Daily limit reached — skipping trade")
        return 0

    amount = min(MAX_TRADE_SIZE_USD, MAX_DAILY_USD - daily_spent)

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        if opp["type"] == "ARITH_ARB":
            # Buy both sides
            for token_id, ask in [(opp["yes_token"], opp["yes_ask"]), (opp["no_token"], opp["no_ask"])]:
                size = round((amount / 2) / ask, 2)
                order_args = OrderArgs(
                    token_id=token_id,
                    price=round(ask + 0.01, 2),   # slight buffer
                    size=size,
                    side=BUY,
                    order_type=OrderType.GTC,
                )
                resp = client.create_and_post_order(order_args)
                print(f"[misprice] Placed order: {resp}")
        else:
            # SPREAD_EDGE — buy the direction further from 0.5
            if opp.get("yes_ask") and abs(opp["yes_ask"] - 0.5) > 0.1:
                direction = opp["yes_token"] if opp["yes_ask"] < 0.5 else opp["no_token"]
                price     = opp["yes_ask"] if direction == opp["yes_token"] else opp["no_ask"]
                size      = round(amount / price, 2)
                order_args = OrderArgs(
                    token_id=direction,
                    price=round(price + 0.01, 2),
                    size=size,
                    side=BUY,
                    order_type=OrderType.GTC,
                )
                resp = client.create_and_post_order(order_args)
                print(f"[misprice] Placed order: {resp}")

        return amount
    except Exception as e:
        print(f"[misprice] Trade error: {e}")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — call this from your main bot
# ─────────────────────────────────────────────────────────────────────────────

def run(client=None, daily_spent=0):
    """
    Main entry point.
    Pass in your py-clob-client `client` instance and running daily_spent total.
    Returns updated daily_spent.
    """
    print("\n[misprice] ── Starting mispriced market scan ──")
    opps = scan_markets()

    if not opps:
        print("[misprice] No opportunities found this run")
        return daily_spent

    # Send summary to Telegram
    summary = (
        f"🔍 <b>Misprice scan complete</b>\n"
        f"Found <b>{len(opps)}</b> opportunities\n"
        f"Top edge: <b>{opps[0]['edge_pct']:.1f}%</b> — {opps[0]['question'][:60]}"
    )
    send_telegram(summary)

    for opp in opps[:5]:   # alert top 5
        alert = format_alert(opp)
        send_telegram(alert)
        print(f"[misprice] Alert sent: {opp['type']} {opp['edge_pct']:.1f}% — {opp['question'][:50]}")

        if client:
            spent = maybe_trade(opp, client, daily_spent)
            daily_spent += spent

    return daily_spent


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
