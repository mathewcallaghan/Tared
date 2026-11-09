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


if __name__ == "__main__":
    unittest.main()
