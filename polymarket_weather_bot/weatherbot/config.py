from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: Literal["paper", "live"] = "paper"
    database_path: Path = Path("weatherbot.db")
    paper_bankroll_usd: float = Field(1000, gt=0)
    scan_interval_seconds: int = Field(300, ge=60)
    cities: str = "nyc,chicago,miami,los_angeles,denver"

    min_edge: float = Field(0.10, ge=0.02, le=0.5)
    min_volume_usd: float = Field(1000, ge=0)
    max_spread: float = Field(0.08, ge=0, le=0.5)
    max_entry_price: float = Field(0.85, gt=0, lt=1)
    max_days_ahead: int = Field(7, ge=0, le=14)
    kelly_fraction: float = Field(0.10, gt=0, le=0.25)
    max_bet_usd: float = Field(25, gt=0)
    max_position_pct: float = Field(0.02, gt=0, le=0.10)
    max_daily_risk_usd: float = Field(50, gt=0)
    max_city_risk_usd: float = Field(30, gt=0)
    max_open_positions: int = Field(8, ge=1)

    poly_private_key: str | None = None
    poly_funder: str | None = None
    poly_api_key: str | None = None
    poly_api_secret: str | None = None
    poly_api_passphrase: str | None = None
    poly_signature_type: int = 0

    @property
    def city_keys(self) -> list[str]:
        return [x.strip() for x in self.cities.split(",") if x.strip()]

    @model_validator(mode="after")
    def validate_live_credentials(self):
        if self.mode == "live":
            required = {
                "POLY_PRIVATE_KEY": self.poly_private_key,
                "POLY_FUNDER": self.poly_funder,
                "POLY_API_KEY": self.poly_api_key,
                "POLY_API_SECRET": self.poly_api_secret,
                "POLY_API_PASSPHRASE": self.poly_api_passphrase,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"Live mode missing: {', '.join(missing)}")
        return self

