"""Full workspace and catalog JSON serialization, inspection, rotation, and atomic database restoration."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import tared
from tared.core.calculator import PercentageSumError, validate_recipe_percentages
from tared.core.models import CatalogIngredient, Ingredient, Recipe
from tared.storage.base import RecipeRepository
from tared.storage.sqlite_repo import SQLiteRecipeRepository

WORKSPACE_SCHEMA_VERSION = "1.0"
CATALOG_SCHEMA_VERSION = "1.0"


def rotate_backups(dest_path: Path, max_rotations: int = 3) -> None:
    """Rotate existing backup files up to max_rotations generations (.1.json, .2.json, .3.json)."""
    if not dest_path.is_file() or max_rotations <= 0:
        return

    for i in range(max_rotations, 1, -1):
        src_i = dest_path.parent / f"{dest_path.stem}.{i - 1}{dest_path.suffix}"
        dst_i = dest_path.parent / f"{dest_path.stem}.{i}{dest_path.suffix}"
        if src_i.is_file():
            try:
                os.replace(str(src_i), str(dst_i))
            except OSError:
                pass

    first_rot = dest_path.parent / f"{dest_path.stem}.1{dest_path.suffix}"
    try:
        shutil.copy2(str(dest_path), str(first_rot))
        with open(first_rot, "rb") as rf:
            os.fsync(rf.fileno())
    except OSError:
        pass


def export_workspace_to_dict(repo: RecipeRepository) -> dict[str, Any]:
    """Serialize the entire application database (catalog, recipes, revisions, batches) to a dictionary."""
    snapshot = repo.export_workspace_snapshot()

    return {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": getattr(tared, "__version__", "1.0.0"),
        "catalog": snapshot["catalog"],
        "recipes": snapshot["recipes"],
        "revisions": snapshot["revisions"],
        "batches": snapshot["batches"],
    }


def export_workspace_to_json_file(
    repo: SQLiteRecipeRepository,
    file_path: str | Path,
    indent: int = 2,
    max_rotations: int = 0,
) -> Path:
    """Atomically export the workspace with multi-generation backup rotation (.1.json, .2.json, etc.)."""
    dest_path = Path(file_path).expanduser().resolve()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    data = export_workspace_to_dict(repo)
    serialized = json.dumps(data, indent=indent, ensure_ascii=False)

    staging_path = dest_path.parent / f".tmp_{dest_path.name}_{uuid.uuid4().hex[:8]}"
    try:
        with open(staging_path, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        if dest_path.is_file() and max_rotations > 0:
            rotate_backups(dest_path, max_rotations=max_rotations)
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

    return dest_path


def inspect_workspace_file(file_path: str | Path) -> dict[str, Any]:
    """Inspect and validate a workspace backup, checking structure across catalog, formulas, and batches."""
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Workspace backup file not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as err:
        raise ValueError(f"Failed to parse workspace backup as JSON: {err}") from err

    if not isinstance(data, dict):
        raise ValueError("Invalid workspace format: root must be a JSON object.")

    schema_version = str(data.get("schema_version", ""))
    if schema_version != WORKSPACE_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported workspace schema version: '{schema_version}' (expected '{WORKSPACE_SCHEMA_VERSION}')."
        )

    catalog = data.get("catalog", [])
    recipes = data.get("recipes", [])
    revisions = data.get("revisions", [])
    batches = data.get("batches", [])

    if not isinstance(catalog, list) or not isinstance(recipes, list):
        raise ValueError("Malformed workspace backup: 'catalog' and 'recipes' must be lists.")
    if not isinstance(batches, list):
        raise ValueError("Malformed workspace backup: 'batches' must be a list.")
    if not isinstance(revisions, list):
        raise ValueError("Malformed workspace backup: 'revisions' must be a list.")

    # 1. Validate catalog items
    for idx, c in enumerate(catalog):
        if not isinstance(c, dict):
            raise ValueError(f"Malformed catalog item at index {idx}: must be an object.")
        name = str(c.get("name", "")).strip()
        if not name:
            raise ValueError(f"Malformed catalog item at index {idx}: name cannot be empty.")
        try:
            cost = Decimal(str(c.get("cost_per_unit", "0.00")))
            if not cost.is_finite() or cost < Decimal("0"):
                raise ValueError("Cost must be a finite non-negative number")
        except (InvalidOperation, ValueError) as err:
            raise ValueError(f"Invalid cost for catalog ingredient '{name}': {err}") from err

    draft_recipes: list[str] = []
    draft_revisions: list[str] = []

    # 2. Structural & referential validation for revision snapshots
    seen_revisions: set[tuple[str, int]] = set()
    revisions_map: dict[tuple[str, int], list[dict[str, str]]] = {}

    for idx, rev in enumerate(revisions):
        if not isinstance(rev, dict):
            raise ValueError(f"Malformed revision at index {idx}: entry must be a dictionary.")
        rec_id = str(rev.get("recipe_id", "")).strip()
        if not rec_id:
            raise ValueError(f"Malformed revision at index {idx}: missing required 'recipe_id'.")
        if "version" not in rev:
            raise ValueError(f"Malformed revision at index {idx}: missing required 'version'.")
        try:
            rev_ver = int(rev["version"])
        except (ValueError, TypeError) as ver_err:
            raise ValueError(f"Malformed revision at index {idx}: invalid 'version': {ver_err}") from ver_err

        rev_key = (rec_id, rev_ver)
        if rev_key in seen_revisions:
            raise ValueError(f"Duplicate revision snapshot found for recipe ID '{rec_id}' version {rev_ver}.")
        seen_revisions.add(rev_key)

        rev_name = rev.get("name", f"Revision #{idx + 1}")
        ing_raw = rev.get("ingredients_json", "[]")
        try:
            ing_items = json.loads(ing_raw)
            if not isinstance(ing_items, list) or not ing_items:
                raise ValueError("ingredients_json must be a non-empty list of ingredients")
            parsed_rev_ings = tuple(
                Ingredient(
                    name=str(it.get("name", "")),
                    percentage=Decimal(str(it.get("percentage", "0"))),
                    cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
                )
                for it in ing_items
            )
            revisions_map[rev_key] = [
                {
                    "name": str(it.get("name", "")).strip(),
                    "percentage": str(it.get("percentage", "0")),
                    "cost_per_unit": str(it.get("cost_per_unit", "0.00")),
                }
                for it in ing_items
            ]
            rev_recipe = Recipe(
                id=rec_id,
                name=str(rev_name),
                description=str(rev.get("description", "")),
                version=rev_ver,
                ingredients=parsed_rev_ings,
            )
        except Exception as parse_err:
            raise ValueError(f"Revision '{rev_name}' has invalid ingredients_json: {parse_err}") from parse_err

        try:
            validate_recipe_percentages(rev_recipe)
        except PercentageSumError:
            draft_revisions.append(f"{rev_name} (v{rev_ver})")

    # 3. Structural, numeric, and cross-revision validation for active/archived recipes
    seen_recipe_ids: set[str] = set()
    for idx, r in enumerate(recipes):
        if not isinstance(r, dict):
            raise ValueError(f"Malformed recipe at index {idx}: must be an object.")
        r_id = str(r.get("id", "")).strip()
        if not r_id:
            raise ValueError(f"Malformed recipe at index {idx}: missing required 'id'.")
        if r_id in seen_recipe_ids:
            raise ValueError(f"Duplicate recipe ID '{r_id}' found in workspace backup.")
        seen_recipe_ids.add(r_id)

        r_name = str(r.get("name", "")).strip()
        if not r_name:
            raise ValueError(f"Malformed recipe at index {idx}: formula name cannot be empty.")
        try:
            r_ver = int(r.get("version", 1))
        except (ValueError, TypeError) as ver_err:
            raise ValueError(f"Recipe '{r_name}' has invalid 'version': {ver_err}") from ver_err

        raw_ings = r.get("ingredients", [])
        if not isinstance(raw_ings, list) or not raw_ings:
            raise ValueError(f"Recipe '{r_name}' must contain at least one ingredient.")
        r_is_draft = bool(r.get("is_draft", 0))
        try:
            parsed_ings = tuple(
                Ingredient(
                    name=str(it.get("name", "")),
                    percentage=Decimal(str(it.get("percentage", "0"))),
                    cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
                )
                for it in raw_ings
            )
            rec = Recipe(
                id=r_id,
                name=r_name,
                description=str(r.get("description", "")),
                version=r_ver,
                is_draft=r_is_draft,
                ingredients=parsed_ings,
            )
        except Exception as err:
            raise ValueError(f"Recipe '{r_name}' contains malformed ingredient data: {err}") from err

        # Assert parity with revision snapshot at same (id, version) when revisions exist
        rev_key = (r_id, r_ver)
        if r_is_draft and rev_key in revisions_map:
            raise ValueError(
                f"Integrity violation in backup: Draft recipe '{r_name}' (id='{r_id}', v{r_ver}) "
                f"conflicts with an immutable revision snapshot at v{r_ver}."
            )
        if rev_key in revisions_map:
            rev_ings = revisions_map[rev_key]
            if len(rec.ingredients) != len(rev_ings):
                raise ValueError(
                    f"Integrity violation in backup: Recipe '{r_name}' (id='{r_id}', v{r_ver}) "
                    f"ingredient count ({len(rec.ingredients)}) does not match its revision snapshot ({len(rev_ings)})."
                )
            for r_ing, rev_ing in zip(rec.ingredients, rev_ings):
                if (
                    r_ing.name.strip() != rev_ing["name"]
                    or r_ing.percentage != Decimal(rev_ing["percentage"])
                    or r_ing.cost_per_unit != Decimal(rev_ing["cost_per_unit"])
                ):
                    raise ValueError(
                        f"Integrity violation in backup: Recipe '{r_name}' (id='{r_id}', v{r_ver}) "
                        f"contradicts its immutable revision snapshot at the same version."
                    )

        try:
            validate_recipe_percentages(rec)
        except PercentageSumError:
            draft_recipes.append(r_name)

    # 4. Structural, numeric, and referential validation for historical production batches
    seen_batch_numbers: set[str] = set()
    for idx, b in enumerate(batches):
        if not isinstance(b, dict):
            raise ValueError(f"Malformed batch entry at index {idx}: entry must be a dictionary.")
        batch_num = str(b.get("batch_number", "")).strip()
        if not batch_num:
            raise ValueError(f"Malformed batch at index {idx}: 'batch_number' cannot be empty.")
        b_num_key = batch_num.lower()
        if b_num_key in seen_batch_numbers:
            raise ValueError(f"Duplicate batch number '{batch_num}' found in workspace backup.")
        seen_batch_numbers.add(b_num_key)

        b_rec_id = str(b.get("recipe_id", "")).strip()
        if not b_rec_id:
            raise ValueError(f"Batch '{batch_num}' missing required 'recipe_id'.")
        try:
            b_ver = int(b.get("recipe_version", 1))
            tw = Decimal(str(b.get("target_weight", "0")))
            tot_w = Decimal(str(b.get("total_weight", "0")))
            tot_c = Decimal(str(b.get("total_cost", "0")))
            if not (tw.is_finite() and tot_w.is_finite() and tot_c.is_finite()):
                raise ValueError("Batch metrics must be finite numbers.")
        except (InvalidOperation, ValueError, TypeError) as err:
            raise ValueError(f"Batch '{batch_num}' contains invalid numeric metrics: {err}") from err

        if revisions and (b_rec_id, b_ver) not in seen_revisions:
            raise ValueError(
                f"Batch '{batch_num}' references non-existent recipe revision "
                f"(recipe_id='{b_rec_id}', version={b_ver})."
            )

        raw_items_json = b.get("items_json", "[]")
        try:
            parsed_items = json.loads(raw_items_json)
            if not isinstance(parsed_items, list):
                raise ValueError("items_json must be a JSON array")
        except Exception as items_err:
            raise ValueError(f"Batch '{batch_num}' contains malformed items_json: {items_err}") from items_err

    archived_count = sum(1 for r in recipes if isinstance(r, dict) and r.get("is_archived", 0) == 1)

    return {
        "schema_version": schema_version,
        "exported_at": str(data.get("exported_at", "Unknown")),
        "app_version": str(data.get("app_version", "Unknown")),
        "catalog_count": len(catalog),
        "recipe_count": len(recipes) - archived_count,
        "archived_recipe_count": archived_count,
        "revision_count": len(revisions),
        "batch_count": len(batches),
        "draft_recipes": tuple(draft_recipes),
        "draft_revisions": tuple(draft_revisions),
        "data": data,
    }


def restore_workspace_from_json(
    repo: SQLiteRecipeRepository,
    file_path: str | Path,
    pre_restore_backup_dir: str | Path | None = None,
) -> Path | None:
    """Pre-flight validate, safety snapshot, and atomically restore workspace with foreign key verification."""
    inspection = inspect_workspace_file(file_path)
    data = inspection["data"]

    backup_dir = Path(pre_restore_backup_dir).expanduser().resolve() if pre_restore_backup_dir else None
    return repo.restore_workspace_data(data, pre_restore_backup_dir=backup_dir)


def export_catalog_to_json_file(
    repo: RecipeRepository,
    file_path: str | Path,
    indent: int = 2,
) -> Path:
    """Atomically export all raw materials and prices to a standalone catalog JSON file."""
    dest_path = Path(file_path).expanduser().resolve()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    items = [
        {
            "id": c.id,
            "name": c.name,
            "cost_per_unit": str(c.cost_per_unit),
            "supplier": c.supplier,
            "sku": c.sku,
            "notes": c.notes,
            "updated_at": c.updated_at,
        }
        for c in repo.list_catalog_ingredients()
    ]
    payload = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "catalog": items,
    }

    serialized = json.dumps(payload, indent=indent, ensure_ascii=False)
    staging_path = dest_path.parent / f".tmp_{dest_path.name}_{uuid.uuid4().hex[:8]}"
    try:
        with open(staging_path, "w", encoding="utf-8") as f:
            f.write(serialized)
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

    return dest_path


def import_catalog_from_json_file(repo: RecipeRepository, file_path: str | Path) -> int:
    """Parse, pre-validate, and atomically upsert raw materials from a catalog JSON file."""
    path = Path(file_path).expanduser().resolve()
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as err:
        raise ValueError(f"Failed to read catalog JSON file: {err}") from err

    raw_items = data.get("catalog", []) if isinstance(data, dict) else data
    if not isinstance(raw_items, list):
        raise ValueError("Invalid catalog format: expected a 'catalog' list of items.")

    items_to_save: list[CatalogIngredient] = []
    for idx, it in enumerate(raw_items):
        if not isinstance(it, dict):
            raise ValueError(f"Malformed catalog item at index {idx}: entry must be an object.")
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        try:
            cost = Decimal(str(it.get("cost_per_unit", "0.00")))
            item = CatalogIngredient(
                id=str(it.get("id") or uuid.uuid4()),
                name=name,
                cost_per_unit=cost,
                supplier=str(it.get("supplier", "")),
                sku=str(it.get("sku", "")),
                notes=str(it.get("notes", "")),
                updated_at=str(it.get("updated_at", "")),
            )
            items_to_save.append(item)
        except (InvalidOperation, TypeError, ValueError) as err:
            raise ValueError(f"Invalid catalog item '{name}' numeric data: {err}") from err

    if items_to_save:
        repo.save_catalog_ingredients(items_to_save)
    return len(items_to_save)
