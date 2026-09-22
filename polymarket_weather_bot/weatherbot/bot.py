import asyncio
from collections import defaultdict

from .config import Settings
from .execution import live_buy_yes
from .forecast import fetch_forecast
from .markets import fetch_market_price, fetch_weather_markets
from .models import Signal
from .store import Store
from .strategy import build_signal


async def scan(settings: Settings, store: Store, confirm_live: bool = False) -> list[Signal]:
    await reconcile_trades(store)
    markets = await fetch_weather_markets(settings.city_keys)
    keys = sorted({(m.city_key, m.target_date, m.metric) for m in markets}, key=str)
    fetched = await asyncio.gather(*(fetch_forecast(*key) for key in keys), return_exceptions=True)
    forecasts = {key: value for key, value in zip(keys, fetched) if not isinstance(value, Exception)}
    signals = []
    for market in markets:
        key = (market.city_key, market.target_date, market.metric)
        try:
            if key not in forecasts:
                raise RuntimeError("forecast unavailable")
            signal = build_signal(market, forecasts[key], settings.paper_bankroll_usd, settings)
        except Exception as exc:
            print(f"SKIP {market.question}: {exc}")
            continue
        store.log_signal(signal)
        signals.append(signal)

    # Execute strongest edge first and enforce portfolio-level gates at the final moment.
    for signal in sorted(signals, key=lambda x: x.edge, reverse=True):
        if signal.size_usd <= 0 or store.already_traded(signal.market.market_id):
            continue
        if store.open_count() >= settings.max_open_positions:
            break
        remaining_daily = settings.max_daily_risk_usd - store.risk_today()
        remaining_city = settings.max_city_risk_usd - store.risk_today(signal.market.city_key)
        allowed = min(signal.size_usd, remaining_daily, remaining_city)
        if allowed < 1:
            continue
        signal = Signal(**{**signal.__dict__, "size_usd": allowed})
        if settings.mode == "live":
            if not confirm_live:
                raise RuntimeError("Live mode requires --confirm-live")
            order_id = live_buy_yes(signal, settings)
            store.record_trade(signal, "live", order_id)
        else:
            store.record_trade(signal, "paper")
    return signals


async def reconcile_trades(store: Store):
    trades = store.open_trades()
    if not trades:
        return
    quotes = await asyncio.gather(*(
        fetch_market_price(trade["market_id"], trade["token_id"]) for trade in trades
    ), return_exceptions=True)
    for trade, quote in zip(trades, quotes):
        if isinstance(quote, Exception):
            print(f"MARK FAILED {trade['market_id']}: {quote}")
            continue
        price, settled = quote
        store.mark_trade(trade["id"], price, settled, apply_stops=trade["mode"] == "paper")


async def run_forever(settings: Settings, store: Store, confirm_live: bool = False):
    while True:
        try:
            signals = await scan(settings, store, confirm_live)
            print_summary(signals)
        except Exception as exc:
            print(f"SCAN FAILED: {exc}")
        await asyncio.sleep(settings.scan_interval_seconds)


def print_summary(signals: list[Signal]):
    if not signals:
        print("No supported open weather markets found.")
        return
    for s in sorted(signals, key=lambda x: x.edge, reverse=True):
        print(f"{s.market.city_key:12} {s.market.target_date} buy={s.outcome} model_yes={s.model_probability:.1%} ask={s.entry_price:.1%} edge={s.edge:+.1%} size=${s.size_usd:.2f} {s.reason}")
