"""JSON serialization and deserialization for discrete recipe interchange."""

import json
import os
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tared.core.models import Ingredient, Recipe


def recipe_to_dict(recipe: Recipe) -> dict[str, Any]:
    """Serialize Recipe model to a schema-versioned dictionary."""
    return {
        "version": "1.0",
        "id": recipe.id,
        "name": recipe.name,
        "description": recipe.description,
        "recipe_version": recipe.version,
        "updated_at": recipe.updated_at,
        "is_draft": recipe.is_draft,
        "ingredients": [
            {
                "name": ing.name,
                "percentage": str(ing.percentage),
                "cost_per_unit": str(ing.cost_per_unit),
            }
            for ing in recipe.ingredients
        ],
    }


def recipe_from_dict(data: dict[str, Any]) -> Recipe:
    """Deserialize Recipe model from a dictionary with strict validation."""
    if not isinstance(data, dict):
        raise ValueError("Malformed recipe JSON: root must be an object.")
    if "name" not in data or "ingredients" not in data:
        raise ValueError("Malformed recipe JSON: missing required 'name' or 'ingredients' keys")

    schema_version = str(data.get("version", "1.0"))
    if schema_version != "1.0":
        raise ValueError(f"Unsupported recipe schema version: '{schema_version}' (expected '1.0')")

    raw_ingredients = data["ingredients"]
    if not isinstance(raw_ingredients, list):
        raise ValueError("Malformed recipe JSON: 'ingredients' must be a list.")

    parsed_ingredients = []
    for item in raw_ingredients:
        if not isinstance(item, dict):
            raise ValueError("Malformed recipe JSON: ingredient item must be an object.")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError("Malformed recipe JSON: ingredient name cannot be empty")
        pct_str = str(item.get("percentage", "")).replace(",", ".")
        cost_str = str(item.get("cost_per_unit", "0.00")).replace(",", ".")
        try:
            parsed_ingredients.append(
                Ingredient(
                    name=name,
                    percentage=Decimal(pct_str),
                    cost_per_unit=Decimal(cost_str),
                )
            )
        except (InvalidOperation, TypeError, ValueError) as err:
            raise ValueError(f"Malformed ingredient numeric data for '{name}': {err}") from err

    raw_id = data.get("id")
    recipe_id = str(raw_id).strip() if raw_id and str(raw_id).strip() else str(uuid.uuid4())

    try:
        return Recipe(
            id=recipe_id,
            name=str(data["name"]).strip(),
            description=str(data.get("description") or ""),
            version=int(data.get("recipe_version", 1)),
            updated_at=str(data.get("updated_at") or ""),
            is_draft=bool(data.get("is_draft", False)),
            ingredients=tuple(parsed_ingredients),
        )
    except Exception as err:
        raise ValueError(f"Invalid recipe formulation data: {err}") from err


def export_recipe_to_json(recipe: Recipe, indent: int = 2) -> str:
    return json.dumps(recipe_to_dict(recipe), indent=indent)


def import_recipe_from_json(json_content: str) -> Recipe:
    try:
        data = json.loads(json_content)
    except json.JSONDecodeError as err:
        raise ValueError(f"Invalid JSON syntax in recipe content: {err}") from err
    return recipe_from_dict(data)


def save_recipe_to_file(recipe: Recipe, file_path: str | Path) -> None:
    dest_path = Path(file_path).resolve()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = dest_path.parent / f".tmp_{dest_path.name}_{uuid.uuid4().hex[:8]}"
    try:
        content = export_recipe_to_json(recipe)
        with open(staging_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(staging_path), str(dest_path))
        try:
            dir_fd = os.open(str(dest_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if staging_path.exists():
            try:
                staging_path.unlink()
            except OSError:
                pass


def load_recipe_from_file(file_path: str | Path) -> Recipe:
    path = Path(file_path)
    return import_recipe_from_json(path.read_text(encoding="utf-8"))
