from datetime import date
from math import floor

from .config import Settings
from .models import Forecast, Signal, WeatherMarket


def calibrated_probability(market: WeatherMarket, forecast: Forecast, today: date | None = None) -> tuple[float, float]:
    hits = sum(market.bucket.contains(x) for x in forecast.members_f)
    n = len(forecast.members_f)
    # Jeffreys smoothing prevents false 0%/100% confidence.
    raw = hits / n
    smoothed = (hits + 0.5) / (n + 1.0)
    horizon = max(0, (market.target_date - (today or date.today())).days)
    # Shrink farther forecasts toward 50%; empirical calibration should replace this after enough settlements.
    reliability = max(0.55, 0.92 - 0.05 * horizon)
    calibrated = 0.5 + (smoothed - 0.5) * reliability
    return raw, min(0.97, max(0.03, calibrated))


def kelly_size(probability: float, price: float, bankroll: float, settings: Settings) -> float:
    if not 0 < price < 1:
        return 0
    full_kelly = max(0.0, (probability - price) / (1.0 - price))
    cap = min(settings.max_bet_usd, bankroll * settings.max_position_pct)
    return max(0.0, min(cap, bankroll * full_kelly * settings.kelly_fraction))


def build_signal(market: WeatherMarket, forecast: Forecast, bankroll: float, settings: Settings, today: date | None = None) -> Signal:
    raw, probability = calibrated_probability(market, forecast, today)
    yes_entry = market.best_ask
    no_entry = 1.0 - market.best_bid
    yes_edge = probability - yes_entry
    no_edge = (1.0 - probability) - no_entry
    if yes_edge >= no_edge:
        outcome, token_id, entry, edge, win_probability = "YES", market.yes_token_id, yes_entry, yes_edge, probability
    else:
        outcome, token_id, entry, edge, win_probability = "NO", market.no_token_id, no_entry, no_edge, 1.0 - probability
    size = floor(kelly_size(win_probability, entry, bankroll, settings) * 100) / 100
    blockers = []
    horizon = (market.target_date - (today or date.today())).days
    if horizon < 0 or horizon > settings.max_days_ahead: blockers.append("forecast horizon")
    if edge < settings.min_edge: blockers.append("edge")
    if market.volume_usd < settings.min_volume_usd: blockers.append("volume")
    if market.spread > settings.max_spread: blockers.append("spread")
    if entry > settings.max_entry_price: blockers.append("entry price")
    if size < 1: blockers.append("size")
    if blockers:
        size = 0
    reason = "actionable" if not blockers else "blocked: " + ", ".join(blockers)
    return Signal(market, raw, probability, edge, entry, outcome, token_id, size, len(forecast.members_f), forecast.model_count, reason)
