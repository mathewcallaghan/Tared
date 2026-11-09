"""Test environment configuration and reusable formulation factories."""

import sys
from decimal import Decimal
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tared.core.models import Ingredient, Recipe  # noqa: E402


def create_test_recipe() -> Recipe:
    """Benchmark formulation for mathematical, storage, and UI testing ($/kg costs)."""
    return Recipe(
        name="Standard Benchmark Formulation",
        ingredients=(
            Ingredient(name="Material A", percentage=Decimal("29.00"), cost_per_unit=Decimal("5.00")),
            Ingredient(name="Material B", percentage=Decimal("29.00"), cost_per_unit=Decimal("3.00")),
            Ingredient(name="Material C", percentage=Decimal("16.70"), cost_per_unit=Decimal("8.00")),
            Ingredient(name="Material D", percentage=Decimal("10.00"), cost_per_unit=Decimal("2.00")),
            Ingredient(name="Material E", percentage=Decimal("5.30"), cost_per_unit=Decimal("1.00")),
            Ingredient(name="Material F", percentage=Decimal("5.00"), cost_per_unit=Decimal("20.00")),
            Ingredient(name="Material G", percentage=Decimal("3.00"), cost_per_unit=Decimal("12.00")),
            Ingredient(name="Material H", percentage=Decimal("2.00"), cost_per_unit=Decimal("35.00")),
        ),
    )


create_sauce_recipe = create_test_recipe

try:
    import pytest

    @pytest.fixture
    def sauce_recipe() -> Recipe:
        return create_test_recipe()
except ImportError:
    pass
