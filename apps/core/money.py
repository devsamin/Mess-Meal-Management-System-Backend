"""Decimal helpers shared by the calculation service and bill splitting."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Hashable, Mapping

CENT = Decimal("0.01")
ZERO = Decimal("0")


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


def round_money(value) -> Decimal:
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def allocate(total, weights: Mapping[Hashable, Decimal]) -> dict:
    """Split ``total`` proportionally to ``weights`` in whole paisa.

    Uses the largest-remainder method so the parts always add up to exactly
    ``round_money(total)`` — no paisa is created or lost through rounding.
    Keys with zero weight get zero. If every weight is zero, everyone gets zero.
    """
    total = round_money(total)
    keys = list(weights.keys())
    weight_sum = sum((to_decimal(w) for w in weights.values()), ZERO)
    if not keys or weight_sum <= 0:
        return {k: ZERO.quantize(CENT) for k in keys}

    total_cents = int(total / CENT)
    raw = {k: Decimal(total_cents) * to_decimal(weights[k]) / weight_sum for k in keys}
    floors = {k: int(raw[k]) for k in keys}  # int() truncates toward zero (amounts are non-negative)
    leftover = total_cents - sum(floors.values())
    # Hand out leftover cents to the largest fractional parts; ties broken by original order.
    order = sorted(keys, key=lambda k: (-(raw[k] - floors[k]), keys.index(k)))
    for k in order[:leftover]:
        floors[k] += 1
    return {k: (Decimal(floors[k]) * CENT).quantize(CENT) for k in keys}
