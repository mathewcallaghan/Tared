"""Unified numeric, currency, and percentage formatting across grid, summary, and print sheet."""

import decimal
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Context, Decimal

FORMAT_CONTEXT = Context(prec=50, rounding=ROUND_HALF_UP)
MAX_ADAPTIVE_PRECISION = 10


def format_percentage(percentage: Decimal) -> str:
    """Format percentage retaining significant fractional digits with a minimum of 2 decimal places."""
    s = f"{percentage:.6f}".rstrip("0")
    if s.endswith("."):
        return f"{s}00"
    parts = s.split(".")
    if len(parts) == 2 and len(parts[1]) < 2:
        return f"{s}0"
    return s


def determine_cost_precision(costs: Iterable[Decimal]) -> int:
    """Detect if any cost has fractional cents or sub-cent values requiring 4 decimal places."""
    for cost in costs:
        if Decimal("0") < cost < Decimal("0.01"):
            return 4
        cents = cost * Decimal("100")
        if cents != cents.to_integral_value():
            return 4
    return 2


def format_currency(
    value: Decimal,
    precision: int | None = None,
    currency_symbol: str = "$",
) -> str:
    """Format currency using explicit ROUND_HALF_UP quantization in high-precision context."""
    with decimal.localcontext(FORMAT_CONTEXT):
        if value == Decimal("0"):
            p = precision if precision is not None else 2
            return f"{currency_symbol}0.{'0' * p}"

        is_negative = value < Decimal("0")
        abs_val = abs(value)
        prefix = f"-{currency_symbol}" if is_negative else currency_symbol

        if precision is None:
            cents = abs_val * Decimal("100")
            if cents != cents.to_integral_value() or (Decimal("0") < abs_val < Decimal("0.01")):
                exp = abs_val.as_tuple().exponent
                raw_prec = max(4, -exp if isinstance(exp, int) else 4)
                precision = min(raw_prec, MAX_ADAPTIVE_PRECISION)
            else:
                precision = 2

        quantum = Decimal("10") ** -precision
        quantized_val = abs_val.quantize(quantum, rounding=ROUND_HALF_UP)

        # Guard against non-zero values underflowing to all zeros
        if abs_val > Decimal("0") and quantized_val == Decimal("0"):
            exp = abs_val.as_tuple().exponent
            target_prec = max(precision, -exp if isinstance(exp, int) else precision)
            precision = min(target_prec, MAX_ADAPTIVE_PRECISION)
            quantum = Decimal("10") ** -precision
            quantized_val = abs_val.quantize(quantum, rounding=ROUND_HALF_UP)

        return f"{prefix}{quantized_val:.{precision}f}"
