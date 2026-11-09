"""Calculation engine enforcing strict conservation of mass, cumulative tare, and $/kg cost tracking."""

import decimal
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal

from tared.core.models import BatchItem, BatchResult, Recipe

PERCENTAGE_TOTAL = Decimal("100.00")
PERCENTAGE_TOLERANCE = Decimal("0.0001")
GRAMS_PER_KG = Decimal("1000")

# High-precision deterministic calculation context isolating the engine from ambient process state
CALCULATION_CONTEXT = Context(prec=50, rounding=ROUND_HALF_UP)


class RecipeCalculationError(Exception):
    """Base exception for recipe calculation errors."""


class PercentageSumError(RecipeCalculationError):
    """Raised when recipe ingredient percentages do not sum to 100.00%."""


class IncompatibleTargetPrecisionError(RecipeCalculationError):
    """Raised when target weight is not an exact multiple of the scale precision."""


class AllocationInvariantError(RecipeCalculationError):
    """Raised when discrete allocation unit deficit exceeds ingredient count."""


class MassConservationError(RecipeCalculationError):
    """Raised when allocated discrete units do not sum to target units."""


def validate_recipe_percentages(
    recipe: Recipe,
    tolerance: Decimal = PERCENTAGE_TOLERANCE,
) -> Decimal:
    """Validate that recipe percentages sum to 100.00% within tolerance."""
    with decimal.localcontext(CALCULATION_CONTEXT):
        total = sum((item.percentage for item in recipe.ingredients), Decimal("0"))
        if abs(total - PERCENTAGE_TOTAL) > tolerance:
            raise PercentageSumError(
                f"Recipe '{recipe.name}' ingredient percentages sum to {total}%, expected {PERCENTAGE_TOTAL}%"
            )
        return total


def _resolve_currency_quantum(currency_precision: Decimal | str | int | None) -> Decimal | None:
    """Resolve currency precision into a discrete step increment."""
    if currency_precision is None:
        return None
    if isinstance(currency_precision, float):
        raise TypeError("Floating-point currency precision is disallowed; use Decimal, str, or int.")
    if isinstance(currency_precision, int):
        if currency_precision < 0:
            raise ValueError(f"Integer currency decimal places must be non-negative, got {currency_precision}")
        return Decimal("10") ** -currency_precision

    quantum = Decimal(str(currency_precision))
    if not quantum.is_finite() or quantum <= Decimal("0"):
        raise ValueError(f"Currency precision must be a positive finite number, got {currency_precision}")
    return quantum


def _quantize_currency(amount: Decimal, quantum: Decimal | None) -> Decimal:
    """Round a currency amount to a specified quantum step increment."""
    if quantum is None:
        return amount
    units = (amount / quantum).to_integral_value(rounding=ROUND_HALF_UP)
    return units * quantum


def compute_batch(
    recipe: Recipe,
    target_weight: Decimal | str | int,
    precision: Decimal | str | int = Decimal("0.01"),
    currency_precision: Decimal | str | int | None = None,
) -> BatchResult:
    """Compute scaled weights, cumulative vessel tares, and $/kg costing.

    Enforces that target_weight is an exact multiple of the discrete precision quantum.
    Uses normalized proportional scaling combined with Hare-Niemeyer allocation
    to strictly conserve mass down to the requested precision quantum.

    Tie-breaking policy for equal remainders:
    1. Higher ingredient formula percentage
    2. Earlier compounding position in the recipe sequence (lower index)
    """
    if isinstance(target_weight, float):
        raise TypeError(
            f"Binary floating-point target_weight ({target_weight}) is disallowed to prevent precision loss. "
            "Pass Decimal, str, or int instead."
        )
    if isinstance(precision, float):
        raise TypeError(
            f"Binary floating-point precision ({precision}) is disallowed to prevent precision loss. "
            "Pass Decimal, str, or int instead."
        )

    with decimal.localcontext(CALCULATION_CONTEXT):
        if not isinstance(target_weight, Decimal):
            target_weight = Decimal(str(target_weight))
        if not isinstance(precision, Decimal):
            precision = Decimal(str(precision))

        currency_quantum = _resolve_currency_quantum(currency_precision)

        if not target_weight.is_finite() or target_weight <= Decimal("0"):
            raise ValueError(f"Target weight must be a finite positive number, got {target_weight}")
        if not precision.is_finite() or precision <= Decimal("0"):
            raise ValueError(f"Precision must be a finite positive number, got {precision}")

        quotient = target_weight / precision
        if quotient != quotient.to_integral_value():
            suggested = (quotient.to_integral_value(rounding=ROUND_HALF_UP)) * precision
            raise IncompatibleTargetPrecisionError(
                f"Target mass {target_weight} is not a multiple of scale precision {precision}. "
                f"Compatible nearest target: {suggested}"
            )

        total_percentage = validate_recipe_percentages(recipe)

        # 1. Proportional exact unrounded raw weights normalized to actual percentage sum
        raw_weights = [(target_weight * ing.percentage) / total_percentage for ing in recipe.ingredients]

        # 2. Hare-Niemeyer discrete quantization for mass conservation
        floor_units: list[int] = []
        remainders: list[tuple[Decimal, Decimal, int]] = []

        for idx, (raw_w, ing) in enumerate(zip(raw_weights, recipe.ingredients)):
            units = int((raw_w / precision).to_integral_value(rounding=ROUND_FLOOR))
            floor_units.append(units)
            rem = raw_w - (Decimal(units) * precision)
            remainders.append((rem, ing.percentage, idx))

        target_units = int(quotient.to_integral_value())
        unit_deficit = target_units - sum(floor_units)

        num_ingredients = len(recipe.ingredients)
        if not (0 <= unit_deficit <= num_ingredients):
            raise AllocationInvariantError(
                f"Allocation invariant violated: unit_deficit={unit_deficit}, ingredients_count={num_ingredients}"
            )

        # Deterministic 3-tier tie-breaking:
        # 1) Largest remainder (-rem)
        # 2) Highest formula percentage (-percentage)
        # 3) Earlier formula index (+idx)
        remainders.sort(key=lambda x: (-x[0], -x[1], x[2]))

        allocated_units = list(floor_units)
        for i in range(unit_deficit):
            target_idx = remainders[i][2]
            allocated_units[target_idx] += 1

        if sum(allocated_units) != target_units:
            raise MassConservationError("Conservation of mass unit invariant violated after allocation.")

        # 3. Assemble BatchItems with cumulative tare, $/kg micro-costing, and deviation tracking
        items: list[BatchItem] = []
        running_tare = Decimal("0")
        unrounded_total_cost = Decimal("0")
        zero_weight_ingredients: list[str] = []
        high_deviation_ingredients: list[str] = []
        max_dev = Decimal("0")

        for idx, ing in enumerate(recipe.ingredients):
            scaled_w = Decimal(allocated_units[idx]) * precision
            running_tare += scaled_w

            deviation = abs(scaled_w - raw_weights[idx])
            if deviation > max_dev:
                max_dev = deviation

            if raw_weights[idx] > Decimal("0"):
                rel_dev = deviation / raw_weights[idx]
                if rel_dev > Decimal("0.25") and ing.percentage > Decimal("0"):
                    high_deviation_ingredients.append(ing.name)

            raw_cost = (scaled_w * ing.cost_per_unit) / GRAMS_PER_KG
            unrounded_total_cost += raw_cost
            line_cost = _quantize_currency(raw_cost, currency_quantum)

            if scaled_w == Decimal("0") and ing.percentage > Decimal("0"):
                zero_weight_ingredients.append(ing.name)

            items.append(
                BatchItem(
                    ingredient_name=ing.name,
                    percentage=ing.percentage,
                    raw_weight=raw_weights[idx],
                    scaled_weight=scaled_w,
                    cumulative_tare=running_tare,
                    cost=line_cost,
                    cost_per_unit=ing.cost_per_unit,
                )
            )

        # Total cost is rounded from unrounded aggregate sum to prevent line-rounding drift
        final_total_cost = _quantize_currency(unrounded_total_cost, currency_quantum)

        return BatchResult(
            recipe_name=recipe.name,
            target_weight=target_weight,
            precision=precision,
            items=tuple(items),
            total_weight=running_tare,
            total_cost=final_total_cost,
            recipe_version=recipe.version,
            recipe_id=recipe.id,
            recipe_description=recipe.description,
            zero_weight_ingredients=tuple(zero_weight_ingredients),
            high_deviation_ingredients=tuple(high_deviation_ingredients),
            max_allocation_deviation=max_dev,
        )
