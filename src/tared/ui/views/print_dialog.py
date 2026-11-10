"""Print and PDF production batch sheet export dialog with atomic transactional batch logging."""

import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
)

from tared.config import ConfigManager
from tared.core.models import BatchResult
from tared.printing.renderer import export_batch_sheet_to_pdf, render_batch_sheet_html
from tared.storage.base import (
    DuplicateBatchNumberError,
    RecipeNotFoundError,
    RecipeRepository,
    StorageError,
)
from tared.storage.workspace_io import export_workspace_to_json_file


class PrintBatchSheetDialog(QDialog):
    """Modal dialog displaying live reactive print preview with mandatory batch details."""

    def __init__(
        self,
        batch: BatchResult,
        parent: Any = None,
        repository: RecipeRepository | None = None,
        config: ConfigManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.batch = batch
        self.repo = repository
        self.config = config
        self.setWindowTitle(f"Batch Sheet - {batch.recipe_name} (Rev v{batch.recipe_version})")
        self.resize(880, 750)

        self._now = datetime.now(timezone.utc)
        self._utc_created_at = self._now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._fixed_timestamp = self._now.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        self._batch_cache: dict[str, bool] = {}

        main_layout = QVBoxLayout(self)

        form_layout = QFormLayout()
        default_batch_num = (
            f"{datetime.now().astimezone().strftime('BAT-%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
        )
        self.batch_num_input = QLineEdit(default_batch_num)
        self.batch_num_input.setPlaceholderText("Required: Unique Batch Identifier")

        default_op = self.config.default_operator if self.config else ""
        self.operator_input = QLineEdit(default_op)
        self.operator_input.setPlaceholderText("Required: Compounding Operator Name")

        self.notes_input = QTextEdit()
        self.notes_input.setPlaceholderText("Optional: Production instructions or compounding notes...")
        self.notes_input.setMaximumHeight(60)

        self.cost_precision_combo = QComboBox()
        self.cost_precision_combo.addItem("Auto (4 Decimals if Sub-Cent)", None)
        self.cost_precision_combo.addItem("Standard 2 Decimals ($0.01)", 2)
        self.cost_precision_combo.addItem("Fixed 4 Decimals ($0.0001)", 4)

        form_layout.addRow("Batch Number:", self.batch_num_input)
        form_layout.addRow("Operator:", self.operator_input)
        form_layout.addRow("Cost Precision:", self.cost_precision_combo)
        form_layout.addRow("Batch Notes:", self.notes_input)
        main_layout.addLayout(form_layout)

        self.preview_browser = QTextBrowser()
        self.preview_browser.setStyleSheet("background-color: #ffffff; color: #111111; border: 1px solid #cccccc;")
        main_layout.addWidget(self.preview_browser)

        btn_layout = QHBoxLayout()
        self.pdf_btn = QPushButton("Save as PDF...")
        self.close_btn = QPushButton("Close")

        btn_layout.addStretch()
        btn_layout.addWidget(self.pdf_btn)
        btn_layout.addWidget(self.close_btn)
        main_layout.addLayout(btn_layout)

        self.batch_num_input.textChanged.connect(self._on_input_changed)
        self.operator_input.textChanged.connect(self._on_input_changed)
        self.notes_input.textChanged.connect(self._update_preview)
        self.cost_precision_combo.currentIndexChanged.connect(self._update_preview)
        self.pdf_btn.clicked.connect(self._export_pdf)
        self.close_btn.clicked.connect(self.accept)

        self._on_input_changed()

    def _is_batch_duplicate(self, batch_num: str, force_refresh: bool = False) -> bool:
        clean = batch_num.strip()
        if not self.repo or not clean:
            return False
        if force_refresh or clean not in self._batch_cache:
            self._batch_cache[clean] = self.repo.batch_exists(clean)
        return self._batch_cache[clean]

    def _on_input_changed(self) -> None:
        has_operator = bool(self.operator_input.text().strip())
        batch_num = self.batch_num_input.text().strip()
        has_batch_num = bool(batch_num)

        is_duplicate = self._is_batch_duplicate(batch_num) if has_batch_num else False
        is_valid = has_operator and has_batch_num and not is_duplicate
        self.pdf_btn.setEnabled(is_valid)

        if not has_operator:
            self.pdf_btn.setToolTip("Operator name is required before generating batch sheet.")
        elif not has_batch_num:
            self.pdf_btn.setToolTip("Batch number is required before generating batch sheet.")
        elif is_duplicate:
            self.pdf_btn.setToolTip(
                f"Batch identifier '{batch_num}' is already recorded. Please choose a unique number."
            )
        else:
            self.pdf_btn.setToolTip("")

        self._update_preview()

    def _get_current_html(self) -> str:
        op_text = self.operator_input.text().strip() or "Unassigned"
        batch_num = self.batch_num_input.text().strip() or "UNASSIGNED"
        selected_decimals = self.cost_precision_combo.currentData()
        return render_batch_sheet_html(
            batch=self.batch,
            batch_number=batch_num,
            operator=op_text,
            notes=self.notes_input.toPlainText().strip(),
            timestamp_str=self._fixed_timestamp,
            cost_decimals=selected_decimals,
        )

    def _update_preview(self) -> None:
        self.preview_browser.setHtml(self._get_current_html())

    def _prompt_staging_recovery(self, dest_path: Path, staging_pdf: Path, error_msg: Exception) -> str:
        """Display single modal recovery dialog with explicit action buttons."""
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Export Placement Warning")
        msg.setText(
            f"The batch was recorded in the database, but moving the file to destination failed:\n{error_msg}\n\n"
            f"Your generated batch sheet is preserved at:\n{staging_pdf}\n\n"
            f"How would you like to proceed?"
        )
        btn_retry = msg.addButton("Retry move", QMessageBox.ButtonRole.ActionRole)
        btn_save_as = msg.addButton("Save a copy…", QMessageBox.ButtonRole.ActionRole)
        msg.addButton("Leave in staging folder", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_retry)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == btn_retry:
            return "retry"
        if clicked == btn_save_as:
            return "save_as"
        return "close"

    def _export_pdf(self) -> None:
        operator_name = self.operator_input.text().strip()
        batch_number = self.batch_num_input.text().strip()

        if not operator_name or not batch_number:
            QMessageBox.warning(self, "Missing Details", "Operator name and batch number are both required.")
            return

        if self.repo is not None and self._is_batch_duplicate(batch_number, force_refresh=True):
            self._on_input_changed()
            QMessageBox.critical(
                self,
                "Duplicate Batch Identifier",
                f"Batch identifier '{batch_number}' has already been recorded in production records. "
                "Please assign a distinct batch number.",
            )
            return
        clean_name = re.sub(r'[\/*?:"<>| ]', "_", self.batch.recipe_name)
        clean_batch = re.sub(r'[\/*?:"<>| ]', "_", batch_number)
        suggested_filename = f"{clean_name}_{clean_batch}.pdf"

        start_dir = self.config.get_resolved_export_dir() if self.config else Path.home()
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Production Batch Sheet",
            str(start_dir / suggested_filename),
            "PDF Files (*.pdf)",
        )
        if not file_path:
            return

        dest_path = Path(file_path).resolve()
        notes_content = self.notes_input.toPlainText().strip()
        selected_decimals = self.cost_precision_combo.currentData()

        staging_pdf = dest_path.parent / f".tmp_{dest_path.name}_{uuid.uuid4().hex[:8]}"
        db_committed = False

        try:
            now_utc = datetime.now(timezone.utc)
            export_utc_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            export_local_str = now_utc.astimezone().strftime("%Y-%m-%d %H:%M %Z")

            # 1. Render PDF to hidden staging file on destination filesystem with active timestamp
            export_batch_sheet_to_pdf(
                batch=self.batch,
                output_path=staging_pdf,
                batch_number=batch_number,
                operator=operator_name,
                notes=notes_content,
                timestamp_str=export_local_str,
                cost_decimals=selected_decimals,
            )

            # 2. Record batch in production database with instant export timestamp
            if self.repo is not None:
                self.repo.record_batch(
                    recipe_id=self.batch.recipe_id,
                    recipe_version=self.batch.recipe_version,
                    batch_number=batch_number,
                    operator=operator_name,
                    precision=self.batch.precision,
                    target_weight=self.batch.target_weight,
                    total_weight=self.batch.total_weight,
                    total_cost=self.batch.total_cost,
                    items=self.batch.items,
                    notes=notes_content,
                    created_at=export_utc_str,
                )
                if (
                    self.config is not None
                    and self.config.is_backup_configured()
                    and not getattr(self.repo, "_is_memory", False)
                ):
                    try:
                        target_backup = Path(self.config.auto_backup_dir) / "tared_workspace.json"
                        export_workspace_to_json_file(self.repo, target_backup, max_rotations=3)
                        self.config.record_backup_result(True)
                    except Exception as bk_err:
                        import logging
                        logging.getLogger(__name__).warning("Auto backup on batch commit failed: %s", bk_err)
                        self.config.record_backup_result(False, str(bk_err))
            db_committed = True

            # Invalidate cache since this batch number is now stored
            self._batch_cache[batch_number] = True

            # 3. Atomically replace destination with verified staging file
            replace_success = False
            while not replace_success:
                try:
                    os.replace(str(staging_pdf), str(dest_path))
                    replace_success = True
                except Exception as replace_err:
                    choice = self._prompt_staging_recovery(dest_path, staging_pdf, replace_err)
                    if choice == "retry":
                        continue
                    elif choice == "save_as":
                        alt_path, _ = QFileDialog.getSaveFileName(
                            self,
                            "Save Staged Batch Sheet Copy",
                            str(Path.home() / dest_path.name),
                            "PDF Files (*.pdf)",
                        )
                        if alt_path:
                            try:
                                shutil.copyfile(str(staging_pdf), alt_path)
                                try:
                                    staging_pdf.unlink()
                                except OSError:
                                    pass
                                QMessageBox.information(
                                    self,
                                    "Copy Saved",
                                    f"Batch sheet copy successfully saved to:\n{alt_path}",
                                )
                                return
                            except Exception as copy_err:
                                QMessageBox.critical(
                                    self,
                                    "Copy Failed",
                                    f"Could not save copy to {alt_path}:\n{copy_err}\n\n"
                                    f"The staging file remains safely preserved at:\n{staging_pdf}",
                                )
                                return
                        else:
                            msg_text = (
                                "Batch run was recorded in the database, but destination placement was not "
                                "completed.\n\n"
                                f"Your generated batch sheet remains safely preserved at:\n{staging_pdf}"
                            )
                            QMessageBox.information(self, "Staging File Preserved", msg_text)
                            return
                    else:  # "close"
                        msg_text = (
                            "Batch run was recorded in the database, but destination placement was not "
                                "completed.\n\n"
                            f"Your generated batch sheet remains safely preserved at:\n{staging_pdf}"
                        )
                        QMessageBox.information(self, "Staging File Preserved", msg_text)
                        return

            QMessageBox.information(self, "Export Complete", f"Batch sheet exported successfully to:\n{dest_path}")
        except DuplicateBatchNumberError:
            self._batch_cache[batch_number] = True
            self._on_input_changed()
            QMessageBox.critical(
                self,
                "Database Error",
                f"Batch identifier '{batch_number}' has already been recorded. Please assign a distinct batch number.",
            )
        except RecipeNotFoundError:
            QMessageBox.critical(
                self,
                "Database Error",
                "Cannot record production batch: Formula must be saved to the database prior to batch generation.",
            )
        except StorageError as err:
            QMessageBox.critical(self, "Database Error", f"Database error while recording batch:\n{err}")
        except Exception as err:
            QMessageBox.critical(self, "Export Failed", f"Could not generate batch sheet: {err}")
        finally:
            if not db_committed and staging_pdf.exists():
                try:
                    staging_pdf.unlink()
                except OSError:
                    pass
