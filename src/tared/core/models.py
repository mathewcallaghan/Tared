"""Immutable domain models for recipe formulation and batch calculations."""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Ingredient:
    name: str
    percentage: Decimal
    cost_per_unit: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Ingredient name cannot be empty")

        if not isinstance(self.percentage, Decimal):
            object.__setattr__(self, "percentage", Decimal(str(self.percentage)))
        if not isinstance(self.cost_per_unit, Decimal):
            object.__setattr__(self, "cost_per_unit", Decimal(str(self.cost_per_unit)))

        if not self.percentage.is_finite() or self.percentage <= Decimal("0"):
            raise ValueError(f"Percentage for '{self.name}' must be a finite positive number, got {self.percentage}%")
        if not self.cost_per_unit.is_finite() or self.cost_per_unit < Decimal("0"):
            raise ValueError(
                f"Cost per unit for '{self.name}' must be a finite non-negative number, got {self.cost_per_unit}"
            )


@dataclass(frozen=True, slots=True)
class CatalogIngredient:
    """Master raw material price-book item shared across recipes."""

    name: str
    cost_per_unit: Decimal = Decimal("0.00")
    supplier: str = ""
    sku: str = ""
    notes: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    updated_at: str = ""

    def __post_init__(self) -> None:
        clean_name = self.name.strip()
        if not clean_name:
            raise ValueError("Catalog ingredient name cannot be empty")
        if clean_name != self.name:
            object.__setattr__(self, "name", clean_name)

        if not isinstance(self.cost_per_unit, Decimal):
            object.__setattr__(self, "cost_per_unit", Decimal(str(self.cost_per_unit)))

        if not self.cost_per_unit.is_finite() or self.cost_per_unit < Decimal("0"):
            raise ValueError(
                f"Cost per unit for '{self.name}' must be a finite non-negative number, got {self.cost_per_unit}"
            )
        if not self.id.strip():
            object.__setattr__(self, "id", str(uuid.uuid4()))


@dataclass(frozen=True, slots=True)
class Recipe:
    name: str
    ingredients: tuple[Ingredient, ...]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    version: int = 1
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Recipe name cannot be empty")
        if not self.ingredients:
            raise ValueError("Recipe must contain at least one ingredient")
        if isinstance(self.ingredients, list):
            object.__setattr__(self, "ingredients", tuple(self.ingredients))
        if not self.id.strip():
            object.__setattr__(self, "id", str(uuid.uuid4()))
        if self.version < 1:
            object.__setattr__(self, "version", 1)


@dataclass(frozen=True, slots=True)
class BatchItem:
    ingredient_name: str
    percentage: Decimal
    raw_weight: Decimal
    scaled_weight: Decimal
    cumulative_tare: Decimal
    cost: Decimal
    cost_per_unit: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if not isinstance(self.cost_per_unit, Decimal):
            object.__setattr__(self, "cost_per_unit", Decimal(str(self.cost_per_unit)))


@dataclass(frozen=True, slots=True)
class BatchResult:
    recipe_name: str
    target_weight: Decimal
    precision: Decimal
    items: tuple[BatchItem, ...]
    total_weight: Decimal
    total_cost: Decimal
    recipe_version: int = 1
    recipe_id: str = ""
    recipe_description: str = ""
    zero_weight_ingredients: tuple[str, ...] = ()
    high_deviation_ingredients: tuple[str, ...] = ()
    max_allocation_deviation: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if not self.target_weight.is_finite() or self.target_weight <= Decimal("0"):
            raise ValueError(f"Target weight must be a finite positive number, got {self.target_weight}")
        if not self.precision.is_finite() or self.precision <= Decimal("0"):
            raise ValueError(f"Precision must be a finite positive number, got {self.precision}")
        if not isinstance(self.max_allocation_deviation, Decimal):
            object.__setattr__(self, "max_allocation_deviation", Decimal(str(self.max_allocation_deviation)))
