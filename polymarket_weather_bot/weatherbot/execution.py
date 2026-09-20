from .config import Settings
from .models import Signal


def live_buy_yes(signal: Signal, settings: Settings) -> str:
    """Submit a fill-or-kill market BUY. Caller must enforce the CLI live confirmation."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
        from py_clob_client.constants import POLYGON
    except ImportError as exc:
        raise RuntimeError("Install live dependencies: pip install -e '.[live]'") from exc
    creds = ApiCreds(settings.poly_api_key, settings.poly_api_secret, settings.poly_api_passphrase)
    client = ClobClient("https://clob.polymarket.com", key=settings.poly_private_key,
                        chain_id=POLYGON, creds=creds, signature_type=settings.poly_signature_type,
                        funder=settings.poly_funder)
    args = MarketOrderArgs(token_id=signal.token_id, amount=signal.size_usd, side="BUY", order_type=OrderType.FOK)
    signed = client.create_market_order(args)
    response = client.post_order(signed, OrderType.FOK)
    if not response.get("success", False):
        raise RuntimeError(f"Polymarket rejected order: {response.get('errorMsg', 'unknown error')}")
    return str(response.get("orderID") or response.get("id") or "submitted")
