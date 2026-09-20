import asyncio

import typer

from .bot import print_summary, run_forever, scan
from .config import Settings
from .store import Store

app = typer.Typer(help="Polymarket weather scanner and risk-gated trader")


@app.command()
def once(confirm_live: bool = typer.Option(False, help="Second key required for live orders")):
    settings = Settings()
    signals = asyncio.run(scan(settings, Store(settings.database_path), confirm_live))
    print_summary(signals)


@app.command()
def run(confirm_live: bool = typer.Option(False, help="Second key required for live orders")):
    settings = Settings()
    asyncio.run(run_forever(settings, Store(settings.database_path), confirm_live))


if __name__ == "__main__":
    app()

