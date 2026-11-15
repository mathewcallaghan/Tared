"""Unit tests for batch sheet HTML generation, escaping, operator metadata, precision formatting, and PDF rendering."""

import html
import unittest
from decimal import Decimal
from pathlib import Path

from tared.core.calculator import compute_batch
from tared.core.models import Ingredient, Recipe
from tared.printing.renderer import (
    export_batch_sheet_to_pdf,
    render_batch_sheet_html,
)
from tests.conftest import create_test_recipe


class TestBatchSheetRenderer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from PyQt6.QtWidgets import QApplication

            if QApplication.instance() is None:
                cls._app = QApplication(["test-printing-runner"])
        except ImportError:
            pass

    def setUp(self) -> None:
        self.recipe = create_test_recipe()
        self.batch = compute_batch(self.recipe, target_weight=Decimal("1500.00"), precision=Decimal("0.01"))

    def test_html_rendering_contents_and_operator_metadata(self) -> None:
        html_out = render_batch_sheet_html(
            self.batch,
            batch_number="BATCH-TEST-001",
            operator="Jane Doe (Lead Chemist)",
            notes="Handle acids with PPE.",
            timestamp_str="2026-11-06 12:00 UTC",
        )

        self.assertIn(self.recipe.name, html_out)
        self.assertIn("Rev v1", html_out)
        self.assertIn("BATCH-TEST-001", html_out)
        self.assertIn("Jane Doe (Lead Chemist)", html_out)
        self.assertIn("2026-11-06 12:00 UTC", html_out)
        self.assertIn("1500.00 g", html_out)
        self.assertIn("Handle acids with PPE.", html_out)

        for item in self.batch.items:
            self.assertIn(html.escape(item.ingredient_name), html_out)
            self.assertIn(f"{item.scaled_weight:.2f}", html_out)
            self.assertIn(f"{item.cumulative_tare:.2f}", html_out)

    def test_recipe_description_rendered_in_batch_sheet(self) -> None:
        recipe = Recipe(
            name="Compounding Formula with SOP",
            description="Dissolve Phase A at 75C before slow emulsification with Phase B.",
            ingredients=self.recipe.ingredients,
        )
        batch = compute_batch(recipe, target_weight=Decimal("1000.00"))
        html_out = render_batch_sheet_html(batch, notes="Ambient batch temperature was 21C.")

        self.assertIn("Dissolve Phase A at 75C before slow emulsification with Phase B.", html_out)
        self.assertIn("Ambient batch temperature was 21C.", html_out)
        self.assertIn("Master Formula Instructions:", html_out)
        self.assertIn("Batch Execution Notes:", html_out)

    def test_high_precision_quantum_and_exact_percentages_rendered(self) -> None:
        recipe = Recipe(
            name="Micro Formulation",
            ingredients=(
                Ingredient(name="Active Substance", percentage=Decimal("0.0001")),
                Ingredient(name="Carrier Excipient", percentage=Decimal("99.9999")),
            ),
        )
        batch = compute_batch(recipe, target_weight=Decimal("10.000"), precision=Decimal("0.001"))
        self.assertEqual(batch.recipe_id, recipe.id)

        html_out = render_batch_sheet_html(batch, batch_number="B-PREC-01")

        self.assertIn("10.000 g", html_out)
        self.assertIn("0.0001%", html_out)
        self.assertIn("99.9999%", html_out)

    def test_micro_costs_avoid_false_zero_display_and_match_total(self) -> None:
        ings = tuple(
            Ingredient(name=f"Ing_{i}", percentage=Decimal("5.00"), cost_per_unit=Decimal("0.40")) for i in range(20)
        )
        recipe = Recipe(name="Micro Cost Formula", ingredients=ings)
        batch = compute_batch(recipe, target_weight=Decimal("100.00"), precision=Decimal("0.01"))

        html_out = render_batch_sheet_html(batch, batch_number="B-MICRO-01")

        self.assertIn("$0.0020", html_out)
        self.assertIn("$0.0400", html_out)
        self.assertNotIn(">$0.00<", html_out)

    def test_html_special_character_and_unit_escaping(self) -> None:
        tricky_recipe = Recipe(
            name="Special <Formula> & Acid",
            ingredients=(
                Ingredient(name="Component & Ground <Phase 1>", percentage=Decimal("50.00")),
                Ingredient(name="Solvent & Carrier <Phase 2>", percentage=Decimal("50.00")),
            ),
        )
        batch = compute_batch(tricky_recipe, target_weight=Decimal("200.00"))
        html_out = render_batch_sheet_html(
            batch,
            operator="Operator <Dr. Q&A>",
            notes="Warning: <script>alert(1)</script>",
            unit="<kg>",
            currency_symbol="<&>",
        )

        self.assertNotIn("<script>", html_out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_out)
        self.assertIn("Special &lt;Formula&gt; &amp; Acid", html_out)
        self.assertIn("&lt;kg&gt;", html_out)
        self.assertIn("&lt;&amp;&gt;", html_out)

    def test_single_pass_substitution_blocks_secondary_token_injection(self) -> None:
        html_out = render_batch_sheet_html(
            self.batch,
            notes="Malicious injection attempt: {{ TABLE_ROWS }} {{ RECIPE_NAME }}",
        )
        self.assertIn("Malicious injection attempt: {{ TABLE_ROWS }} {{ RECIPE_NAME }}", html_out)

    def test_pdf_generation_headless_and_document_fidelity(self) -> None:
        import tempfile

        try:
            import PyQt6  # noqa: F401
            from PyQt6.QtGui import QTextDocument
        except ImportError:
            self.skipTest("PyQt6 not installed in current environment; skipping PDF export test.")

        html_content = render_batch_sheet_html(
            self.batch,
            batch_number="B-VERIFY-99",
            operator="Dr. Alex Morgan",
            notes="Verified scale calibration standard.",
            timestamp_str="2026-11-06 14:30 AEDT",
        )
        doc = QTextDocument()
        doc.setHtml(html_content)
        plain_text = doc.toPlainText()

        self.assertIn(self.batch.recipe_name, plain_text)
        self.assertIn("B-VERIFY-99", plain_text)
        self.assertIn("Dr. Alex Morgan", plain_text)
        self.assertIn("1500.00 g", plain_text)
        self.assertIn("Verified scale calibration standard.", plain_text)

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "test_production_batch.pdf"
            result_path = export_batch_sheet_to_pdf(
                self.batch,
                pdf_path,
                batch_number="B-VERIFY-99",
                operator="Dr. Alex Morgan",
                notes="Verified scale calibration standard.",
                timestamp_str="2026-11-06 14:30 AEDT",
            )
            self.assertTrue(result_path.exists())
            pdf_bytes = result_path.read_bytes()
            self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
            self.assertGreater(len(pdf_bytes), 3000)

    def test_print_dialog_captures_real_time_export_timestamp(self) -> None:
        from unittest.mock import MagicMock, patch

        from tared.ui.views.print_dialog import PrintBatchSheetDialog

        mock_repo = MagicMock()
        mock_repo.batch_exists.return_value = False

        dialog = PrintBatchSheetDialog(self.batch, repository=mock_repo)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-TS-VERIFY-01")

        # Simulate delayed export
        frozen_time = "2026-11-06T15:00:00Z"
        with patch("tared.ui.views.print_dialog.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.strftime.return_value = frozen_time
            mock_now.astimezone.return_value.strftime.return_value = "2026-11-06 15:00 UTC"
            mock_dt.now.return_value = mock_now

            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=("/tmp/out.pdf", "PDF Files (*.pdf)")
            ):
                with patch("tared.ui.views.print_dialog.export_batch_sheet_to_pdf"):
                    with patch("os.replace"):
                        with patch("PyQt6.QtWidgets.QMessageBox.information"):
                            dialog._export_pdf()

        # The recorded timestamp must be taken from export time
        mock_repo.record_batch.assert_called_once()
        call_kwargs = mock_repo.record_batch.call_args[1]
        self.assertEqual(call_kwargs["created_at"], frozen_time)

    def test_deviation_row_rendered_when_allocation_shifts_mass(self) -> None:
        batch = compute_batch(self.recipe, target_weight=Decimal("333.33"), precision=Decimal("0.01"))
        self.assertGreater(batch.max_allocation_deviation, Decimal("0"))

        html_out = render_batch_sheet_html(batch, batch_number="B-DEV-01")
        self.assertIn("Discrete Allocation Shift (at most):", html_out)
        self.assertIn(f"{batch.max_allocation_deviation:.2f} g", html_out)

    def test_explicit_two_decimal_cost_footnote_and_line_consistency(self) -> None:
        ings = (
            Ingredient(name="Item 1", percentage=Decimal("33.34"), cost_per_unit=Decimal("0.10")),
            Ingredient(name="Item 2", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.10")),
            Ingredient(name="Item 3", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.10")),
        )
        recipe = Recipe(name="Subcent Rounding Footnote", ingredients=ings)
        batch = compute_batch(recipe, target_weight=Decimal("120.00"), precision=Decimal("0.01"))

        html_out = render_batch_sheet_html(batch, batch_number="B-FOOTNOTE-01", cost_decimals=2)
        self.assertIn(">$0.00<", html_out)
        self.assertNotIn(">$0.0040<", html_out)
        self.assertIn("total ($0.01) is rounded from exact unrounded sum ($0.0120).", html_out)

    def test_itemless_batch_reprint_preserves_fractional_cent_total_cost(self) -> None:
        from tared.core.models import BatchResult

        itemless_batch = BatchResult(
            recipe_name="Legacy Itemless Formula",
            target_weight=Decimal("100.00"),
            precision=Decimal("0.01"),
            items=(),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("0.1250"),
        )
        html_out = render_batch_sheet_html(itemless_batch, batch_number="B-ITEMLESS-01")
        self.assertIn("$0.1250", html_out)
        self.assertNotIn("Line items are rounded", html_out)

    def test_sub_four_decimal_micro_cost_footnote_does_not_misreport_line_rounding(self) -> None:
        ings = (
            Ingredient(name="Ultra Micro A", percentage=Decimal("33.34"), cost_per_unit=Decimal("0.001")),
            Ingredient(name="Ultra Micro B", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.001")),
            Ingredient(name="Ultra Micro C", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.001")),
        )
        recipe = Recipe(name="Sub-Four-Decimal Formula", ingredients=ings)
        batch = compute_batch(recipe, target_weight=Decimal("120.00"), precision=Decimal("0.01"))

        html_out = render_batch_sheet_html(batch, batch_number="B-ULTRA-01")
        self.assertIn("$0.00004", html_out)
        self.assertNotIn("Line items are rounded to 4 decimals", html_out)
        self.assertIn("Line items retain adaptive micro-cost precision", html_out)

    def test_export_pdf_raises_on_silent_qprinter_failure_and_cleans_invalid_file(self) -> None:
        import tempfile
        from unittest.mock import patch

        from PyQt6.QtGui import QTextDocument

        with tempfile.TemporaryDirectory() as tmpdir:
            missing_pdf = Path(tmpdir) / "unwritten.pdf"
            with patch.object(QTextDocument, "print", return_value=None):
                with self.assertRaises(OSError):
                    export_batch_sheet_to_pdf(self.batch, missing_pdf, batch_number="B-FAIL-01")
            self.assertFalse(missing_pdf.exists())

            corrupt_pdf = Path(tmpdir) / "corrupt.pdf"
            corrupt_pdf.write_bytes(b"NOT_A_PDF")
            with patch.object(QTextDocument, "print", return_value=None):
                with self.assertRaises(OSError):
                    export_batch_sheet_to_pdf(self.batch, corrupt_pdf, batch_number="B-FAIL-02")
            self.assertFalse(corrupt_pdf.exists())

    def test_print_dialog_does_not_commit_batch_when_pdf_write_fails(self) -> None:
        import tempfile
        from unittest.mock import patch

        from PyQt6.QtGui import QTextDocument

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.ui.views.print_dialog import PrintBatchSheetDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)

        dialog = PrintBatchSheetDialog(self.batch, repository=repo)
        self.addCleanup(dialog.close)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-PDF-FAIL-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_pdf = Path(tmpdir) / "failed_sheet.pdf"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(str(dest_pdf), "PDF Files (*.pdf)"),
            ):
                with patch.object(QTextDocument, "print", return_value=None):
                    with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                        dialog._export_pdf()
                        mock_crit.assert_called_once()

        self.assertFalse(repo.batch_exists("BAT-PDF-FAIL-01"))
        self.assertEqual(len(repo.list_batches()), 0)

    def test_batch_history_reprint_reports_error_when_pdf_write_fails(self) -> None:
        import tempfile
        from unittest.mock import patch

        from PyQt6.QtGui import QTextDocument

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.ui.views.batch_history_dialog import BatchHistoryDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-REPRINT-FAIL-01",
            operator="Operator Lead",
            target_weight=Decimal("1000.00"),
            total_weight=Decimal("1000.00"),
            total_cost=Decimal("10.00"),
        )

        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        self.addCleanup(dialog.close)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = Path(tmpdir) / "failed_reprint.pdf"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(str(out_pdf), "PDF Files (*.pdf)"),
            ):
                with patch.object(QTextDocument, "print", return_value=None):
                    with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
                        with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                            dialog._reprint_selected_batch()
                            mock_info.assert_not_called()
                            mock_crit.assert_called_once()

    def test_batch_history_reprint_preserves_existing_file_and_cleans_staging_on_failure(self) -> None:
        import tempfile
        from unittest.mock import patch

        from PyQt6.QtGui import QTextDocument

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.ui.views.batch_history_dialog import BatchHistoryDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-REPRINT-KEEP-01",
            operator="Operator Lead",
            target_weight=Decimal("1000.00"),
            total_weight=Decimal("1000.00"),
            total_cost=Decimal("10.00"),
        )

        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        self.addCleanup(dialog.close)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = Path(tmpdir) / "existing_reprint.pdf"
            out_pdf.write_bytes(b"%PDF-1.4 existing content")

            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(str(out_pdf), "PDF Files (*.pdf)"),
            ):
                with patch.object(QTextDocument, "print", return_value=None):
                    with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
                        with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                            dialog._reprint_selected_batch()
                            mock_info.assert_not_called()
                            mock_crit.assert_called_once()

            self.assertTrue(out_pdf.is_file())
            self.assertEqual(out_pdf.read_bytes(), b"%PDF-1.4 existing content")
            self.assertEqual(list(Path(tmpdir).glob(".tmp_*")), [])

    def test_batch_history_reprint_raw_weight_isolated_from_ambient_decimal_context(self) -> None:
        import decimal
        import tempfile
        from unittest.mock import patch

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.ui.views.batch_history_dialog import BatchHistoryDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        thirds = Recipe(
            id="rec-thirds-reprint",
            name="Thirds Reprint",
            ingredients=(
                Ingredient("A", Decimal("33.3333"), Decimal("1.00")),
                Ingredient("B", Decimal("33.3333"), Decimal("1.00")),
                Ingredient("C", Decimal("33.3334"), Decimal("1.00")),
            ),
        )
        repo.save_recipe(thirds)
        repo.record_batch(
            recipe_id=thirds.id,
            recipe_version=1,
            batch_number="BAT-THIRDS-CTX-01",
            operator="Operator Lead",
            target_weight=Decimal("1234.56"),
            total_weight=Decimal("1234.56"),
            total_cost=Decimal("1.23"),
            items=[
                {
                    "ingredient_name": "A",
                    "percentage": "33.3333",
                    "scaled_weight": "411.52",
                    "cumulative_tare": "411.52",
                    "cost": "0.41",
                },
                {
                    "ingredient_name": "B",
                    "percentage": "33.3333",
                    "scaled_weight": "411.52",
                    "cumulative_tare": "823.04",
                    "cost": "0.41",
                },
                {
                    "ingredient_name": "C",
                    "percentage": "33.3334",
                    "scaled_weight": "411.52",
                    "cumulative_tare": "1234.56",
                    "cost": "0.41",
                },
            ],
        )

        dialog = BatchHistoryDialog(thirds.id, thirds.name, repo)
        self.addCleanup(dialog.close)
        captured_batch = None

        def mock_export(batch, output_path, **kwargs):
            nonlocal captured_batch
            captured_batch = batch
            Path(output_path).write_bytes(b"%PDF-thirds")

        orig_ctx = decimal.getcontext().copy()
        try:
            decimal.getcontext().prec = 4
            decimal.getcontext().rounding = decimal.ROUND_DOWN
            with tempfile.TemporaryDirectory() as tmpdir:
                out_pdf = str(Path(tmpdir) / "thirds_reprint.pdf")
                with patch(
                    "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                    return_value=(out_pdf, "PDF Files (*.pdf)"),
                ):
                    with patch(
                        "tared.ui.views.batch_history_dialog.export_batch_sheet_to_pdf",
                        side_effect=mock_export,
                    ):
                        with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
                            with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                                dialog._reprint_selected_batch()
                                mock_info.assert_called_once()
                                mock_crit.assert_not_called()
        finally:
            decimal.setcontext(orig_ctx)

        self.assertIsNotNone(captured_batch)
        assert captured_batch is not None
        # Expected exact raw_w for 33.3334% of 1234.56 is 411.52082304 -> deviation 0.00082304...
        # Under ambient prec=4 ROUND_DOWN, raw_w would have truncated to 411.5 and deviation to 0.02
        self.assertLess(captured_batch.max_allocation_deviation, Decimal("0.001"))

    def test_render_normalizes_precision_decimals_and_handles_none_metadata(self) -> None:
        from tared.core.models import BatchResult

        batch = compute_batch(self.recipe, target_weight=Decimal("100"), precision=Decimal("1.0"))
        html_out = render_batch_sheet_html(
            batch,
            batch_number=None,  # type: ignore[arg-type]
            operator=None,  # type: ignore[arg-type]
            notes=None,  # type: ignore[arg-type]
        )
        self.assertIn("100 g", html_out)
        self.assertNotIn("100.0 g", html_out)
        self.assertIn("Unassigned", html_out)
        self.assertIn("None provided.", html_out)

        none_desc_batch = BatchResult(
            recipe_name="Null Desc",
            target_weight=Decimal("100"),
            precision=Decimal("1.0"),
            items=batch.items,
            total_weight=Decimal("100"),
            total_cost=batch.total_cost,
            recipe_description=None,  # type: ignore[arg-type]
        )
        html_none_desc = render_batch_sheet_html(none_desc_batch, batch_number="B-NORM-01")
        self.assertIn("Standard compounding procedure.", html_none_desc)

    def test_batch_history_reprint_handles_none_operator_and_notes(self) -> None:
        import tempfile
        from unittest.mock import patch

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.ui.views.batch_history_dialog import BatchHistoryDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-NONE-REPRINT",
            operator="Op",
            target_weight=Decimal("100"),
            total_weight=Decimal("100"),
            total_cost=Decimal("1.00"),
        )
        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        self.addCleanup(dialog.close)
        dialog._batches[0]["operator"] = None
        dialog._batches[0]["notes"] = None

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = str(Path(tmpdir) / "none_reprint.pdf")
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(out_pdf, "PDF Files (*.pdf)"),
            ):
                with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
                    with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                        dialog._reprint_selected_batch()
                        mock_info.assert_called_once()
                        mock_crit.assert_not_called()
            self.assertTrue(Path(out_pdf).is_file())

    def test_batch_history_reprint_handles_null_fields_in_items_json(self) -> None:
        import json
        import tempfile
        from unittest.mock import patch

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.ui.views.batch_history_dialog import BatchHistoryDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-NULL-ITEM-REPRINT",
            operator="Op",
            target_weight=Decimal("100"),
            total_weight=Decimal("100"),
            total_cost=Decimal("1.00"),
            items=[
                {
                    "ingredient_name": None,
                    "percentage": None,
                    "scaled_weight": "100.00",
                    "cumulative_tare": None,
                    "cost": None,
                    "cost_per_unit": None,
                }
            ],
        )
        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        self.addCleanup(dialog.close)
        dialog._batches[0]["items_json"] = json.dumps(
            [
                {
                    "ingredient_name": None,
                    "percentage": None,
                    "scaled_weight": "100.00",
                    "cumulative_tare": None,
                    "cost": None,
                    "cost_per_unit": None,
                }
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = str(Path(tmpdir) / "null_item_reprint.pdf")
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(out_pdf, "PDF Files (*.pdf)"),
            ):
                with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
                    with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                        dialog._reprint_selected_batch()
                        mock_info.assert_called_once()
                        mock_crit.assert_not_called()
            self.assertTrue(Path(out_pdf).is_file())

    def test_export_and_reprint_append_pdf_extension_preserving_dots(self) -> None:
        import tempfile
        from unittest.mock import patch

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.ui.views.batch_history_dialog import BatchHistoryDialog
        from tared.ui.views.print_dialog import PrintBatchSheetDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)

        with tempfile.TemporaryDirectory() as tmpdir:
            base_no_ext = Path(tmpdir) / "batch_v1.2"
            print_dlg = PrintBatchSheetDialog(self.batch, repository=repo)
            self.addCleanup(print_dlg.close)
            print_dlg.operator_input.setText("Operator Lead")
            print_dlg.batch_num_input.setText("BAT-EXT-01")
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(str(base_no_ext), "PDF Files (*.pdf)"),
            ):
                with patch("PyQt6.QtWidgets.QMessageBox.information"):
                    print_dlg._export_pdf()
            expected_pdf = Path(tmpdir) / "batch_v1.2.pdf"
            self.assertTrue(expected_pdf.is_file())

            hist_dlg = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
            self.addCleanup(hist_dlg.close)
            reprint_no_ext = Path(tmpdir) / "reprint_v1.2"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(str(reprint_no_ext), "PDF Files (*.pdf)"),
            ):
                with patch("PyQt6.QtWidgets.QMessageBox.information"):
                    hist_dlg._reprint_selected_batch()
            self.assertTrue((Path(tmpdir) / "reprint_v1.2.pdf").is_file())

    def test_print_and_reprint_preserve_explicit_cost_decimals_across_workspace_roundtrip(self) -> None:
        import tempfile
        from unittest.mock import patch

        from tared.storage.sqlite_repo import SQLiteRecipeRepository
        from tared.storage.workspace_io import export_workspace_to_json_file, restore_workspace_from_json
        from tared.ui.views.batch_history_dialog import BatchHistoryDialog
        from tared.ui.views.print_dialog import PrintBatchSheetDialog

        ings = (
            Ingredient(name="Item 1", percentage=Decimal("33.34"), cost_per_unit=Decimal("0.10")),
            Ingredient(name="Item 2", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.10")),
            Ingredient(name="Item 3", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.10")),
        )
        recipe = Recipe(id="rec-cd-roundtrip", name="Subcent Cost Mode", ingredients=ings)
        batch = compute_batch(recipe, target_weight=Decimal("120.00"), precision=Decimal("0.01"))

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(recipe)

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_out = str(Path(tmpdir) / "two_dec.pdf")
            print_dlg = PrintBatchSheetDialog(batch, repository=repo)
            self.addCleanup(print_dlg.close)
            print_dlg.operator_input.setText("Operator Lead")
            print_dlg.batch_num_input.setText("BAT-CD-01")
            idx_2dec = print_dlg.cost_precision_combo.findData(2)
            print_dlg.cost_precision_combo.setCurrentIndex(idx_2dec)

            with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(pdf_out, "PDF Files (*.pdf)")):
                with patch("PyQt6.QtWidgets.QMessageBox.information"):
                    print_dlg._export_pdf()

            batches = repo.list_batches(recipe.id)
            self.assertEqual(batches[0]["cost_decimals"], 2)

            ws_file = Path(tmpdir) / "ws_cd.json"
            export_workspace_to_json_file(repo, ws_file)

            repo2 = SQLiteRecipeRepository(":memory:")
            self.addCleanup(repo2.close)
            restore_workspace_from_json(repo2, ws_file)
            self.assertEqual(repo2.list_batches(recipe.id)[0]["cost_decimals"], 2)

            hist_dlg = BatchHistoryDialog(recipe.id, recipe.name, repo2)
            self.addCleanup(hist_dlg.close)
            reprint_pdf = str(Path(tmpdir) / "reprint_two_dec.pdf")
            def _write_mock_pdf(batch, output_path, **kwargs):
                Path(output_path).write_bytes(b"%PDF-mock")

            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(reprint_pdf, "PDF Files (*.pdf)"),
            ):
                with patch(
                    "tared.ui.views.batch_history_dialog.export_batch_sheet_to_pdf",
                    side_effect=_write_mock_pdf,
                ) as mock_pdf:
                    with patch("PyQt6.QtWidgets.QMessageBox.information"):
                        with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                            hist_dlg._reprint_selected_batch()
                            mock_crit.assert_not_called()
                    mock_pdf.assert_called_once()
                    self.assertEqual(mock_pdf.call_args[1]["cost_decimals"], 2)

    def test_render_isolates_ambient_decimal_context_and_rejects_invalid_cost_decimals(self) -> None:
        import decimal

        for bad_cd in (True, False, -1):
            with self.assertRaises(ValueError):
                render_batch_sheet_html(self.batch, batch_number="B-BAD-CD", cost_decimals=bad_cd)  # type: ignore[arg-type]

        shift_batch = compute_batch(self.recipe, target_weight=Decimal("333.33"), precision=Decimal("0.01"))
        orig_ctx = decimal.getcontext().copy()
        try:
            decimal.getcontext().prec = 4
            decimal.getcontext().rounding = decimal.ROUND_DOWN
            html_out = render_batch_sheet_html(shift_batch, batch_number="B-CTX-ISO")
        finally:
            decimal.setcontext(orig_ctx)

        self.assertIn("333.33 g", html_out)
        self.assertIn(f"{shift_batch.max_allocation_deviation:.2f} g", html_out)


if __name__ == "__main__":
    unittest.main()
