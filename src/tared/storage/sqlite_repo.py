"""SQLite persistence layer with immutable revision snapshots, production batch logging, and soft archiving."""

import json
import logging
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tared.core.models import CatalogIngredient, Ingredient, Recipe
from tared.core.service import has_formulation_changed
from tared.storage.base import DuplicateBatchNumberError, RecipeNotFoundError, RecipeRepository, StorageError

logger = logging.getLogger(__name__)


class SQLiteRecipeRepository(RecipeRepository):
    """SQLite repository enforcing revision history and production batch logging."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._is_memory = self.db_path == ":memory:" or "mode=memory" in self.db_path
        self._mem_conn: sqlite3.Connection | None = None

        if self._is_memory:
            self._mem_conn = self._create_connection()

        self._init_db()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        return self._create_connection()

    def _init_db(self) -> None:
        conn = self._get_connection()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT '',
                    is_archived INTEGER NOT NULL DEFAULT 0,
                    is_draft INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS ingredients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipe_id TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    percentage TEXT NOT NULL,
                    cost_per_unit TEXT NOT NULL,
                    FOREIGN KEY (recipe_id) REFERENCES recipes (id) ON DELETE CASCADE,
                    UNIQUE (recipe_id, sort_order)
                );

                CREATE TABLE IF NOT EXISTS recipe_revisions (
                    id TEXT PRIMARY KEY,
                    recipe_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    ingredients_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (recipe_id) REFERENCES recipes (id) ON DELETE RESTRICT,
                    UNIQUE (recipe_id, version)
                );

                CREATE TRIGGER IF NOT EXISTS prevent_revision_update
                BEFORE UPDATE ON recipe_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'Recipe revisions are immutable and cannot be updated.');
                END;

                CREATE TRIGGER IF NOT EXISTS prevent_revision_delete
                BEFORE DELETE ON recipe_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'Recipe revisions are immutable and cannot be deleted.');
                END;


                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    recipe_id TEXT NOT NULL,
                    recipe_version INTEGER NOT NULL,
                    batch_number TEXT NOT NULL UNIQUE,
                    operator TEXT NOT NULL,
                    precision TEXT NOT NULL DEFAULT '0.01',
                    target_weight TEXT NOT NULL,
                    total_weight TEXT NOT NULL,
                    total_cost TEXT NOT NULL,
                    items_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (recipe_id, recipe_version)
                        REFERENCES recipe_revisions (recipe_id, version) ON DELETE RESTRICT
                );

                CREATE TRIGGER IF NOT EXISTS prevent_batch_update
                BEFORE UPDATE ON batches
                BEGIN
                    SELECT RAISE(ABORT, 'Production batches are immutable and cannot be updated.');
                END;

                CREATE TRIGGER IF NOT EXISTS prevent_batch_delete
                BEFORE DELETE ON batches
                BEGIN
                    SELECT RAISE(ABORT, 'Production batches are immutable and cannot be deleted.');
                END;

                CREATE TABLE IF NOT EXISTS ingredients_catalog (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    cost_per_unit TEXT NOT NULL DEFAULT '0.00',
                    supplier TEXT NOT NULL DEFAULT '',
                    sku TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                """
            )

            # Migrations for existing databases
            user_version = conn.execute("PRAGMA user_version;").fetchone()[0]
            if user_version < 1:
                cursor = conn.execute("PRAGMA table_info(recipes);")
                recipe_cols = [row["name"] for row in cursor.fetchall()]
                if "version" not in recipe_cols:
                    conn.execute("ALTER TABLE recipes ADD COLUMN version INTEGER NOT NULL DEFAULT 1;")
                if "updated_at" not in recipe_cols:
                    conn.execute("ALTER TABLE recipes ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';")
                if "is_archived" not in recipe_cols:
                    conn.execute("ALTER TABLE recipes ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0;")

                batch_cursor = conn.execute("PRAGMA table_info(batches);")
                batch_cols = [row["name"] for row in batch_cursor.fetchall()]
                if "precision" not in batch_cols:
                    conn.execute("ALTER TABLE batches ADD COLUMN precision TEXT NOT NULL DEFAULT '0.01';")
                if "items_json" not in batch_cols:
                    conn.execute("ALTER TABLE batches ADD COLUMN items_json TEXT NOT NULL DEFAULT '[]';")

                # Migration: Backfill missing snapshots for unversioned existing recipes
                missing_revisions = conn.execute(
                    """
                    SELECT r.id, r.name, r.description, r.version, r.updated_at
                    FROM recipes r
                    LEFT JOIN recipe_revisions rv ON r.id = rv.recipe_id AND r.version = rv.version
                    WHERE rv.id IS NULL;
                    """
                ).fetchall()

                for r_row in missing_revisions:
                    r_id = r_row["id"]
                    ing_rows = conn.execute(
                        "SELECT name, percentage, cost_per_unit FROM ingredients "
                        "WHERE recipe_id = ? ORDER BY sort_order ASC;",
                        (r_id,),
                    ).fetchall()
                    ing_json = json.dumps(
                        [
                            {
                                "name": row["name"],
                                "percentage": row["percentage"],
                                "cost_per_unit": row["cost_per_unit"],
                            }
                            for row in ing_rows
                        ]
                    )
                    created_at = r_row["updated_at"] or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO recipe_revisions (
                            id, recipe_id, version, name, description, ingredients_json, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            str(uuid.uuid4()),
                            r_id,
                            r_row["version"],
                            r_row["name"],
                            r_row["description"],
                            ing_json,
                            created_at,
                        ),
                    )

                conn.execute("PRAGMA user_version = 1;")

            if user_version < 2:
                cursor = conn.execute("PRAGMA table_info(recipes);")
                recipe_cols = [row["name"] for row in cursor.fetchall()]
                if "is_draft" not in recipe_cols:
                    conn.execute("ALTER TABLE recipes ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0;")
                conn.execute("PRAGMA user_version = 2;")

        if not self._is_memory:
            conn.close()

    def _is_revision_equivalent(self, rev_row: sqlite3.Row | dict[str, Any], recipe: Recipe) -> bool:
        """Compare revision snapshot semantically against a Recipe candidate using Decimal values."""
        try:
            rev_items = json.loads(rev_row["ingredients_json"])
            rev_ingredients = tuple(
                Ingredient(
                    name=it["name"],
                    percentage=Decimal(str(it["percentage"])),
                    cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
                )
                for it in rev_items
            )
            rev_recipe = Recipe(
                id=recipe.id,
                name=rev_row["name"],
                description=rev_row["description"] or "",
                version=recipe.version,
                ingredients=rev_ingredients,
            )
            return not has_formulation_changed(rev_recipe, recipe)
        except Exception:
            return False

    def save_recipe(self, recipe: Recipe) -> None:
        """Save active recipe and record an immutable revision snapshot in a single atomic transaction."""
        conn = self._get_connection()
        ingredients_snapshot = json.dumps(
            [
                {
                    "name": ing.name,
                    "percentage": str(ing.percentage),
                    "cost_per_unit": str(ing.cost_per_unit),
                }
                for ing in recipe.ingredients
            ]
        )
        snapshot_id = str(uuid.uuid4())
        created_at = recipe.updated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        is_draft_int = 1 if recipe.is_draft else 0
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO recipes (id, name, description, version, updated_at, is_archived, is_draft)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        version = excluded.version,
                        updated_at = excluded.updated_at,
                        is_archived = 0,
                        is_draft = excluded.is_draft;
                    """,
                    (recipe.id, recipe.name, recipe.description, recipe.version, recipe.updated_at, is_draft_int),
                )
                conn.execute("DELETE FROM ingredients WHERE recipe_id = ?;", (recipe.id,))
                conn.executemany(
                    """
                    INSERT INTO ingredients (recipe_id, sort_order, name, percentage, cost_per_unit)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    [
                        (
                            recipe.id,
                            idx,
                            ing.name,
                            str(ing.percentage),
                            str(ing.cost_per_unit),
                        )
                        for idx, ing in enumerate(recipe.ingredients)
                    ],
                )
                existing_rev = conn.execute(
                    "SELECT name, description, ingredients_json FROM recipe_revisions "
                    "WHERE recipe_id = ? AND version = ?;",
                    (recipe.id, recipe.version),
                ).fetchone()
                if recipe.is_draft:
                    if existing_rev is not None:
                        raise StorageError(
                            f"Draft version conflict: Recipe '{recipe.name}' (id='{recipe.id}') "
                            f"already has an immutable revision at v{recipe.version}."
                        )
                elif existing_rev is None:
                    conn.execute(
                        """
                        INSERT INTO recipe_revisions (
                            id, recipe_id, version, name, description, ingredients_json, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            snapshot_id,
                            recipe.id,
                            recipe.version,
                            recipe.name,
                            recipe.description,
                            ingredients_snapshot,
                            created_at,
                        ),
                    )
                else:
                    if not self._is_revision_equivalent(existing_rev, recipe):
                        raise StorageError(
                            f"Revision integrity violation: Recipe '{recipe.name}' (id='{recipe.id}') "
                            f"revision v{recipe.version} already exists with differing formulation data. "
                            "Revisions are immutable and cannot be overwritten."
                        )
        finally:
            if not self._is_memory:
                conn.close()

    def get_recipe(self, recipe_id: str, include_archived: bool = False) -> Recipe | None:
        conn = self._get_connection()
        try:
            query = "SELECT id, name, description, version, updated_at, is_draft FROM recipes WHERE id = ?"
            if not include_archived:
                query += " AND is_archived = 0;"
            else:
                query += ";"

            recipe_row = conn.execute(query, (recipe_id,)).fetchone()

            if recipe_row is None:
                return None

            ingredient_rows = conn.execute(
                """
                SELECT name, percentage, cost_per_unit
                FROM ingredients
                WHERE recipe_id = ?
                ORDER BY sort_order ASC;
                """,
                (recipe_id,),
            ).fetchall()

            ingredients = tuple(
                Ingredient(
                    name=row["name"],
                    percentage=Decimal(row["percentage"]),
                    cost_per_unit=Decimal(row["cost_per_unit"]),
                )
                for row in ingredient_rows
            )

            return Recipe(
                id=recipe_row["id"],
                name=recipe_row["name"],
                description=recipe_row["description"],
                version=int(recipe_row["version"]),
                updated_at=recipe_row["updated_at"],
                is_draft=bool(recipe_row["is_draft"]),
                ingredients=ingredients,
            )
        finally:
            if not self._is_memory:
                conn.close()

    def get_recipe_revisions(self, recipe_id: str) -> list[dict[str, Any]]:
        """Retrieve all historical immutable snapshots for a recipe."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, recipe_id, version, name, description, ingredients_json, created_at
                FROM recipe_revisions
                WHERE recipe_id = ?
                ORDER BY version DESC;
                """,
                (recipe_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            if not self._is_memory:
                conn.close()

    def get_recipe_revision(self, recipe_id: str, version: int) -> dict[str, Any] | None:
        """Retrieve a specific historical immutable snapshot by recipe ID and version."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                """
                SELECT id, recipe_id, version, name, description, ingredients_json, created_at
                FROM recipe_revisions
                WHERE recipe_id = ? AND version = ?
                LIMIT 1;
                """,
                (recipe_id, version),
            ).fetchone()
            return dict(row) if row else None
        finally:
            if not self._is_memory:
                conn.close()

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
        """Log a completed production batch run referencing an immutable recipe revision."""
        clean_batch_number = str(batch_number).strip()
        if not clean_batch_number:
            raise ValueError("Batch number cannot be empty")

        if created_at is None:
            created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        items_list = []
        if items is not None:
            for it in items:
                if hasattr(it, "ingredient_name"):
                    raw_w = str(getattr(it, "raw_weight", it.scaled_weight))
                    items_list.append(
                        {
                            "ingredient_name": it.ingredient_name,
                            "percentage": str(it.percentage),
                            "cost_per_unit": str(getattr(it, "cost_per_unit", "0.00")),
                            "raw_weight": raw_w,
                            "scaled_weight": str(it.scaled_weight),
                            "cumulative_tare": str(it.cumulative_tare),
                            "cost": str(it.cost),
                        }
                    )
                elif isinstance(it, dict):
                    items_list.append(it)
        items_json = json.dumps(items_list)

        batch_id = str(uuid.uuid4())
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO batches (
                        id, recipe_id, recipe_version, batch_number, operator,
                        precision, target_weight, total_weight, total_cost,
                        items_json, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        batch_id,
                        str(recipe_id).strip(),
                        recipe_version,
                        clean_batch_number,
                        str(operator).strip(),
                        str(precision),
                        str(target_weight),
                        str(total_weight),
                        str(total_cost),
                        items_json,
                        notes,
                        created_at,
                    ),
                )
            return batch_id
        except sqlite3.IntegrityError as err:
            err_msg = str(err)
            if "UNIQUE constraint failed" in err_msg or "batch_number" in err_msg:
                raise DuplicateBatchNumberError(
                    f"Batch identifier '{clean_batch_number}' has already been recorded in audit logs."
                ) from err
            if "FOREIGN KEY constraint failed" in err_msg:
                raise RecipeNotFoundError(
                    f"Cannot record production batch: Recipe revision "
                    f"(id='{recipe_id}', version={recipe_version}) does not exist."
                ) from err
            raise StorageError(f"Database constraint violation: {err}") from err
        finally:
            if not self._is_memory:
                conn.close()

    def batch_exists(self, batch_number: str) -> bool:
        """Check whether a batch identifier has already been recorded."""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM batches WHERE batch_number = ? LIMIT 1;",
                (batch_number.strip(),),
            ).fetchone()
            return row is not None
        finally:
            if not self._is_memory:
                conn.close()

    def list_batches(self, recipe_id: str | None = None) -> list[dict[str, Any]]:
        """List historical production batches."""
        conn = self._get_connection()
        try:
            if recipe_id:
                rows = conn.execute(
                    "SELECT * FROM batches WHERE recipe_id = ? ORDER BY created_at DESC;",
                    (recipe_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM batches ORDER BY created_at DESC;").fetchall()
            return [dict(row) for row in rows]
        finally:
            if not self._is_memory:
                conn.close()

    def get_corrupted_recipes(self) -> list[dict[str, Any]]:
        """Identify active recipes that have no ingredients or invalid formulation data."""
        conn = self._get_connection()
        try:
            recipe_rows = conn.execute(
                "SELECT id, name, description, version, updated_at FROM recipes "
                "WHERE is_archived = 0 ORDER BY name ASC;"
            ).fetchall()

            if not recipe_rows:
                return []

            ingredient_rows = conn.execute(
                """
                SELECT recipe_id, name, percentage, cost_per_unit
                FROM ingredients
                WHERE recipe_id IN (SELECT id FROM recipes WHERE is_archived = 0)
                ORDER BY recipe_id, sort_order ASC;
                """
            ).fetchall()

            raw_ingredients_by_recipe: dict[str, list[dict[str, Any]]] = {}
            for row in ingredient_rows:
                r_id = row["recipe_id"]
                if r_id not in raw_ingredients_by_recipe:
                    raw_ingredients_by_recipe[r_id] = []
                raw_ingredients_by_recipe[r_id].append(row)

            corrupted: list[dict[str, Any]] = []
            for r_row in recipe_rows:
                r_id = r_row["id"]
                raw_ings = raw_ingredients_by_recipe.get(r_id, [])
                if not raw_ings:
                    corrupted.append({
                        "id": r_id,
                        "name": r_row["name"],
                        "error": "Recipe contains no ingredients",
                    })
                    continue
                try:
                    ings = tuple(
                        Ingredient(
                            name=row["name"],
                            percentage=Decimal(row["percentage"]),
                            cost_per_unit=Decimal(row["cost_per_unit"]),
                        )
                        for row in raw_ings
                    )
                    Recipe(
                        id=r_id,
                        name=r_row["name"],
                        description=r_row["description"],
                        version=int(r_row["version"]),
                        updated_at=r_row["updated_at"],
                        ingredients=ings,
                    )
                except Exception as err:
                    corrupted.append({
                        "id": r_id,
                        "name": r_row["name"],
                        "error": str(err),
                    })

            return corrupted
        finally:
            if not self._is_memory:
                conn.close()

    def list_recipes(self) -> list[Recipe]:
        conn = self._get_connection()
        try:
            recipe_rows = conn.execute(
                "SELECT id, name, description, version, updated_at, is_draft FROM recipes "
                "WHERE is_archived = 0 ORDER BY name ASC;"
            ).fetchall()

            if not recipe_rows:
                return []

            ingredient_rows = conn.execute(
                """
                SELECT recipe_id, name, percentage, cost_per_unit
                FROM ingredients
                WHERE recipe_id IN (SELECT id FROM recipes WHERE is_archived = 0)
                ORDER BY recipe_id, sort_order ASC;
                """
            ).fetchall()

            raw_ingredients_by_recipe: dict[str, list[dict[str, Any]]] = {}
            for row in ingredient_rows:
                r_id = row["recipe_id"]
                if r_id not in raw_ingredients_by_recipe:
                    raw_ingredients_by_recipe[r_id] = []
                raw_ingredients_by_recipe[r_id].append(row)

            recipes: list[Recipe] = []
            for r_row in recipe_rows:
                r_id = r_row["id"]
                raw_ings = raw_ingredients_by_recipe.get(r_id, [])
                if not raw_ings:
                    continue
                try:
                    ings = tuple(
                        Ingredient(
                            name=row["name"],
                            percentage=Decimal(row["percentage"]),
                            cost_per_unit=Decimal(row["cost_per_unit"]),
                        )
                        for row in raw_ings
                    )
                    recipes.append(
                        Recipe(
                            id=r_id,
                            name=r_row["name"],
                            description=r_row["description"],
                            version=int(r_row["version"]),
                            updated_at=r_row["updated_at"],
                            is_draft=bool(r_row["is_draft"]),
                            ingredients=ings,
                        )
                    )
                except Exception as err:
                    logger.warning("Skipping corrupted recipe '%s' (id=%s): %s", r_row["name"], r_id, err)

            return recipes
        finally:
            if not self._is_memory:
                conn.close()

    def delete_recipe(self, recipe_id: str) -> bool:
        """Soft-archive recipe to preserve historical revision snapshots and production batch records."""
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE recipes SET is_archived = 1 WHERE id = ? AND is_archived = 0;",
                    (recipe_id,),
                )
                return cursor.rowcount > 0
        finally:
            if not self._is_memory:
                conn.close()

    def list_archived_recipes(self) -> list[dict[str, Any]]:
        """Retrieve archived recipe summaries ordered by most recently updated."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    r.id,
                    r.name,
                    r.description,
                    r.version,
                    r.updated_at,
                    r.is_draft,
                    (SELECT COUNT(*) FROM ingredients i WHERE i.recipe_id = r.id) AS ingredient_count,
                    (SELECT COUNT(*) FROM batches b WHERE b.recipe_id = r.id) AS batch_count,
                    (SELECT COUNT(*) FROM recipe_revisions rv WHERE rv.recipe_id = r.id) AS revision_count
                FROM recipes r
                WHERE r.is_archived = 1
                ORDER BY r.updated_at DESC, r.name ASC;
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            if not self._is_memory:
                conn.close()

    def restore_recipe(self, recipe_id: str) -> bool:
        """Restore an archived recipe to the active library without altering its version history."""
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE recipes SET is_archived = 0 WHERE id = ? AND is_archived = 1;",
                    (recipe_id,),
                )
                return cursor.rowcount > 0
        finally:
            if not self._is_memory:
                conn.close()

    def delete_draft_recipe(self, recipe_id: str) -> bool:
        """Permanently delete an unreleased draft recipe with zero revisions and zero batches."""
        conn = self._get_connection()
        try:
            with conn:
                row = conn.execute(
                    "SELECT is_draft FROM recipes WHERE id = ?;",
                    (recipe_id,),
                ).fetchone()
                if row is None:
                    return False
                if not bool(row["is_draft"]):
                    raise StorageError("Only draft formulas can be permanently deleted.")

                rev_count = conn.execute(
                    "SELECT COUNT(*) FROM recipe_revisions WHERE recipe_id = ?;",
                    (recipe_id,),
                ).fetchone()[0]
                if rev_count > 0:
                    raise StorageError(
                        "Cannot permanently delete a draft that has prior committed revisions. "
                        "Use Discard Draft to revert instead."
                    )

                batch_count = conn.execute(
                    "SELECT COUNT(*) FROM batches WHERE recipe_id = ?;",
                    (recipe_id,),
                ).fetchone()[0]
                if batch_count > 0:
                    raise StorageError("Cannot permanently delete a draft referenced by production batches.")

                cursor = conn.execute(
                    "DELETE FROM recipes WHERE id = ? AND is_draft = 1;",
                    (recipe_id,),
                )
                return cursor.rowcount > 0
        finally:
            if not self._is_memory:
                conn.close()

    def get_catalog_ingredient(self, name: str) -> CatalogIngredient | None:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT id, name, cost_per_unit, supplier, sku, notes, updated_at "
                "FROM ingredients_catalog WHERE name = ? COLLATE NOCASE LIMIT 1;",
                (name.strip(),),
            ).fetchone()
            if row is None:
                return None
            return CatalogIngredient(
                id=row["id"],
                name=row["name"],
                cost_per_unit=Decimal(row["cost_per_unit"]),
                supplier=row["supplier"],
                sku=row["sku"],
                notes=row["notes"],
                updated_at=row["updated_at"],
            )
        finally:
            if not self._is_memory:
                conn.close()

    def get_catalog_ingredients_map(self) -> dict[str, CatalogIngredient]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT id, name, cost_per_unit, supplier, sku, notes, updated_at FROM ingredients_catalog;"
            ).fetchall()
            return {
                row["name"].strip().lower(): CatalogIngredient(
                    id=row["id"],
                    name=row["name"],
                    cost_per_unit=Decimal(row["cost_per_unit"]),
                    supplier=row["supplier"],
                    sku=row["sku"],
                    notes=row["notes"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            }
        finally:
            if not self._is_memory:
                conn.close()

    def list_catalog_ingredients(self) -> list[CatalogIngredient]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT id, name, cost_per_unit, supplier, sku, notes, updated_at "
                "FROM ingredients_catalog ORDER BY name COLLATE NOCASE ASC;"
            ).fetchall()
            return [
                CatalogIngredient(
                    id=row["id"],
                    name=row["name"],
                    cost_per_unit=Decimal(row["cost_per_unit"]),
                    supplier=row["supplier"],
                    sku=row["sku"],
                    notes=row["notes"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]
        finally:
            if not self._is_memory:
                conn.close()

    def save_catalog_ingredient(self, item: CatalogIngredient) -> None:
        self.save_catalog_ingredients([item])

    def save_catalog_ingredients(self, items: list[CatalogIngredient]) -> None:
        if not items:
            return
        for item in items:
            if not item.name.strip():
                raise ValueError("Catalog ingredient name cannot be empty")
        conn = self._get_connection()
        try:
            with conn:
                existing_rows = conn.execute(
                    "SELECT id, name FROM ingredients_catalog;"
                ).fetchall()
                existing_by_name = {row["name"].strip().lower(): row["id"] for row in existing_rows}
                existing_ids = {row["id"] for row in existing_rows}

                records = []
                for item in items:
                    clean_name = item.name.strip()
                    lower_name = clean_name.lower()
                    if lower_name in existing_by_name:
                        resolved_id = existing_by_name[lower_name]
                    else:
                        if item.id in existing_ids:
                            resolved_id = str(uuid.uuid4())
                        else:
                            resolved_id = item.id
                        existing_ids.add(resolved_id)
                        existing_by_name[lower_name] = resolved_id

                    records.append(
                        (
                            resolved_id,
                            clean_name,
                            str(item.cost_per_unit),
                            item.supplier.strip(),
                            item.sku.strip(),
                            item.notes.strip(),
                            item.updated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        )
                    )

                conn.executemany(
                    """
                    INSERT INTO ingredients_catalog (id, name, cost_per_unit, supplier, sku, notes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        cost_per_unit = excluded.cost_per_unit,
                        supplier = CASE WHEN excluded.supplier != ''
                            THEN excluded.supplier ELSE ingredients_catalog.supplier END,
                        sku = CASE WHEN excluded.sku != '' THEN excluded.sku ELSE ingredients_catalog.sku END,
                        notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE ingredients_catalog.notes END,
                        updated_at = excluded.updated_at;
                    """,
                    records,
                )
        finally:
            if not self._is_memory:
                conn.close()

    def update_catalog_ingredient(self, item: CatalogIngredient) -> bool:
        """Update an existing catalog item by ID, allowing safe rename without primary key conflict."""
        clean_name = item.name.strip()
        if not clean_name:
            raise ValueError("Catalog ingredient name cannot be empty")
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE ingredients_catalog
                    SET name = ?, cost_per_unit = ?, supplier = ?, sku = ?, notes = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (
                        clean_name,
                        str(item.cost_per_unit),
                        item.supplier.strip(),
                        item.sku.strip(),
                        item.notes.strip(),
                        item.updated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        item.id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise StorageError(f"Catalog ingredient with ID '{item.id}' does not exist.")
                return True
        except sqlite3.IntegrityError as err:
            if "UNIQUE constraint failed" in str(err) and "name" in str(err):
                raise StorageError(f"A raw material named '{clean_name}' already exists in the catalog.") from err
            raise StorageError(f"Database error updating catalog ingredient: {err}") from err
        finally:
            if not self._is_memory:
                conn.close()

    def upsert_catalog_price(
        self,
        name: str,
        cost_per_unit: Decimal,
        timestamp: str | None = None,
    ) -> str:
        """Update unit cost for an ingredient name while strictly preserving existing metadata."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Catalog ingredient name cannot be empty")
        if not isinstance(cost_per_unit, Decimal):
            cost_per_unit = Decimal(str(cost_per_unit))
        if cost_per_unit < Decimal("0"):
            raise ValueError("Cost per unit cannot be negative")

        ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO ingredients_catalog (id, name, cost_per_unit, supplier, sku, notes, updated_at)
                    VALUES (?, ?, ?, '', '', '', ?)
                    ON CONFLICT(name) DO UPDATE SET
                        cost_per_unit = excluded.cost_per_unit,
                        updated_at = excluded.updated_at;
                    """,
                    (str(uuid.uuid4()), clean_name, str(cost_per_unit), ts),
                )
                row = conn.execute(
                    "SELECT id FROM ingredients_catalog WHERE name = ? COLLATE NOCASE;",
                    (clean_name,),
                ).fetchone()
                return str(row["id"]) if row else ""
        finally:
            if not self._is_memory:
                conn.close()

    def delete_catalog_ingredient(self, item_id: str) -> bool:
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM ingredients_catalog WHERE id = ?;",
                    (item_id,),
                )
                return cursor.rowcount > 0
        finally:
            if not self._is_memory:
                conn.close()

    def list_all_recipes_data(self) -> list[dict[str, Any]]:
        """Retrieve all recipes (including archived) with ingredients for workspace export."""
        conn = self._get_connection()
        try:
            recipe_rows = conn.execute(
                "SELECT id, name, description, version, updated_at, is_archived, is_draft "
                "FROM recipes ORDER BY name ASC;"
            ).fetchall()
            if not recipe_rows:
                return []

            ingredient_rows = conn.execute(
                "SELECT recipe_id, sort_order, name, percentage, cost_per_unit "
                "FROM ingredients ORDER BY recipe_id, sort_order ASC;"
            ).fetchall()

            ingredients_by_recipe: dict[str, list[dict[str, str]]] = {}
            for row in ingredient_rows:
                r_id = row["recipe_id"]
                if r_id not in ingredients_by_recipe:
                    ingredients_by_recipe[r_id] = []
                ingredients_by_recipe[r_id].append(
                    {
                        "name": row["name"],
                        "percentage": str(row["percentage"]),
                        "cost_per_unit": str(row["cost_per_unit"]),
                    }
                )

            result = []
            for r_row in recipe_rows:
                r_id = r_row["id"]
                result.append(
                    {
                        "id": r_id,
                        "name": r_row["name"],
                        "description": r_row["description"],
                        "version": int(r_row["version"]),
                        "updated_at": r_row["updated_at"],
                        "is_archived": int(r_row["is_archived"]),
                        "is_draft": int(r_row["is_draft"]),
                        "ingredients": ingredients_by_recipe.get(r_id, []),
                    }
                )
            return result
        finally:
            if not self._is_memory:
                conn.close()

    def export_workspace_snapshot(self) -> dict[str, Any]:
        """Atomically read all workspace entities within a single read connection and transaction."""
        conn = self._get_connection()
        try:
            with conn:
                cat_rows = conn.execute(
                    "SELECT id, name, cost_per_unit, supplier, sku, notes, updated_at "
                    "FROM ingredients_catalog ORDER BY name COLLATE NOCASE ASC;"
                ).fetchall()
                catalog = [
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "cost_per_unit": str(Decimal(row["cost_per_unit"])),
                        "supplier": row["supplier"],
                        "sku": row["sku"],
                        "notes": row["notes"],
                        "updated_at": row["updated_at"],
                    }
                    for row in cat_rows
                ]

                recipe_rows = conn.execute(
                    "SELECT id, name, description, version, updated_at, is_archived, is_draft "
                    "FROM recipes ORDER BY name ASC;"
                ).fetchall()

                ingredient_rows = conn.execute(
                    "SELECT recipe_id, sort_order, name, percentage, cost_per_unit "
                    "FROM ingredients ORDER BY recipe_id, sort_order ASC;"
                ).fetchall()

                ingredients_by_recipe: dict[str, list[dict[str, str]]] = {}
                for row in ingredient_rows:
                    r_id = row["recipe_id"]
                    if r_id not in ingredients_by_recipe:
                        ingredients_by_recipe[r_id] = []
                    ingredients_by_recipe[r_id].append(
                        {
                            "name": row["name"],
                            "percentage": str(row["percentage"]),
                            "cost_per_unit": str(row["cost_per_unit"]),
                        }
                    )

                recipes = []
                for r_row in recipe_rows:
                    r_id = r_row["id"]
                    recipes.append(
                        {
                            "id": r_id,
                            "name": r_row["name"],
                            "description": r_row["description"],
                            "version": int(r_row["version"]),
                            "updated_at": r_row["updated_at"],
                            "is_archived": int(r_row["is_archived"]),
                            "is_draft": int(r_row["is_draft"]),
                            "ingredients": ingredients_by_recipe.get(r_id, []),
                        }
                    )

                rev_rows = conn.execute(
                    "SELECT id, recipe_id, version, name, description, ingredients_json, created_at "
                    "FROM recipe_revisions ORDER BY recipe_id, version ASC;"
                ).fetchall()
                revisions = [dict(row) for row in rev_rows]

                batch_rows = conn.execute(
                    "SELECT id, recipe_id, recipe_version, batch_number, operator, precision, "
                    "target_weight, total_weight, total_cost, items_json, notes, created_at "
                    "FROM batches ORDER BY created_at DESC;"
                ).fetchall()
                batches = [dict(row) for row in batch_rows]

                return {
                    "catalog": catalog,
                    "recipes": recipes,
                    "revisions": revisions,
                    "batches": batches,
                }
        finally:
            if not self._is_memory:
                conn.close()

    def list_all_revisions(self) -> list[dict[str, Any]]:
        """Retrieve all historical immutable revision snapshots across all recipes."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT id, recipe_id, version, name, description, ingredients_json, created_at "
                "FROM recipe_revisions ORDER BY recipe_id, version ASC;"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            if not self._is_memory:
                conn.close()

    def restore_workspace_data(
        self,
        workspace_data: dict[str, Any],
        pre_restore_backup_dir: Path | None = None,
    ) -> Path | None:
        """Atomically restore all workspace tables from verified dictionary data.

        For disk-backed databases, creates a pre-restore safety snapshot (.bak),
        populates an isolated staged database, and atomically replaces active database file.
        """
        recipes_data = workspace_data.get("recipes", [])
        catalog_data = workspace_data.get("catalog", [])
        revisions_data = workspace_data.get("revisions", [])
        batches_data = workspace_data.get("batches", [])

        # If revisions were not captured, backfill baseline revisions from recipes
        if not revisions_data and recipes_data:
            revisions_data = [
                {
                    "id": str(uuid.uuid4()),
                    "recipe_id": r["id"],
                    "version": r["version"],
                    "name": r["name"],
                    "description": r.get("description", ""),
                    "ingredients_json": json.dumps(r.get("ingredients", [])),
                    "created_at": r.get("updated_at", "") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                for r in recipes_data
                if not r.get("is_draft", 0)
            ]

        def _populate_connection(target_conn: sqlite3.Connection) -> None:
            target_conn.execute("PRAGMA foreign_keys = OFF;")
            with target_conn:
                # Clear existing rows
                target_conn.execute("DELETE FROM batches;")
                target_conn.execute("DELETE FROM recipe_revisions;")
                target_conn.execute("DELETE FROM ingredients;")
                target_conn.execute("DELETE FROM recipes;")
                target_conn.execute("DELETE FROM ingredients_catalog;")

                # 1. Recipes & Ingredients
                for r in recipes_data:
                    target_conn.execute(
                        "INSERT INTO recipes (id, name, description, version, updated_at, is_archived, is_draft) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?);",
                        (
                            r["id"],
                            r["name"],
                            r.get("description", ""),
                            r.get("version", 1),
                            r.get("updated_at", ""),
                            r.get("is_archived", 0),
                            1 if r.get("is_draft", 0) else 0,
                        ),
                    )
                    ings = r.get("ingredients", [])
                    for idx, ing in enumerate(ings):
                        target_conn.execute(
                            "INSERT INTO ingredients (recipe_id, sort_order, name, percentage, cost_per_unit) "
                            "VALUES (?, ?, ?, ?, ?);",
                            (
                                r["id"],
                                idx,
                                ing["name"],
                                str(ing["percentage"]),
                                str(ing.get("cost_per_unit", "0.00")),
                            ),
                        )

                # 2. Revisions
                for rev in revisions_data:
                    target_conn.execute(
                        "INSERT INTO recipe_revisions "
                        "(id, recipe_id, version, name, description, ingredients_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?);",
                        (
                            rev.get("id", str(uuid.uuid4())),
                            rev["recipe_id"],
                            rev["version"],
                            rev["name"],
                            rev.get("description", ""),
                            rev["ingredients_json"],
                            rev.get("created_at", ""),
                        ),
                    )

                # 3. Batches
                for b in batches_data:
                    target_conn.execute(
                        "INSERT INTO batches (id, recipe_id, recipe_version, batch_number, operator, precision, "
                        "target_weight, total_weight, total_cost, items_json, notes, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                        (
                            b.get("id", str(uuid.uuid4())),
                            b["recipe_id"],
                            b["recipe_version"],
                            b["batch_number"],
                            b["operator"],
                            b.get("precision", "0.01"),
                            str(b["target_weight"]),
                            str(b["total_weight"]),
                            str(b["total_cost"]),
                            b.get("items_json", "[]"),
                            b.get("notes", ""),
                            b.get("created_at", ""),
                        ),
                    )

                # 4. Master Catalog
                for c in catalog_data:
                    target_conn.execute(
                        "INSERT INTO ingredients_catalog (id, name, cost_per_unit, supplier, sku, notes, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?);",
                        (
                            c.get("id", str(uuid.uuid4())),
                            c["name"],
                            str(c.get("cost_per_unit", "0.00")),
                            c.get("supplier", ""),
                            c.get("sku", ""),
                            c.get("notes", ""),
                            c.get("updated_at", ""),
                        ),
                    )

            target_conn.execute("PRAGMA foreign_keys = ON;")

        def _verify_fk(target_conn: sqlite3.Connection) -> None:
            fk_violations = target_conn.execute("PRAGMA foreign_key_check;").fetchall()
            if fk_violations:
                details = [f"table={row[0]} rowid={row[1]} parent={row[2]} fkid={row[3]}" for row in fk_violations]
                raise StorageError(f"Foreign key integrity check failed after restore: {'; '.join(details)}")

        if self._is_memory:
            conn = self._get_connection()
            conn.execute("PRAGMA foreign_keys = OFF;")
            conn.executescript("""
                DROP TABLE IF EXISTS batches;
                DROP TABLE IF EXISTS recipe_revisions;
                DROP TABLE IF EXISTS ingredients;
                DROP TABLE IF EXISTS recipes;
                DROP TABLE IF EXISTS ingredients_catalog;
            """)
            self._init_db()
            _populate_connection(conn)
            conn.execute("PRAGMA foreign_keys = ON;")
            _verify_fk(conn)
            return None

        # On-disk atomic replacement with safety backup
        active_db = Path(self.db_path).resolve()
        safety_backup_path: Path | None = None

        if active_db.is_file():
            conn = self._get_connection()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            finally:
                conn.close()

            dest_dir = pre_restore_backup_dir or active_db.parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safety_backup_path = dest_dir / f"recipes_pre_restore_{ts}.bak"
            shutil.copy2(str(active_db), str(safety_backup_path))

            # Retain only the latest 5 safety backups to prevent unbounded accumulation
            try:
                existing_baks = sorted(
                    dest_dir.glob("recipes_pre_restore_*.bak"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for old_bak in existing_baks[5:]:
                    try:
                        old_bak.unlink()
                    except OSError:
                        pass
            except Exception:
                pass

        staged_db_path = active_db.parent / f".restore_stage_{uuid.uuid4().hex[:8]}.db"
        try:
            staged_repo = SQLiteRecipeRepository(staged_db_path)
            staged_conn = staged_repo._get_connection()
            try:
                _populate_connection(staged_conn)
                staged_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                _verify_fk(staged_conn)
            finally:
                if not staged_repo._is_memory:
                    staged_conn.close()
                staged_repo.close()

            self.close()

            # Atomically replace database file
            os.replace(str(staged_db_path), str(active_db))

            # Remove obsolete WAL/SHM artifacts
            for suffix in ("-wal", "-shm"):
                aux = active_db.with_name(active_db.name + suffix)
                if aux.exists():
                    try:
                        aux.unlink()
                    except OSError:
                        pass

            self._init_db()
            return safety_backup_path
        finally:
            if staged_db_path.exists():
                try:
                    staged_db_path.unlink()
                except OSError:
                    pass

    def reset_workspace_data(
        self,
        pre_reset_backup_dir: Path | None = None,
    ) -> Path | None:
        """Atomically reset all workspace tables to clean default state with pre-reset backup."""
        active_db = Path(self.db_path).resolve()
        safety_backup_path: Path | None = None

        if not self._is_memory and active_db.is_file():
            conn = self._get_connection()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            finally:
                conn.close()

            dest_dir = pre_reset_backup_dir or active_db.parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safety_backup_path = dest_dir / f"recipes_pre_reset_{ts}.bak"
            shutil.copy2(str(active_db), str(safety_backup_path))
            try:
                from tared.storage.workspace_io import export_workspace_to_json_file
                json_backup_path = dest_dir / f"recipes_pre_reset_{ts}.json"
                export_workspace_to_json_file(self, json_backup_path)
            except Exception as json_err:
                logger.warning("Could not create pre-reset JSON snapshot: %s", json_err)

            try:
                existing_baks = sorted(
                    dest_dir.glob("recipes_pre_reset_*.bak"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for old_bak in existing_baks[5:]:
                    try:
                        old_bak.unlink()
                    except OSError:
                        pass
            except Exception:
                pass

        conn = self._get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = OFF;")
            with conn:
                conn.execute("DROP TABLE IF EXISTS batches;")
                conn.execute("DROP TABLE IF EXISTS recipe_revisions;")
                conn.execute("DROP TABLE IF EXISTS ingredients;")
                conn.execute("DROP TABLE IF EXISTS recipes;")
                conn.execute("DROP TABLE IF EXISTS ingredients_catalog;")
        finally:
            if not self._is_memory:
                conn.close()

        self._init_db()
        return safety_backup_path

    def close(self) -> None:
        if self._mem_conn is not None:
            try:
                self._mem_conn.close()
            except Exception:
                pass
            self._mem_conn = None

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "SQLiteRecipeRepository":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
