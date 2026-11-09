"""Desktop application entry point with persistent error file logging and CLI handling."""

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

import tared
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tared.ui.views.main_window import MainWindow

LOG_FILE: Path | None = None


def global_exception_hook(exctype, value, tb) -> None:
    """Catch unhandled exceptions in Qt slots, log to persistent file, and display modal dialog."""
    trace_msg = "".join(traceback.format_exception(exctype, value, tb))
    if sys.__stderr__ is not None:
        try:
            sys.__stderr__.write(trace_msg)
        except Exception:
            pass

    if LOG_FILE is not None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n--- Application Crash: {datetime.now().isoformat()} ---\n")
                f.write(trace_msg)
        except Exception:
            pass

    from PyQt6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance()
    if app is not None:
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setWindowTitle("Unexpected Application Error")
        msg.setText("An unexpected application error occurred.")
        msg.setInformativeText(str(value))
        msg.setDetailedText(trace_msg)
        msg.exec()


def parse_cli_args() -> None:
    """Handle standard CLI flags (--help, --version) before Qt application initialization."""
    version_str = getattr(tared, "__version__", "1.0.0")
    parser = argparse.ArgumentParser(
        prog="tared",
        description="Precision recipe formulation, batch scaling, and production management",
        add_help=True,
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {version_str}",
    )

    # Intercept help and version flags without triggering GUI subsystems
    if any(arg in sys.argv[1:] for arg in ("-h", "--help", "-v", "--version")):
        parser.parse_args()


def main() -> None:
    global LOG_FILE

    parse_cli_args()

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("tared")
    app.setApplicationDisplayName("")

    data_dir = Path.home() / ".local" / "share" / "tared"
    data_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = data_dir / "tared_crash.log"

    sys.excepthook = global_exception_hook

    db_path = data_dir / "recipes.db"

    # Migrate legacy data directory if active database does not yet exist
    if not db_path.exists():
        legacy_db = Path.home() / ".local" / "share" / "recipe_studio" / "recipes.db"
        if legacy_db.is_file():
            try:
                import shutil

                shutil.copy2(str(legacy_db), str(db_path))
                if sys.__stderr__ is not None:
                    sys.__stderr__.write(
                        f"Notice: Migrated legacy formulation database from {legacy_db} to {db_path}\n"
                    )
            except Exception as mig_err:
                if sys.__stderr__ is not None:
                    sys.__stderr__.write(f"Warning: Failed to migrate legacy database: {mig_err}\n")

    repo = SQLiteRecipeRepository(db_path)
    window = MainWindow(repo)
    if not window.windowIcon().isNull():
        app.setWindowIcon(window.windowIcon())
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
