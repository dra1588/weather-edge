from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Metric(StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class TemperatureBucket:
    lower_f: float | None = None
    upper_f: float | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    def contains(self, value: float) -> bool:
        lower_ok = self.lower_f is None or (value >= self.lower_f if self.lower_inclusive else value > self.lower_f)
        upper_ok = self.upper_f is None or (value <= self.upper_f if self.upper_inclusive else value < self.upper_f)
        return lower_ok and upper_ok


@dataclass(frozen=True)
class WeatherMarket:
    market_id: str
    event_slug: str
    question: str
    city_key: str
    target_date: date
    metric: Metric
    bucket: TemperatureBucket
    yes_token_id: str
    no_token_id: str
    yes_price: float
    best_ask: float
    best_bid: float
    volume_usd: float

    @property
    def spread(self) -> float:
        return max(0.0, self.best_ask - self.best_bid)


@dataclass(frozen=True)
class Forecast:
    city_key: str
    target_date: date
    metric: Metric
    members_f: tuple[float, ...]
    model_count: int


@dataclass(frozen=True)
class Signal:
    market: WeatherMarket
    raw_probability: float
    model_probability: float
    edge: float
    entry_price: float
    outcome: str
    token_id: str
    size_usd: float
    members: int
    model_count: int
    reason: str
