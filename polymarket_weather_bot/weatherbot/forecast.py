from datetime import date

import httpx

from .models import Forecast, Metric

CITIES = {
    "nyc": (40.7831, -73.9712),
    "chicago": (41.9742, -87.9073),
    "miami": (25.7959, -80.2870),
    "los_angeles": (33.9416, -118.4085),
    "denver": (39.8561, -104.6737),
}
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"


async def _fetch_model(client: httpx.AsyncClient, city_key: str, target: date, model: str, metric: Metric) -> list[float]:
    lat, lon = CITIES[city_key]
    variable = "temperature_2m_max" if metric == Metric.HIGH else "temperature_2m_min"
    response = await client.get(ENSEMBLE_URL, params={
        "latitude": lat, "longitude": lon, "daily": variable,
        "temperature_unit": "fahrenheit", "timezone": "auto",
        "start_date": target.isoformat(), "end_date": target.isoformat(), "models": model,
    })
    response.raise_for_status()
    daily = response.json().get("daily", {})
    return [float(values[0]) for key, values in daily.items()
            if key.startswith(variable) and isinstance(values, list) and values and values[0] is not None]


async def fetch_forecast(city_key: str, target: date, metric: Metric) -> Forecast:
    if city_key not in CITIES:
        raise ValueError(f"Unsupported city: {city_key}")
    per_model: list[list[float]] = []
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "weatherbot/0.1"}) as client:
        for model in ("gfs_seamless", "icon_seamless"):
            try:
                members = await _fetch_model(client, city_key, target, model, metric)
                if members:
                    per_model.append(members)
            except (httpx.HTTPError, ValueError):
                continue
    if not per_model:
        raise RuntimeError(f"No ensemble forecast for {city_key} {target}")
    # Equal weight per model; cap each at the smallest member count.
    count = min(len(x) for x in per_model)
    combined = tuple(value for model in per_model for value in model[:count])
    return Forecast(city_key, target, metric, combined, len(per_model))

