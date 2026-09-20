import json
import re
from datetime import date, datetime

import httpx

from .models import Metric, TemperatureBucket, WeatherMarket

GAMMA_URL = "https://gamma-api.polymarket.com/events"
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"

CITY_ALIASES = {
    "new york city": "nyc", "new york": "nyc", "nyc": "nyc",
    "chicago": "chicago", "miami": "miami",
    "los angeles": "los_angeles", "denver": "denver",
}


def _json_list(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


def parse_date(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:,?\s+(\d{4}))?",
        text, re.I,
    )
    if not match:
        return None
    month = datetime.strptime(match.group(1)[:3], "%b").month
    year = int(match.group(3) or today.year)
    candidate = date(year, month, int(match.group(2)))
    if not match.group(3) and candidate < today:
        candidate = date(year + 1, month, int(match.group(2)))
    return candidate


def parse_city(text: str) -> str | None:
    lowered = text.lower()
    for alias in sorted(CITY_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return CITY_ALIASES[alias]
    return None


def parse_bucket(text: str) -> TemperatureBucket | None:
    clean = text.lower().replace("º", "°")
    range_match = re.search(
        r"([+-]?\d+(?:\.\d+)?)\s*(?:°?\s*f)?\s*(?:-|–|to|through)\s*([+-]?\d+(?:\.\d+)?)",
        clean,
    )
    if range_match:
        first, second = float(range_match.group(1)), float(range_match.group(2))
        return TemperatureBucket(min(first, second), max(first, second))
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?(?=\s*°?\s*f)", clean)]
    if not nums:
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", clean)]
    if not nums:
        return None
    value = nums[0]
    if re.search(r"or below|or lower|at most|≤|below|under|less than", clean):
        inclusive = bool(re.search(r"or below|or lower|at most|≤", clean))
        return TemperatureBucket(upper_f=value, upper_inclusive=inclusive)
    if re.search(r"or above|or higher|at least|≥|above|over|exceed", clean):
        inclusive = bool(re.search(r"or above|or higher|at least|≥", clean))
        return TemperatureBucket(lower_f=value, lower_inclusive=inclusive)
    return TemperatureBucket(value, value)


def parse_market(event: dict, raw: dict, allowed_cities: set[str]) -> WeatherMarket | None:
    event_text = " ".join(str(event.get(k, "")) for k in ("title", "question", "slug"))
    question = str(raw.get("question") or raw.get("groupItemTitle") or "")
    combined = f"{event_text} {question}"
    city = parse_city(combined)
    target = parse_date(combined)
    if not city or city not in allowed_cities or not target:
        return None
    if target < date.today() or (target - date.today()).days > 14:
        return None
    metric = Metric.LOW if re.search(r"\b(low|minimum|min temp)", combined, re.I) else Metric.HIGH
    bucket = parse_bucket(str(raw.get("groupItemTitle") or question))
    if not bucket:
        return None
    prices = _json_list(raw.get("outcomePrices"))
    tokens = _json_list(raw.get("clobTokenIds"))
    outcomes = [str(x).lower() for x in _json_list(raw.get("outcomes"))]
    if len(prices) < 2 or len(tokens) < 2:
        return None
    yes_idx = outcomes.index("yes") if "yes" in outcomes else 0
    yes_price = float(prices[yes_idx])
    best_ask = float(raw.get("bestAsk") or yes_price)
    best_bid = float(raw.get("bestBid") or yes_price)
    if raw.get("closed") or not raw.get("active", True) or not 0.01 < yes_price < 0.99:
        return None
    return WeatherMarket(
        market_id=str(raw.get("conditionId") or raw.get("id") or ""),
        event_slug=str(event.get("slug") or ""), question=question, city_key=city,
        target_date=target, metric=metric, bucket=bucket,
        yes_token_id=str(tokens[yes_idx]), no_token_id=str(tokens[1 - yes_idx]), yes_price=yes_price,
        best_ask=best_ask, best_bid=best_bid,
        volume_usd=float(raw.get("volumeNum") or raw.get("volume") or 0),
    )


async def fetch_weather_markets(city_keys: list[str]) -> list[WeatherMarket]:
    results: dict[str, WeatherMarket] = {}
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "weatherbot/0.1"}) as client:
        # Public search is required because weather events may be far beyond the
        # first pages of the global active-event feed.
        labels = {"nyc": "NYC", "chicago": "Chicago", "miami": "Miami",
                  "los_angeles": "Los Angeles", "denver": "Denver"}
        for city in city_keys:
            response = await client.get(SEARCH_URL, params={"q": f"temperature {labels.get(city, city)}"})
            response.raise_for_status()
            events = response.json().get("events", [])
            for event in events:
                if event.get("closed") or not event.get("active", True):
                    continue
                haystack = " ".join(str(event.get(k, "")) for k in ("title", "question", "slug")).lower()
                if not any(word in haystack for word in ("temperature", "weather", "highest", "lowest")):
                    continue
                for raw in event.get("markets", []):
                    market = parse_market(event, raw, set(city_keys))
                    if market and market.market_id:
                        results[market.market_id] = market
    return list(results.values())
