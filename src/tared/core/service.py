"""Domain service orchestrating formulation change detection, versioning, and persistence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from tared.core.formatting import format_currency, format_percentage
from tared.core.models import CatalogIngredient, Ingredient, Recipe

if TYPE_CHECKING:
    from tared.storage.base import RecipeRepository


def summarize_revision_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> str:
    """Produce a concise summary of formulation changes between two revision snapshots."""
    if previous is None:
        return "Initial saved formulation"

    changes: list[str] = []
    prev_name = str(previous.get("name", ""))
    curr_name = str(current.get("name", ""))
    if prev_name != curr_name:
        changes.append(f"Renamed to '{curr_name}'")

    prev_desc = str(previous.get("description") or "")
    curr_desc = str(current.get("description") or "")
    if prev_desc != curr_desc:
        changes.append("Updated description/SOP")

    try:
        prev_items = json.loads(str(previous.get("ingredients_json", "[]")))
        curr_items = json.loads(str(current.get("ingredients_json", "[]")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return "; ".join(changes) if changes else "Revision snapshot"

    if not isinstance(prev_items, list) or not isinstance(curr_items, list):
        return "; ".join(changes) if changes else "Revision snapshot"

    prev_map = {str(it.get("name", "")).strip().lower(): it for it in prev_items if isinstance(it, dict)}
    curr_map = {str(it.get("name", "")).strip().lower(): it for it in curr_items if isinstance(it, dict)}

    for key, p_it in prev_map.items():
        if key not in curr_map:
            changes.append(f"Removed {str(p_it.get('name', '')).strip()}")

    for key, c_it in curr_map.items():
        c_name = str(c_it.get("name", "")).strip()
        try:
            c_pct = Decimal(str(c_it.get("percentage", "0")))
            c_cost = Decimal(str(c_it.get("cost_per_unit", "0.00")))
        except (InvalidOperation, ValueError):
            continue

        if key not in prev_map:
            changes.append(f"Added {c_name} ({format_percentage(c_pct)}%)")
            continue

        p_it = prev_map[key]
        try:
            p_pct = Decimal(str(p_it.get("percentage", "0")))
            p_cost = Decimal(str(p_it.get("cost_per_unit", "0.00")))
        except (InvalidOperation, ValueError):
            continue

        if p_pct != c_pct:
            changes.append(f"{c_name}: {format_percentage(p_pct)}% → {format_percentage(c_pct)}%")
        if p_cost != c_cost:
            changes.append(f"{c_name} cost: {format_currency(p_cost)} → {format_currency(c_cost)}/kg")

    prev_order = [str(it.get("name", "")).strip().lower() for it in prev_items if isinstance(it, dict)]
    curr_order = [str(it.get("name", "")).strip().lower() for it in curr_items if isinstance(it, dict)]
    if prev_order != curr_order and set(prev_order) == set(curr_order):
        changes.append("Reordered ingredients")

    if not changes:
        return "No formulation changes"
    if len(changes) > 3:
        return "; ".join(changes[:3]) + f"; +{len(changes) - 3} more"
    return "; ".join(changes)


def has_formulation_changed(existing: Recipe, candidate: Recipe) -> bool:
    """Compare full recipe specification: name, description, ingredient composition, order, and unit costs."""
    if existing.name != candidate.name or existing.description != candidate.description:
        return True
    if len(existing.ingredients) != len(candidate.ingredients):
        return True
    for e_ing, c_ing in zip(existing.ingredients, candidate.ingredients):
        if (
            e_ing.name != c_ing.name
            or e_ing.percentage != c_ing.percentage
            or e_ing.cost_per_unit != c_ing.cost_per_unit
        ):
            return True
    return False


@dataclass(slots=True)
class RecipeService:
    """Application service managing recipe formulation lifecycles and version transitions."""

    repository: RecipeRepository

    def compute_next_version(self, candidate: Recipe) -> int:
        """Determine next recipe version based on draft state and formulation changes."""
        existing = self.repository.get_recipe(candidate.id, include_archived=True)
        if existing is None:
            return 1

        # Drafts stay on their target version across repeated edits and on finalization
        if existing.is_draft:
            return existing.version

        # Branching a draft from a finalized revision reserves the next version number
        if candidate.is_draft:
            return existing.version + 1

        if has_formulation_changed(existing, candidate):
            return existing.version + 1

        revisions = self.repository.get_recipe_revisions(candidate.id)
        for rev in revisions:
            if rev["version"] == existing.version:
                try:
                    rev_items = json.loads(rev["ingredients_json"])
                    rev_recipe = Recipe(
                        id=candidate.id,
                        name=rev["name"],
                        description=rev.get("description", "") or "",
                        version=existing.version,
                        ingredients=tuple(
                            Ingredient(
                                name=it["name"],
                                percentage=Decimal(str(it["percentage"])),
                                cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
                            )
                            for it in rev_items
                        ),
                    )
                    if has_formulation_changed(rev_recipe, candidate):
                        return existing.version + 1
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    pass
                break

        return existing.version

    def prepare_recipe_for_save(self, candidate: Recipe, timestamp: str | None = None) -> Recipe:
        """Assign version and timestamp, returning updated Recipe instance ready for persistence."""
        version = self.compute_next_version(candidate)
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return Recipe(
            id=candidate.id,
            name=candidate.name,
            description=candidate.description,
            version=version,
            updated_at=timestamp,
            is_draft=candidate.is_draft,
            ingredients=candidate.ingredients,
        )

    def save_recipe(self, candidate: Recipe, timestamp: str | None = None) -> Recipe:
        """Persist recipe to repository with automated formulation version resolution."""
        prepared = self.prepare_recipe_for_save(candidate, timestamp=timestamp)
        self.repository.save_recipe(prepared)
        return prepared

    def discard_draft(self, recipe_id: str) -> Recipe | None:
        """Revert an unreleased draft formula back to its latest committed revision snapshot."""
        existing = self.repository.get_recipe(recipe_id, include_archived=True)
        if existing is None or not existing.is_draft:
            raise ValueError("Only an active draft formula can be discarded.")

        revisions = self.repository.get_recipe_revisions(recipe_id)
        if not revisions:
            return None

        latest_rev = revisions[0]
        rev_items = json.loads(latest_rev["ingredients_json"])
        reverted = Recipe(
            id=recipe_id,
            name=latest_rev["name"],
            description=latest_rev.get("description", "") or "",
            version=int(latest_rev["version"]),
            updated_at=latest_rev.get("created_at", "") or "",
            is_draft=False,
            ingredients=tuple(
                Ingredient(
                    name=it["name"],
                    percentage=Decimal(str(it["percentage"])),
                    cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
                )
                for it in rev_items
            ),
        )
        self.repository.save_recipe(reverted)
        return reverted

    def get_catalog_price(self, ingredient_name: str) -> Decimal | None:
        """Retrieve master catalog unit cost for an ingredient name if known."""
        item = self.repository.get_catalog_ingredient(ingredient_name)
        return item.cost_per_unit if item is not None else None

    def sync_recipe_costs_from_catalog(self, recipe: Recipe) -> tuple[Recipe, list[tuple[str, Decimal, Decimal]]]:
        """Compare and sync recipe ingredient costs with catalog default prices in a single query."""
        changes: list[tuple[str, Decimal, Decimal]] = []
        new_ingredients = []
        cat_map = self.repository.get_catalog_ingredients_map()

        for ing in recipe.ingredients:
            cat_item = cat_map.get(ing.name.strip().lower())
            if cat_item is not None and cat_item.cost_per_unit != ing.cost_per_unit:
                changes.append((ing.name, ing.cost_per_unit, cat_item.cost_per_unit))
                new_ingredients.append(
                    Ingredient(
                        name=ing.name,
                        percentage=ing.percentage,
                        cost_per_unit=cat_item.cost_per_unit,
                    )
                )
            else:
                new_ingredients.append(ing)

        updated_recipe = Recipe(
            id=recipe.id,
            name=recipe.name,
            description=recipe.description,
            version=recipe.version,
            updated_at=recipe.updated_at,
            is_draft=recipe.is_draft,
            ingredients=tuple(new_ingredients),
        )
        return updated_recipe, changes

    def save_ingredient_to_catalog(
        self,
        name: str,
        cost_per_unit: Decimal,
        supplier: str = "",
        sku: str = "",
        notes: str = "",
    ) -> tuple[str, Decimal | None]:
        """Save a single ingredient and its unit cost to the master catalog."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Ingredient name cannot be empty")

        existing = self.repository.get_catalog_ingredient(clean_name)
        if existing is not None:
            if existing.cost_per_unit == cost_per_unit:
                return "identical", existing.cost_per_unit
            return "different", existing.cost_per_unit

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.repository.save_catalog_ingredient(
            CatalogIngredient(
                name=clean_name,
                cost_per_unit=cost_per_unit,
                supplier=supplier.strip(),
                sku=sku.strip(),
                notes=notes.strip(),
                updated_at=timestamp,
            )
        )
        return "added", None

    def preview_learn_from_recipe(self, recipe: Recipe) -> list[tuple[str, Decimal | None, Decimal]]:
        """Identify candidate catalog additions and price updates from a recipe.

        Skips ingredients with zero or negative unit cost, as well as those
        matching existing catalog prices.
        Returns list of (ingredient_name, old_unit_cost_or_none, new_unit_cost).
        """
        diffs: list[tuple[str, Decimal | None, Decimal]] = []
        seen: set[str] = set()

        for ing in recipe.ingredients:
            clean_name = ing.name.strip()
            if not clean_name:
                continue
            name_lower = clean_name.lower()
            if name_lower in seen:
                continue
            seen.add(name_lower)

            if ing.cost_per_unit <= Decimal("0.00"):
                continue

            existing = self.repository.get_catalog_ingredient(clean_name)
            if existing is None:
                diffs.append((clean_name, None, ing.cost_per_unit))
            elif existing.cost_per_unit != ing.cost_per_unit:
                diffs.append((clean_name, existing.cost_per_unit, ing.cost_per_unit))

        return diffs

    def update_catalog_from_recipe(self, recipe: Recipe, allowed_names: set[str] | None = None) -> int:
        """Atomically upsert non-zero recipe ingredient prices into master catalog."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        items_to_save: list[CatalogIngredient] = []
        seen: set[str] = set()

        for ing in recipe.ingredients:
            clean_name = ing.name.strip()
            if not clean_name:
                continue
            name_lower = clean_name.lower()
            if name_lower in seen:
                continue
            seen.add(name_lower)

            if allowed_names is not None and clean_name not in allowed_names and name_lower not in allowed_names:
                continue

            if ing.cost_per_unit <= Decimal("0.00"):
                continue

            existing = self.repository.get_catalog_ingredient(clean_name)
            if existing is not None:
                if existing.cost_per_unit == ing.cost_per_unit:
                    continue
                items_to_save.append(
                    CatalogIngredient(
                        id=existing.id,
                        name=existing.name,
                        cost_per_unit=ing.cost_per_unit,
                        supplier=existing.supplier,
                        sku=existing.sku,
                        notes=existing.notes,
                        updated_at=timestamp,
                    )
                )
            else:
                items_to_save.append(
                    CatalogIngredient(
                        name=clean_name,
                        cost_per_unit=ing.cost_per_unit,
                        updated_at=timestamp,
                    )
                )

        if items_to_save:
            self.repository.save_catalog_ingredients(items_to_save)
        return len(items_to_save)

    def prepare_imported_recipe(
        self,
        candidate: Recipe,
        collision_mode: str = "copy",
        timestamp: str | None = None,
    ) -> Recipe:
        """Prepare an imported recipe for persistence, resolving versioning and ID collisions."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        existing = self.repository.get_recipe(candidate.id, include_archived=True)

        if existing is not None:
            if collision_mode == "copy":
                return Recipe(
                    id=str(uuid.uuid4()),
                    name=f"{candidate.name} (Imported Copy)",
                    description=candidate.description,
                    version=1,
                    updated_at=timestamp,
                    is_draft=candidate.is_draft,
                    ingredients=candidate.ingredients,
                )
            if collision_mode == "overwrite":
                next_ver = max(existing.version, candidate.version) + 1
                return Recipe(
                    id=existing.id,
                    name=candidate.name,
                    description=candidate.description,
                    version=next_ver,
                    updated_at=timestamp,
                    is_draft=candidate.is_draft,
                    ingredients=candidate.ingredients,
                )
            raise ValueError(
                f"Recipe with ID '{candidate.id}' already exists as '{existing.name}' (v{existing.version})."
            )

        return Recipe(
            id=candidate.id or str(uuid.uuid4()),
            name=candidate.name,
            description=candidate.description,
            version=1,
            updated_at=timestamp,
            is_draft=candidate.is_draft,
            ingredients=candidate.ingredients,
        )

    def import_recipe(
        self,
        candidate: Recipe,
        collision_mode: str = "copy",
        timestamp: str | None = None,
    ) -> Recipe:
        """Prepare and persist an imported recipe according to collision policy."""
        prepared = self.prepare_imported_recipe(candidate, collision_mode=collision_mode, timestamp=timestamp)
        self.repository.save_recipe(prepared)
        return prepared
