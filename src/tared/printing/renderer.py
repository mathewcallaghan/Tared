"""HTML batch sheet generator and PDF rendering bridge with strict QTextDocument compatibility."""

import html
import re
import sys
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from tared.core.formatting import determine_cost_precision, format_currency, format_percentage
from tared.core.models import BatchResult

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    TEMPLATE_PATH = Path(sys._MEIPASS) / "tared" / "printing" / "templates" / "batch_sheet.html"
else:
    TEMPLATE_PATH = Path(__file__).parent / "templates" / "batch_sheet.html"


def get_default_timestamp() -> str:
    """Format current local system time with active timezone identifier."""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


def render_batch_sheet_html(
    batch: BatchResult,
    batch_number: str = "",
    operator: str = "Unassigned",
    notes: str = "",
    timestamp_str: str | None = None,
    unit: str = "g",
    currency_symbol: str = "$",
    cost_decimals: int | None = None,
) -> str:
    """Render a standalone, print-ready HTML batch sheet dynamically formatted to scale precision quantum."""
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Batch sheet HTML template not found at {TEMPLATE_PATH}")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    if timestamp_str is None:
        timestamp_str = get_default_timestamp()
    if not batch_number.strip():
        batch_number = datetime.now().strftime("BAT-%Y%m%d-%H%M")
    if not operator.strip():
        operator = "Unassigned"

    escaped_unit = html.escape(unit)
    escaped_currency = html.escape(currency_symbol)

    if cost_decimals is None:
        cost_decimals = determine_cost_precision([it.cost for it in batch.items])

    exp = batch.precision.as_tuple().exponent
    prec_decimals = max(0, -exp) if isinstance(exp, int) else 2

    row_fragments: list[str] = []
    for idx, item in enumerate(batch.items, start=1):
        bg_attr = ' bgcolor="#fafbfc"' if idx % 2 == 0 else ""
        pct_formatted = format_percentage(item.percentage)
        cost_formatted = format_currency(item.cost, precision=cost_decimals, currency_symbol=escaped_currency)
        row_html = (
            f"<tr{bg_attr}>\n"
            f'  <td align="center">{idx}</td>\n'
            f'  <td align="left"><strong>{html.escape(item.ingredient_name)}</strong></td>\n'
            f'  <td align="right">{pct_formatted}%</td>\n'
            f'  <td align="right">{item.scaled_weight:.{prec_decimals}f} {escaped_unit}</td>\n'
            f'  <td class="col-tare" align="right">{item.cumulative_tare:.{prec_decimals}f} {escaped_unit}</td>\n'
            f'  <td align="right">{cost_formatted}</td>\n'
            f'  <td align="center">&#9633;</td>\n'
            f"</tr>"
        )
        row_fragments.append(row_html)

    rendered_rows = "\n".join(row_fragments)
    escaped_notes = html.escape(notes).replace("\n", "<br>") if notes.strip() else "None provided."
    raw_instructions = getattr(batch, "recipe_description", "")
    escaped_instructions = (
        html.escape(raw_instructions).replace("\n", "<br>")
        if raw_instructions.strip()
        else "Standard compounding procedure."
    )

    # Surface maximum discrete quantization deviation when non-zero
    if batch.max_allocation_deviation > Decimal("0"):
        deviation_row = (
            f"<tr>\n"
            f'  <td align="left" style="font-size: 8.5pt; font-weight: normal; color: #555555;">'
            f"Discrete Allocation Shift (at most):</td>\n"
            f'  <td align="right" style="font-size: 8.5pt; font-weight: normal; color: #555555;">'
            f"{batch.max_allocation_deviation:.{prec_decimals}f} {escaped_unit}</td>\n"
            f'  <td colspan="2">&nbsp;</td>\n'
            f"</tr>"
        )
    else:
        deviation_row = ""

    quantum = Decimal(10) ** -cost_decimals
    sum_printed_lines = sum(
        (item.cost.quantize(quantum, rounding=ROUND_HALF_UP) for item in batch.items),
        Decimal("0"),
    )
    total_printed = batch.total_cost.quantize(quantum, rounding=ROUND_HALF_UP)

    if sum_printed_lines != total_printed:
        unrounded_cost_str = format_currency(batch.total_cost, precision=4, currency_symbol=escaped_currency)
        cost_footnote = (
            f'<div style="font-size: 8pt; color: #666666; text-align: right; margin-top: -4px; margin-bottom: 8px;">'
            f'* Line items displayed to {cost_decimals} decimals; formula total shown '
            f'({unrounded_cost_str}) is rounded to 4 decimals from exact unrounded valuation.'
            f"</div>"
        )
    else:
        cost_footnote = ""

    total_cost_formatted = format_currency(batch.total_cost, precision=cost_decimals, currency_symbol=escaped_currency)

    replacements = {
        "RECIPE_NAME": html.escape(batch.recipe_name),
        "RECIPE_VERSION": str(batch.recipe_version),
        "BATCH_NUMBER": html.escape(batch_number),
        "OPERATOR": html.escape(operator),
        "GENERATED_AT": html.escape(timestamp_str),
        "TARGET_WEIGHT": f"{batch.target_weight:.{prec_decimals}f} {escaped_unit}",
        "TOTAL_WEIGHT": f"{batch.total_weight:.{prec_decimals}f} {escaped_unit}",
        "TOTAL_COST": total_cost_formatted,
        "DEVIATION_ROW": deviation_row,
        "COST_FOOTNOTE": cost_footnote,
        "FORMULA_INSTRUCTIONS": escaped_instructions,
        "BATCH_NOTES": escaped_notes,
        "NOTES": escaped_notes,
        "TABLE_ROWS": rendered_rows,
    }

    token_pattern = re.compile(r"\{\{\s*(\w+)\s*\}\}")
    return token_pattern.sub(lambda match: replacements.get(match.group(1), match.group(0)), template)


def export_batch_sheet_to_pdf(
    batch: BatchResult,
    output_path: str | Path,
    batch_number: str = "",
    operator: str = "Unassigned",
    notes: str = "",
    timestamp_str: str | None = None,
    unit: str = "g",
    currency_symbol: str = "$",
    cost_decimals: int | None = None,
) -> Path:
    """Render batch result directly to a PDF file using explicit page dimensions."""
    if timestamp_str is None:
        timestamp_str = get_default_timestamp()

    html_content = render_batch_sheet_html(
        batch=batch,
        batch_number=batch_number,
        operator=operator,
        notes=notes,
        timestamp_str=timestamp_str,
        unit=unit,
        currency_symbol=currency_symbol,
        cost_decimals=cost_decimals,
    )

    try:
        from PyQt6.QtCore import QMarginsF
        from PyQt6.QtGui import QGuiApplication, QPageLayout, QPageSize, QTextDocument
        from PyQt6.QtPrintSupport import QPrinter
    except ImportError as err:
        raise ImportError(
            "PyQt6 is required for direct PDF compilation. Install PyQt6 or use render_batch_sheet_html."
        ) from err

    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication(["tared-renderer", "-platform", "offscreen"])

    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out_file))

    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4),
        QPageLayout.Orientation.Portrait,
        QMarginsF(10.0, 10.0, 10.0, 10.0),
        QPageLayout.Unit.Millimeter,
    )
    printer.setPageLayout(layout)

    doc = QTextDocument()
    doc.setDocumentMargin(0)
    # Set explicit page size to printable area in points to suppress Qt's default 2cm margin and page counter
    printable_size = printer.pageLayout().paintRect(QPageLayout.Unit.Point).size()
    doc.setPageSize(printable_size)
    doc.setHtml(html_content)
    doc.print(printer)

    return out_file
