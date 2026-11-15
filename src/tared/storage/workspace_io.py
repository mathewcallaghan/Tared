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
from tared.storage.base import RecipeRepository, parse_bool_flag
from tared.storage.sqlite_repo import _sqlite_nocase_key

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
    repo: RecipeRepository,
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
    seen_catalog_ids: set[str] = set()
    seen_catalog_names: set[str] = set()
    for idx, c in enumerate(catalog):
        if not isinstance(c, dict):
            raise ValueError(f"Malformed catalog item at index {idx}: must be an object.")
        raw_cat_id = c.get("id")
        if raw_cat_id is not None and str(raw_cat_id).strip():
            cat_id = str(raw_cat_id).strip()
            if cat_id in seen_catalog_ids:
                raise ValueError(f"Duplicate catalog ID '{cat_id}' found in workspace backup.")
            seen_catalog_ids.add(cat_id)
        raw_cat_name = c.get("name")
        if not isinstance(raw_cat_name, str) or not raw_cat_name.strip():
            raise ValueError(f"Malformed catalog item at index {idx}: name must be a non-empty string.")
        name = raw_cat_name.strip()
        cat_key = _sqlite_nocase_key(name)
        if cat_key in seen_catalog_names:
            raise ValueError(f"Duplicate catalog material name '{name}' found in workspace backup.")
        seen_catalog_names.add(cat_key)
        try:
            raw_cat_cost = c.get("cost_per_unit")
            cost = Decimal(str(raw_cat_cost if raw_cat_cost is not None else "0.00"))
            if not cost.is_finite() or cost < Decimal("0"):
                raise ValueError("Cost must be a finite non-negative number")
        except (InvalidOperation, ValueError) as err:
            raise ValueError(f"Invalid cost for catalog ingredient '{name}': {err}") from err

    draft_recipes: list[str] = []
    draft_revisions: list[str] = []

    # 2. Structural & referential validation for revision snapshots
    seen_revisions: set[tuple[str, int]] = set()
    revisions_map: dict[tuple[str, int], dict[str, Any]] = {}

    for idx, rev in enumerate(revisions):
        if not isinstance(rev, dict):
            raise ValueError(f"Malformed revision at index {idx}: entry must be a dictionary.")
        raw_rec_id = rev.get("recipe_id")
        if not isinstance(raw_rec_id, str) or not raw_rec_id.strip():
            raise ValueError(f"Malformed revision at index {idx}: missing required 'recipe_id'.")
        rec_id = raw_rec_id.strip()
        if "version" not in rev:
            raise ValueError(f"Malformed revision at index {idx}: missing required 'version'.")
        raw_rev_ver = rev["version"]
        if isinstance(raw_rev_ver, (bool, float)) or raw_rev_ver is None:
            raise ValueError(f"Malformed revision at index {idx}: 'version' must be an integer.")
        try:
            rev_ver = int(raw_rev_ver)
        except (ValueError, TypeError) as ver_err:
            raise ValueError(f"Malformed revision at index {idx}: invalid 'version': {ver_err}") from ver_err
        if rev_ver < 1:
            raise ValueError(f"Malformed revision at index {idx}: 'version' must be >= 1, got {rev_ver}.")

        rev_key = (rec_id, rev_ver)
        if rev_key in seen_revisions:
            raise ValueError(f"Duplicate revision snapshot found for recipe ID '{rec_id}' version {rev_ver}.")
        seen_revisions.add(rev_key)

        raw_rev_name = rev.get("name")
        if not isinstance(raw_rev_name, str) or not raw_rev_name.strip():
            raise ValueError(f"Malformed revision at index {idx}: 'name' must be a non-empty string.")
        rev_name = raw_rev_name
        ing_raw = rev.get("ingredients_json", "[]")
        try:
            ing_items = json.loads(ing_raw)
            if not isinstance(ing_items, list) or not ing_items:
                raise ValueError("ingredients_json must be a non-empty list of ingredients")
            parsed_rev_ings = []
            mapped_rev_ings = []
            for it in ing_items:
                if not isinstance(it, dict):
                    raise ValueError("ingredient entry must be an object")
                it_name = it.get("name")
                if not isinstance(it_name, str) or not it_name.strip():
                    raise ValueError("ingredient name must be a non-empty string")
                it_pct = it.get("percentage")
                if it_pct is None:
                    raise ValueError(f"ingredient '{it_name}' missing percentage")
                it_cost = it.get("cost_per_unit")
                cost_str = str(it_cost if it_cost is not None else "0.00")
                parsed_rev_ings.append(
                    Ingredient(
                        name=it_name,
                        percentage=Decimal(str(it_pct)),
                        cost_per_unit=Decimal(cost_str),
                    )
                )
                mapped_rev_ings.append(
                    {
                        "name": it_name.strip(),
                        "percentage": str(it_pct),
                        "cost_per_unit": cost_str,
                    }
                )
            rev_desc = str(rev.get("description") or "")
            revisions_map[rev_key] = {
                "name": rev_name.strip(),
                "description": rev_desc,
                "ingredients": mapped_rev_ings,
            }
            rev_recipe = Recipe(
                id=rec_id,
                name=str(rev_name),
                description=rev_desc,
                version=rev_ver,
                ingredients=tuple(parsed_rev_ings),
            )
        except Exception as parse_err:
            raise ValueError(f"Revision '{rev_name}' has invalid ingredients_json: {parse_err}") from parse_err

        try:
            validate_recipe_percentages(rev_recipe)
        except PercentageSumError:
            draft_revisions.append(f"{rev_name} (v{rev_ver})")

    # 3. Structural, numeric, and cross-revision validation for active/archived recipes
    seen_recipe_ids: set[str] = set()
    recipe_is_draft_by_id: dict[str, bool] = {}
    non_draft_recipe_versions: dict[str, int] = {}
    for idx, r in enumerate(recipes):
        if not isinstance(r, dict):
            raise ValueError(f"Malformed recipe at index {idx}: must be an object.")
        raw_r_id = r.get("id")
        if not isinstance(raw_r_id, str) or not raw_r_id.strip():
            raise ValueError(f"Malformed recipe at index {idx}: missing required 'id'.")
        r_id = raw_r_id.strip()
        if r_id in seen_recipe_ids:
            raise ValueError(f"Duplicate recipe ID '{r_id}' found in workspace backup.")
        seen_recipe_ids.add(r_id)

        raw_r_name = r.get("name")
        if not isinstance(raw_r_name, str) or not raw_r_name.strip():
            raise ValueError(f"Malformed recipe at index {idx}: formula name cannot be empty.")
        r_name = raw_r_name.strip()
        raw_r_ver = r.get("version")
        if isinstance(raw_r_ver, (bool, float)):
            raise ValueError(f"Recipe '{r_name}' has invalid 'version': must be an integer.")
        try:
            r_ver = 1 if raw_r_ver is None else int(raw_r_ver)
        except (ValueError, TypeError) as ver_err:
            raise ValueError(f"Recipe '{r_name}' has invalid 'version': {ver_err}") from ver_err
        if r_ver < 1:
            raise ValueError(f"Recipe '{r_name}' has invalid 'version': must be >= 1, got {r_ver}.")

        raw_ings = r.get("ingredients", [])
        if not isinstance(raw_ings, list) or not raw_ings:
            raise ValueError(f"Recipe '{r_name}' must contain at least one ingredient.")
        r_is_draft = parse_bool_flag(r.get("is_draft", 0))
        recipe_is_draft_by_id[r_id] = r_is_draft
        if not r_is_draft:
            non_draft_recipe_versions[r_id] = r_ver
        try:
            parsed_ings_list = []
            for it in raw_ings:
                if not isinstance(it, dict):
                    raise ValueError("ingredient entry must be an object")
                it_name = it.get("name")
                if not isinstance(it_name, str) or not it_name.strip():
                    raise ValueError("ingredient name must be a non-empty string")
                it_pct = it.get("percentage")
                if it_pct is None:
                    raise ValueError(f"ingredient '{it_name}' missing percentage")
                it_cost = it.get("cost_per_unit")
                cost_str = str(it_cost if it_cost is not None else "0.00")
                parsed_ings_list.append(
                    Ingredient(
                        name=it_name,
                        percentage=Decimal(str(it_pct)),
                        cost_per_unit=Decimal(cost_str),
                    )
                )
            parsed_ings = tuple(parsed_ings_list)
            rec = Recipe(
                id=r_id,
                name=r_name,
                description=str(r.get("description") or ""),
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
            rev_snap = revisions_map[rev_key]
            if r_name != rev_snap["name"] or rec.description != rev_snap["description"]:
                raise ValueError(
                    f"Integrity violation in backup: Recipe '{r_name}' (id='{r_id}', v{r_ver}) "
                    f"contradicts its immutable revision snapshot at the same version."
                )
            rev_ings = rev_snap["ingredients"]
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

    for rev in revisions:
        rev_recipe_id = str(rev.get("recipe_id", "")).strip()
        if rev_recipe_id not in seen_recipe_ids:
            rev_label = rev.get("name", "Unknown Revision")
            raise ValueError(
                f"Revision '{rev_label}' references non-existent recipe ID '{rev_recipe_id}'."
            )

    # 4. Structural, numeric, and referential validation for historical production batches
    seen_batch_numbers: set[str] = set()
    for idx, b in enumerate(batches):
        if not isinstance(b, dict):
            raise ValueError(f"Malformed batch entry at index {idx}: entry must be a dictionary.")
        raw_batch_num = b.get("batch_number")
        if not isinstance(raw_batch_num, str) or not raw_batch_num.strip():
            raise ValueError(f"Malformed batch at index {idx}: 'batch_number' cannot be empty.")
        batch_num = raw_batch_num.strip()
        b_num_key = _sqlite_nocase_key(batch_num)
        if b_num_key in seen_batch_numbers:
            raise ValueError(f"Duplicate batch number '{batch_num}' found in workspace backup.")
        seen_batch_numbers.add(b_num_key)

        raw_b_rec_id = b.get("recipe_id")
        if not isinstance(raw_b_rec_id, str) or not raw_b_rec_id.strip():
            raise ValueError(f"Batch '{batch_num}' missing required 'recipe_id'.")
        b_rec_id = raw_b_rec_id.strip()
        if "recipe_version" not in b:
            raise ValueError(f"Batch '{batch_num}' missing required 'recipe_version'.")
        raw_b_ver = b["recipe_version"]
        if isinstance(raw_b_ver, (bool, float)) or raw_b_ver is None:
            raise ValueError(f"Batch '{batch_num}' contains invalid 'recipe_version'.")
        for req_metric in ("target_weight", "total_weight", "total_cost"):
            if req_metric not in b or b[req_metric] is None:
                raise ValueError(f"Batch '{batch_num}' missing required '{req_metric}'.")
        try:
            b_ver = int(raw_b_ver)
            tw = Decimal(str(b["target_weight"]))
            tot_w = Decimal(str(b["total_weight"]))
            tot_c = Decimal(str(b["total_cost"]))
            raw_prec = b.get("precision")
            prec_val = Decimal(str(raw_prec if raw_prec not in (None, "") else "0.01"))
            if not (tw.is_finite() and tot_w.is_finite() and tot_c.is_finite()):
                raise ValueError("Batch metrics must be finite numbers.")
            if tw <= Decimal("0") or tot_w <= Decimal("0") or tot_c < Decimal("0"):
                raise ValueError("Batch target/total weight must be positive and total cost non-negative.")
            if not prec_val.is_finite() or prec_val <= Decimal("0"):
                raise ValueError("Batch precision must be a finite positive number.")
        except (InvalidOperation, ValueError, TypeError) as err:
            raise ValueError(f"Batch '{batch_num}' contains invalid numeric metrics: {err}") from err

        if revisions:
            if (b_rec_id, b_ver) not in seen_revisions:
                raise ValueError(
                    f"Batch '{batch_num}' references non-existent recipe revision "
                    f"(recipe_id='{b_rec_id}', version={b_ver})."
                )
        else:
            if b_rec_id not in seen_recipe_ids:
                raise ValueError(
                    f"Batch '{batch_num}' references non-existent recipe ID '{b_rec_id}'."
                )
            if recipe_is_draft_by_id.get(b_rec_id, False):
                raise ValueError(
                    f"Batch '{batch_num}' references draft recipe ID '{b_rec_id}' which has no committed revisions."
                )
            if non_draft_recipe_versions.get(b_rec_id) != b_ver:
                raise ValueError(
                    f"Batch '{batch_num}' references recipe version {b_ver}, "
                    f"which does not match recipe '{b_rec_id}' version {non_draft_recipe_versions.get(b_rec_id)} "
                    "when revisions are absent."
                )

        raw_cost_decimals = b.get("cost_decimals")
        if raw_cost_decimals is not None:
            if isinstance(raw_cost_decimals, (bool, float)):
                raise ValueError(f"Batch '{batch_num}' contains invalid 'cost_decimals'.")
            try:
                cd_val = int(raw_cost_decimals)
            except (ValueError, TypeError) as cd_err:
                raise ValueError(f"Batch '{batch_num}' contains invalid 'cost_decimals': {cd_err}") from cd_err
            if cd_val < 0 or cd_val > 10:
                raise ValueError(
                    f"Batch '{batch_num}' contains invalid 'cost_decimals': must be between 0 and 10, got {cd_val}."
                )

        raw_items_json = b.get("items_json")
        if raw_items_json is None:
            raw_items_json = "[]"
        elif not isinstance(raw_items_json, str):
            raise ValueError(f"Batch '{batch_num}' contains malformed items_json: expected JSON string.")
        try:
            parsed_items = json.loads(raw_items_json)
            if not isinstance(parsed_items, list) or any(not isinstance(it, dict) for it in parsed_items):
                raise ValueError("items_json must be a JSON array of ingredient objects")
        except Exception as items_err:
            raise ValueError(f"Batch '{batch_num}' contains malformed items_json: {items_err}") from items_err

    archived_count = sum(1 for r in recipes if isinstance(r, dict) and parse_bool_flag(r.get("is_archived", False)))

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
    repo: RecipeRepository,
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

    if isinstance(data, dict):
        if "catalog" not in data:
            raise ValueError("Invalid catalog format: missing required 'catalog' list.")
        raw_items = data["catalog"]
    else:
        raw_items = data
    if not isinstance(raw_items, list):
        raise ValueError("Invalid catalog format: expected a 'catalog' list of items.")

    items_to_save: list[CatalogIngredient] = []
    seen_names: set[str] = set()
    for idx, it in enumerate(raw_items):
        if not isinstance(it, dict):
            raise ValueError(f"Malformed catalog item at index {idx}: entry must be an object.")
        raw_name = it.get("name")
        if raw_name is None or not isinstance(raw_name, str):
            raise ValueError(f"Malformed catalog item at index {idx}: 'name' must be a string.")
        name = raw_name.strip()
        if not name:
            continue
        try:
            raw_cost = it.get("cost_per_unit")
            cost = Decimal(str(raw_cost if raw_cost is not None else "0.00"))
            item = CatalogIngredient(
                id=str(it.get("id") or uuid.uuid4()),
                name=name,
                cost_per_unit=cost,
                supplier=str(it.get("supplier") or ""),
                sku=str(it.get("sku") or ""),
                notes=str(it.get("notes") or ""),
                updated_at=str(it.get("updated_at") or ""),
            )
            items_to_save.append(item)
            seen_names.add(_sqlite_nocase_key(name))
        except (InvalidOperation, TypeError, ValueError) as err:
            raise ValueError(f"Invalid catalog item '{name}' numeric data: {err}") from err

    if items_to_save:
        repo.save_catalog_ingredients(items_to_save)
    return len(seen_names)
