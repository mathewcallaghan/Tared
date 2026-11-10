"""Abstract repository contract for recipe persistence and production batch logging."""

from abc import ABC, abstractmethod
from decimal import Decimal
from pathlib import Path
from typing import Any

from tared.core.models import CatalogIngredient, Recipe


class StorageError(Exception):
    """Base exception for storage and persistence errors."""


class DuplicateBatchNumberError(StorageError):
    """Raised when a production batch run with the same batch number already exists."""


class RecipeNotFoundError(StorageError):
    """Raised when a referenced recipe or revision does not exist."""


class RecipeRepository(ABC):
    """Abstract interface defining recipe persistence and production batch operations."""

    @abstractmethod
    def save_recipe(self, recipe: Recipe) -> None:
        """Create or update a recipe and record an immutable revision atomically."""

    @abstractmethod
    def get_recipe(self, recipe_id: str, include_archived: bool = False) -> Recipe | None:
        """Fetch a single recipe by its unique ID."""

    @abstractmethod
    def list_recipes(self) -> list[Recipe]:
        """Retrieve all active recipes ordered by name."""

    @abstractmethod
    def delete_recipe(self, recipe_id: str) -> bool:
        """Archive a recipe by ID while preserving audit revisions and batch runs."""

    @abstractmethod
    def get_recipe_revisions(self, recipe_id: str) -> list[dict[str, Any]]:
        """Retrieve historical immutable revision snapshots for a recipe."""

    @abstractmethod
    def record_batch(
        self,
        recipe_id: str,
        recipe_version: int,
        batch_number: str,
        operator: str,
        target_weight: str | Decimal,
        total_weight: str | Decimal,
        total_cost: str | Decimal,
        notes: str = "",
        created_at: str | None = None,
        precision: str | Decimal = Decimal("0.01"),
        items: Any = None,
    ) -> str:
        """Log a completed production batch run referencing an immutable revision."""

    @abstractmethod
    def batch_exists(self, batch_number: str) -> bool:
        """Check whether a batch with the specified batch number has already been recorded."""

    @abstractmethod
    def list_batches(self, recipe_id: str | None = None) -> list[dict[str, Any]]:
        """List historical production batch records."""

    @abstractmethod
    def get_catalog_ingredient(self, name: str) -> CatalogIngredient | None:
        """Fetch a catalog raw material by name (case-insensitive)."""

    @abstractmethod
    def get_catalog_ingredients_map(self) -> dict[str, CatalogIngredient]:
        """Fetch all catalog raw materials indexed by lowercase name."""

    @abstractmethod
    def list_catalog_ingredients(self) -> list[CatalogIngredient]:
        """List all catalog raw materials ordered by name."""

    @abstractmethod
    def save_catalog_ingredient(self, item: CatalogIngredient) -> None:
        """Create or update a single raw material catalog item."""

    @abstractmethod
    def save_catalog_ingredients(self, items: list[CatalogIngredient]) -> None:
        """Atomically create or update multiple raw material catalog items."""

    @abstractmethod
    def update_catalog_ingredient(self, item: CatalogIngredient) -> bool:
        """Update an existing catalog raw material by its unique ID."""

    @abstractmethod
    def upsert_catalog_price(
        self,
        name: str,
        cost_per_unit: Decimal,
        timestamp: str | None = None,
    ) -> str:
        """Update unit cost for a catalog ingredient by name while preserving supplier, SKU, and notes."""

    @abstractmethod
    def delete_catalog_ingredient(self, item_id: str) -> bool:
        """Remove a raw material from the catalog by ID."""

    @abstractmethod
    def restore_workspace_data(
        self,
        workspace_data: dict[str, Any],
        pre_restore_backup_dir: Path | None = None,
    ) -> Path | None:
        """Atomically restore all workspace tables from verified dictionary data."""

    @abstractmethod
    def list_all_recipes_data(self) -> list[dict[str, Any]]:
        """Retrieve all recipes (including archived) with ingredients for workspace export."""

    @abstractmethod
    def list_all_revisions(self) -> list[dict[str, Any]]:
        """Retrieve all historical immutable revision snapshots across all recipes."""

    def get_corrupted_recipes(self) -> list[dict[str, Any]]:
        """Retrieve recipes from storage that fail integrity validation or lack ingredients."""
        return []

    def export_workspace_snapshot(self) -> dict[str, Any]:
        """Atomically read all workspace entities within a single read transaction."""
        return {
            "catalog": [
                {
                    "id": item.id,
                    "name": item.name,
                    "cost_per_unit": str(item.cost_per_unit),
                    "supplier": item.supplier,
                    "sku": item.sku,
                    "notes": item.notes,
                    "updated_at": item.updated_at,
                }
                for item in self.list_catalog_ingredients()
            ],
            "recipes": self.list_all_recipes_data(),
            "revisions": self.list_all_revisions(),
            "batches": self.list_batches(),
        }

    @abstractmethod
    def reset_workspace_data(
        self,
        pre_reset_backup_dir: Path | None = None,
    ) -> Path | None:
        """Atomically reset all workspace tables to a clean empty state with pre-reset backup."""

    def close(self) -> None:
        """Release underlying database connections or storage resources."""
        pass
