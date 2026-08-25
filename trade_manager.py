"""
trade_manager.py  —  Auto-trading with Take Profit / Stop Loss
Works with Manifold arb, weather edge, crypto edge, and misprice signals.

Positions are saved to positions.json in the repo root.
The workflow commits this file back to GitHub after each run
so state persists between hourly cron runs.

Flow each run:
  1. check_exits()  — scan open positions, close any at TP or SL
  2. enter_trades() — open new positions from fresh signals
  3. save_positions()
"""

import os
import json
import time
import requests
from datetime import datetime, timezone

# ── Trade settings ────────────────────────────────────────────────────────────
TAKE_PROFIT_PCT   = 3.00    # exit when position up 300%
STOP_LOSS_PCT     = 0.15    # exit when position down 15%
MAX_TRADE_USD     = 50      # max per trade (hard cap)
MAX_DAILY_USD     = 200     # daily spend cap
MAX_OPEN_POSITIONS = 5      # never hold more than this many at once
POSITIONS_FILE    = "positions.json"

# ── Endpoints ─────────────────────────────────────────────────────────────────
CLOB_BASE = "https://clob.polymarket.com"
HEADERS   = {"User-Agent": "PolyArbBot/2.0"}


# ─────────────────────────────────────────────────────────────────────────────
# Position persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_positions():
    try:
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"open": [], "closed": [], "daily_spent": 0, "daily_date": ""}


def save_positions(state):
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[trade_mgr] save error: {e}")


def reset_daily_if_needed(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_date") != today:
        state["daily_spent"] = 0
        state["daily_date"]  = today
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Market price lookup
# ─────────────────────────────────────────────────────────────────────────────

def get_best_bid(token_id):
    """Get current best bid (what we can sell at)."""
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        bids = r.json().get("bids", [])
        return float(bids[0]["price"]) if bids else None
    except Exception:
        return None


def get_best_ask(token_id):
    """Get current best ask (what we can buy at)."""
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        asks = r.json().get("asks", [])
        return float(asks[0]["price"]) if asks else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Order placement
# ─────────────────────────────────────────────────────────────────────────────

def place_order(client, token_id, price, size, side="BUY"):
    """Place a GTC limit order. Returns order dict or None."""
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(size, 2),
            side=BUY if side == "BUY" else SELL,
            order_type=OrderType.GTC,
        )
        resp = client.create_and_post_order(order_args)
        print(f"[trade_mgr] Order placed: {side} {size:.2f} @ {price:.4f} → {resp}")
        return resp
    except Exception as e:
        print(f"[trade_mgr] Order error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(msg):
    token   = os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        print("[trade_mgr] No Telegram config:", msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[trade_mgr] Telegram error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Exit logic — runs at the START of each cron tick
# ─────────────────────────────────────────────────────────────────────────────

def check_exits(client, state):
    """
    For each open position, check current price.
    Close if TP or SL is hit.
    """
    remaining = []
    for pos in state.get("open", []):
        token_id    = pos["token_id"]
        entry_price = pos["entry_price"]
        size        = pos["size"]
        question    = pos.get("question", "")[:50]

        current_bid = get_best_bid(token_id)
        if current_bid is None:
            print(f"[trade_mgr] Can't price {question} — keeping open")
            remaining.append(pos)
            continue

        pnl_pct = (current_bid - entry_price) / entry_price

        should_exit = False
        reason      = ""

        if pnl_pct >= TAKE_PROFIT_PCT:
            should_exit = True
            reason      = f"TAKE PROFIT +{pnl_pct*100:.1f}%"
        elif pnl_pct <= -STOP_LOSS_PCT:
            should_exit = True
            reason      = f"STOP LOSS {pnl_pct*100:.1f}%"

        if should_exit:
            print(f"[trade_mgr] Closing position: {reason} — {question}")
            resp = place_order(client, token_id, current_bid, size, side="SELL")
            if resp:
                pnl_usd = round((current_bid - entry_price) * size, 2)
                pos["exit_price"]  = current_bid
                pos["exit_reason"] = reason
                pos["pnl_usd"]     = pnl_usd
                pos["closed_at"]   = datetime.now(timezone.utc).isoformat()
                state.setdefault("closed", []).append(pos)

                emoji = "✅" if pnl_usd >= 0 else "🔴"
                send_telegram(
                    f"{emoji} <b>POSITION CLOSED — {reason}</b>\n"
                    f"📋 {question}\n"
                    f"📈 Entry: ${entry_price:.3f} → Exit: ${current_bid:.3f}\n"
                    f"💰 P&L: <b>${pnl_usd:+.2f}</b>\n"
                    f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
            else:
                remaining.append(pos)  # keep if sell failed
        else:
            unrealised = round((current_bid - entry_price) * size, 2)
            print(f"[trade_mgr] Holding {question} | entry {entry_price:.3f} bid {current_bid:.3f} | {pnl_pct*100:+.1f}% (${unrealised:+.2f})")
            remaining.append(pos)

        time.sleep(0.1)

    state["open"] = remaining
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Entry logic — runs AFTER exits
# ─────────────────────────────────────────────────────────────────────────────

def enter_trades(client, signals, state):
    """
    signals: list of dicts from manifold_scanner / signal_misprice etc.
    Each signal must have:
      - token_id     (Polymarket CLOB token to buy)
      - ask_price    (current ask)
      - question     (market title)
      - signal_type  (e.g. "MANIFOLD_ARB", "MISPRICE")
      - edge_pct     (float)
    """
    execute = os.environ.get("EXECUTE_TRADES", "false").lower() == "true"
    state   = reset_daily_if_needed(state)

    open_tokens = {p["token_id"] for p in state.get("open", [])}

    for sig in signals:
        if len(state["open"]) >= MAX_OPEN_POSITIONS:
            print("[trade_mgr] Max open positions reached")
            break

        if state["daily_spent"] >= MAX_DAILY_USD:
            print("[trade_mgr] Daily cap reached")
            break

        token_id  = sig.get("token_id")
        ask_price = sig.get("ask_price")
        question  = sig.get("question", "")[:60]
        edge      = sig.get("edge_pct", 0)
        sig_type  = sig.get("signal_type", "UNKNOWN")

        if not token_id or not ask_price:
            continue
        if ask_price <= 0 or ask_price >= 1:
            continue

        # Resolve market URL to CLOB token_id if needed
        if isinstance(token_id, str) and token_id.startswith("http"):
            slug = token_id.rstrip("/").split("/")[-1]
            try:
                r = requests.get(
                    f"{CLOB_BASE}/markets",
                    params={"market_slug": slug},
                    headers=HEADERS,
                    timeout=10,
                )
                data = r.json()
                tokens = data.get("tokens", [])
                resolved = next(
                    (t["token_id"] for t in tokens if t.get("outcome", "").upper() == "YES"),
                    None
                )
                if not resolved:
                    print(f"[trade_mgr] Could not resolve token for {slug}")
                    continue
                token_id = resolved
            except Exception as e:
                print(f"[trade_mgr] Token resolve error: {e}")
                continue

        if token_id in open_tokens:
            continue  # already holding

        budget = min(MAX_TRADE_USD, MAX_DAILY_USD - state["daily_spent"])
        size   = round(budget / ask_price, 2)

        if not execute:
            print(f"[trade_mgr] DRY RUN — would buy {size:.2f} shares @ {ask_price:.3f} | {question}")
            continue

        resp = place_order(client, token_id, ask_price + 0.01, size, side="BUY")
        if resp:
            tp_price = round(ask_price * (1 + TAKE_PROFIT_PCT), 4)
            sl_price = round(ask_price * (1 - STOP_LOSS_PCT),  4)

            position = {
                "token_id":    token_id,
                "question":    question,
                "signal_type": sig_type,
                "edge_pct":    edge,
                "entry_price": ask_price,
                "size":        size,
                "tp_price":    tp_price,
                "sl_price":    sl_price,
                "cost_usd":    round(ask_price * size, 2),
                "opened_at":   datetime.now(timezone.utc).isoformat(),
            }
            state["open"].append(position)
            state["daily_spent"] = round(state["daily_spent"] + position["cost_usd"], 2)
            open_tokens.add(token_id)

            send_telegram(
                f"🟢 <b>POSITION OPENED — {sig_type}</b>\n"
                f"📋 {question}\n"
                f"📈 Edge: <b>{edge:.1f}%</b> | Buy @ ${ask_price:.3f}\n"
                f"🎯 TP: ${tp_price:.3f} (+{TAKE_PROFIT_PCT*100:.0f}%) | "
                f"🛑 SL: ${sl_price:.3f} (-{STOP_LOSS_PCT*100:.0f}%)\n"
                f"💵 Size: {size:.2f} shares (${position['cost_usd']:.2f})\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )

        time.sleep(0.2)

    return state


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio summary (sent to Telegram once per run)
# ─────────────────────────────────────────────────────────────────────────────

def send_portfolio_summary(state):
    open_pos  = state.get("open", [])
    closed    = state.get("closed", [])
    daily     = state.get("daily_spent", 0)
    total_pnl = sum(p.get("pnl_usd", 0) for p in closed)

    if not open_pos and not closed:
        return

    lines = [f"📊 <b>Portfolio — {datetime.now(timezone.utc).strftime('%H:%M UTC')}</b>"]
    lines.append(f"Open positions: {len(open_pos)} | Daily spent: ${daily:.2f}")
    lines.append(f"Total realised P&L: <b>${total_pnl:+.2f}</b>")

    for p in open_pos:
        bid = get_best_bid(p["token_id"])
        if bid:
            pnl = round((bid - p["entry_price"]) * p["size"], 2)
            lines.append(f"  • {p['question'][:40]} | {pnl:+.2f} ({((bid-p['entry_price'])/p['entry_price'])*100:+.1f}%)")

    send_telegram("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(client, signals):
    """
    Call this from weather_edge_github.py passing:
      client  — authenticated py-clob-client instance
      signals — list of signal dicts (see enter_trades docstring)
    """
    print("\n[trade_mgr] ── Trade manager starting ──")
    state = load_positions()
    state = reset_daily_if_needed(state)

    # 1. Exit any positions at TP/SL
    if state["open"]:
        print(f"[trade_mgr] Checking {len(state['open'])} open positions for exits...")
        state = check_exits(client, state)

    # 2. Enter new trades from signals
    if signals:
        print(f"[trade_mgr] Processing {len(signals)} signals for entry...")
        state = enter_trades(client, signals, state)

    # 3. Portfolio summary
    send_portfolio_summary(state)

    # 4. Save state
    save_positions(state)
    print(f"[trade_mgr] Done. Open: {len(state['open'])} | Daily spent: ${state['daily_spent']:.2f}")
    return state
