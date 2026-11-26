"""fal endpoint prices, USD per request, as listed on fal's model pages on 2026-11-25.

Kept here (not in mirror_api/pricing.py) while another engineer owns that file. [UNTESTED]
against real invoices. Unknown or "$0 per compute seconds" endpoints get a placeholder so they
still show up in the spend ledger.
"""

from __future__ import annotations

import logging
from decimal import Decimal

log = logging.getLogger(__name__)

FAL_PRICES: dict[str, Decimal] = {
    # Listed as "$0 per compute seconds"; real price unknown. Placeholder.
    "fal-ai/birefnet/v2": Decimal("0.002"),
    "fal-ai/birefnet": Decimal("0.002"),
    "bria/rmbg-3": Decimal("0.018"),
    "pixelcut/background-removal": Decimal("0.016"),
    "fal-ai/feynobg": Decimal("0.001"),
    # Per request, however many boxes (whether a multi-box request bills as one: unverified).
    "fal-ai/sam-3/image": Decimal("0.005"),
    "fal-ai/sam-3-1/image": Decimal("0.01"),
}
UNKNOWN_ENDPOINT_USD = Decimal("0.05")


def fal_price(endpoint: str) -> Decimal:
    price = FAL_PRICES.get(endpoint)
    if price is None:
        log.warning("no price for fal endpoint %s; charging $%s", endpoint, UNKNOWN_ENDPOINT_USD)
        return UNKNOWN_ENDPOINT_USD
    return price
