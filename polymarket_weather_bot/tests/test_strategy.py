from datetime import date

from weatherbot.config import Settings
from weatherbot.markets import parse_bucket, parse_date, parse_market
from weatherbot.models import Forecast, Metric, TemperatureBucket, WeatherMarket
from weatherbot.strategy import build_signal, calibrated_probability


def test_bucket_parser_supports_ranges_and_tails():
    assert parse_bucket("69-70°F") == TemperatureBucket(69, 70)
    assert parse_bucket("68°F or below") == TemperatureBucket(upper_f=68)
    assert parse_bucket("71°F or higher") == TemperatureBucket(lower_f=71)


def test_date_rolls_year_forward():
    assert parse_date("December 3", date(2026, 12, 10)) == date(2027, 12, 3)


def test_parse_grouped_market():
    event = {"title": "Highest temperature in New York City on September 22", "slug": "nyc-temp-sep-22"}
    raw = {"id": "1", "conditionId": "c1", "question": "Will it be 69-70°F?", "groupItemTitle": "69-70°F",
           "outcomes": '["Yes","No"]', "outcomePrices": '["0.25","0.75"]',
           "clobTokenIds": '["yes-token","no-token"]', "bestAsk": 0.27, "bestBid": 0.23,
           "active": True, "closed": False, "volumeNum": 5000}
    result = parse_market(event, raw, {"nyc"})
    assert result and result.bucket.contains(70) and result.yes_token_id == "yes-token" and result.no_token_id == "no-token"


def test_signal_uses_ask_and_blocks_wide_spread():
    market = WeatherMarket("m", "e", "q", "nyc", date(2026, 9, 21), Metric.HIGH,
                            TemperatureBucket(lower_f=70), "yes", "no", .30, .36, .20, 5000)
    forecast = Forecast("nyc", date(2026, 9, 21), Metric.HIGH, tuple([75] * 20 + [65] * 10), 2)
    settings = Settings(max_spread=.08, min_edge=.05)
    signal = build_signal(market, forecast, 1000, settings, today=date(2026, 9, 20))
    assert signal.entry_price == .36
    assert signal.size_usd == 0
    assert "spread" in signal.reason


def test_probability_never_becomes_one():
    market = WeatherMarket("m", "e", "q", "nyc", date(2026, 9, 21), Metric.HIGH,
                            TemperatureBucket(lower_f=70), "yes", "no", .3, .31, .29, 5000)
    forecast = Forecast("nyc", market.target_date, Metric.HIGH, tuple([80] * 31), 1)
    raw, calibrated = calibrated_probability(market, forecast, date(2026, 9, 20))
    assert raw == 1
    assert calibrated < 1
