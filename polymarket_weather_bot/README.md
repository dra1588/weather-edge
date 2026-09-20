# Polymarket Weather Bot

A weather-only Polymarket scanner and trader inspired by
[`suislanchez/polymarket-kalshi-weather-bot`](https://github.com/suislanchez/polymarket-kalshi-weather-bot).
It discovers temperature markets, converts GFS/ICON ensemble forecasts into
smoothed probabilities, compares YES and NO with executable asks, and paper
trades only signals that pass liquidity and portfolio-risk gates.

## Safety first

- `MODE=paper` is the default.
- Live trading needs both `MODE=live` and `--confirm-live`.
- Orders are fill-or-kill; partial resting exposure is avoided.
- The bot deduplicates markets and caps bet size, daily risk, city risk, and
  total open positions.
- A forecast edge is not guaranteed profit. Resolution-source mismatch,
  station mismatch, model bias, forecast updates, spread, and thin liquidity
  can erase apparent edge.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
pytest
weatherbot once
weatherbot run
```

Start with at least 30–50 settled paper trades. Compare Brier score, calibration
by probability band, expected edge versus realised return, and slippage before
considering live mode.

## Strategy

1. Page through active Gamma events and parse supported city/date/high-or-low
   temperature markets, including tails and ranges such as `69-70°F`.
2. Fetch GFS and ICON ensemble daily maxima/minima from Open-Meteo.
3. Equal-weight the available models, use Jeffreys smoothing, then shrink
   longer-horizon forecasts toward 50%.
4. Calculate edge against the current best ask, not the displayed midpoint.
5. Require minimum edge, volume, spread, entry price, forecast horizon and size.
6. Size with conservative fractional Kelly subject to hard dollar caps.

## Before live trading

Install the optional client and fill the five Polymarket credential variables:

```bash
pip install -e '.[live]'
MODE=live weatherbot once --confirm-live
```

Verify each market's exact resolution source and station manually during the
paper trial. The coordinates in `weatherbot/forecast.py` are airport/station
proxies and may need changing to match a market's written rules exactly.

## Railway

Set the environment variables from `.env.example`; use `weatherbot run` as the
start command. Keep `MODE=paper` until the trial metrics pass your thresholds.

## Attribution

The architecture and ensemble-weather concept are based on the MIT-licensed
reference repository above. This is an independent, weather-only rewrite with
additional market parsing and risk controls.
