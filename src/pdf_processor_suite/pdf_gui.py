#!/usr/bin/env python3
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# Ensure the script can find the pdf_split_rasterize module
try:
    import pdf_split_rasterize
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import pdf_split_rasterize


# Define a simple Args class to hold attributes like argparse.Namespace
# --- Configuration Management ---
def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # Running in a PyInstaller bundle
        _base_path = sys._MEIPASS  # type: ignore
    else:
        # Running in a normal Python environment
        _base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(_base_path, relative_path)


CONFIG_FILE = resource_path("config.json")

CONFIG = {"gs_path": "gs", "magick_path": "magick"}


def load_config():
    global CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                CONFIG.update(json.load(f))
        except (json.JSONDecodeError, TypeError):
            logging.warning("Could not read config.json. Using default paths.")


def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(CONFIG, f, indent=4)


class WorkerSignals(QObject):
    """Defines signals available from a running worker thread."""

    finished = pyqtSignal()
    error = pyqtSignal(str, str)
    log = pyqtSignal(str)
    progress = pyqtSignal(int, str)
    summary = pyqtSignal(dict)
    cleanup_ui = pyqtSignal(str)


class Worker(QRunnable):
    """Worker thread for running tasks in the background."""

    def __init__(self, fn, cancel_event, pause_event, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.cancel_event = cancel_event
        self.pause_event = pause_event

    def run(self):
        """Set up logging and run the target function."""

        class QtLogHandler(logging.Handler):
            def __init__(self, signals):
                super().__init__()
                self.signals = signals

            def emit(self, record):
                # Use the worker's signals object to emit logs
                self.signals.log.emit(self.format(record))

        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        handler = QtLogHandler(self.signals)
        logger.handlers = [handler]  # Replace existing handlers for this thread

        try:
            # Call the target function (e.g., run_split_and_rasterize_wrapper)
            self.fn(
                *self.args,
                **self.kwargs,
                signals=self.signals,
                cancel_event=self.cancel_event,
                pause_event=self.pause_event,
            )
        except Exception as e:
            self.signals.error.emit(
                "Worker Error", f"An unexpected error occurred in the worker: {e}"
            )
        finally:
            logger.removeHandler(handler)  # Clean up the handler
            self.signals.finished.emit()  # Signal that the worker is done


class Args:
    """A simple class to hold attributes like argparse.Namespace."""

    def __init__(self):
        self.input: Optional[Path] = None
        self.output: Optional[Path] = None
        self.resolution: Optional[int] = None
        self.keep_originals: Optional[bool] = None
        self.dry_run: bool = False
        self.workers: Optional[int] = None
        self.flatten_output: bool = False
        self.max_split_level: Optional[int] = None
        self.gs_path: Optional[Path] = None
        self.magick_path: Optional[Path] = None
        self.html_report: Optional[Path] = None
        self.merge: Optional[Path] = None
        self.merge_output: Optional[Path] = None
        self.no_recreate_bookmarks: bool = False
        self.verbose: bool = False  # Added verbose since it's a common arg


def run_split_and_rasterize_wrapper(
    input_pdf,
    output_dir,
    dpi,
    keep_originals,
    dry_run,
    workers,
    config,
    flatten_output,
    max_split_level,
    rasterize,
    gs_path,  # Added gs_path
    magick_path,  # Added magick_path
    signals,
    cancel_event,
    pause_event,
):
    """Wrapper to call the main script's entry point for splitting and rasterizing."""

    args = Args()
    args.input = Path(input_pdf)
    args.output = Path(output_dir)
    args.resolution = dpi
    args.keep_originals = keep_originals
    args.dry_run = dry_run
    args.workers = workers
    args.flatten_output = flatten_output
    args.max_split_level = max_split_level
    args.gs_path = gs_path  # Set gs_path
    args.magick_path = magick_path  # Set magick_path
    args.html_report = None

    _, _, report_data = pdf_split_rasterize.main_entry(
        args,
        lambda v, t: signals.progress.emit(v, t),
        cancel_event,
        pause_event,
        rasterize=rasterize,
    )
    if not cancel_event.is_set():
        signals.summary.emit(report_data)
    signals.cleanup_ui.emit("split")


def run_merge_wrapper(
    merge_dir,
    merge_output_path,  # Added output path for merge
    recreate_bookmarks,
    dry_run,  # Added dry_run flag
    config,
    signals,
    cancel_event,
    pause_event,
    gs_path,
    magick_path,
):
    """Wrapper to call the main script's entry point for merging."""

    args = Args()
    args.merge = Path(merge_dir)
    args.merge_output = None
    args.dry_run = False
    args.no_recreate_bookmarks = not recreate_bookmarks
    args.gs_path = (
        gs_path  # Set gs_path (not strictly needed for merge, but passed consistently)
    )
    args.magick_path = magick_path  # Set magick_path (not strictly needed for merge, but passed consistently)

    _, _, report_data = pdf_split_rasterize.main_entry(
        args, lambda v, t: signals.progress.emit(v, t), cancel_event, pause_event
    )
    if not cancel_event.is_set():  # Only emit summary if not cancelled
        signals.summary.emit(report_data)
    signals.cleanup_ui.emit("merge")


class SettingsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        layout = QGridLayout(self)

        # Ghostscript Path
        self.gs_path_edit = QLineEdit(CONFIG.get("gs_path", "gs"))
        layout.addWidget(QLabel("Ghostscript (gs) Path:"), 0, 0)
        layout.addWidget(self.gs_path_edit, 0, 1)
        gs_browse_btn = QPushButton("Browse")
        gs_browse_btn.clicked.connect(lambda: self.browse_file(self.gs_path_edit))
        layout.addWidget(gs_browse_btn, 0, 2)

        # ImageMagick Path
        self.magick_path_edit = QLineEdit(CONFIG.get("magick_path", "magick"))
        layout.addWidget(QLabel("ImageMagick (magick) Path:"), 1, 0)
        layout.addWidget(self.magick_path_edit, 1, 1)
        magick_browse_btn = QPushButton("Browse")
        magick_browse_btn.clicked.connect(
            lambda: self.browse_file(self.magick_path_edit)
        )
        layout.addWidget(magick_browse_btn, 1, 2)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box, 2, 0, 1, 3)

    def browse_file(self, line_edit):
        path, _ = QFileDialog.getOpenFileName(self, "Select Executable")
        if path:
            line_edit.setText(path)

    def accept(self):
        CONFIG["gs_path"] = self.gs_path_edit.text()
        CONFIG["magick_path"] = self.magick_path_edit.text()
        save_config()
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = {}
        self.threadpool = QThreadPool()
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.setWindowTitle("PDF Rasterize")
        self.setGeometry(100, 100, 700, 800)
        self.create_ui()
        self.input_path_edit.setText(os.path.expanduser("~/Downloads"))
        self.output_path_edit.setText(os.path.expanduser("~/Desktop/Split Books"))

    def create_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        self.create_menu()

        tabs = QTabWidget()
        self.split_tab = QWidget()
        self.merge_tab = QWidget()
        tabs.addTab(self.split_tab, "Split")
        tabs.addTab(self.merge_tab, "Merge")
        main_layout.addWidget(tabs)

        self.create_split_tab_ui()
        self.create_merge_tab_ui()

        # --- Progress Bar ---
        self.progress_bar = QProgressBar()
        self.progress_bar.hide()
        main_layout.addWidget(self.progress_bar)

        # --- Log Box ---
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Courier", 9))
        main_layout.addWidget(self.log_box)

    def create_menu(self):
        from PyQt6.QtWidgets import (  # Import QMenuBar, QMenu for type hinting
            QMenu,
            QMenuBar,
        )
        # Optional is already imported at the top of the file

        menu_bar: Optional[QMenuBar] = self.menuBar()
        if menu_bar:
            file_menu: Optional[QMenu] = menu_bar.addMenu("&File")
            if file_menu:
                settings_action = QAction("Settings", self)
                settings_action.triggered.connect(lambda: SettingsWindow(self).exec())
                file_menu.addAction(settings_action)
                file_menu.addSeparator()
                exit_action = QAction("Exit", self)
                file_menu.addAction(exit_action)
                exit_action.triggered.connect(self.close)
            else:
                print("Warning: File menu not available for MainWindow.")
        else:
            # Handle the case where menuBar() returns None.
            # In a production application, more robust error handling or
            # ensuring the menu bar is always initialized might be needed.
            print("Warning: QMenuBar not available for MainWindow.")

    def create_split_tab_ui(self):
        layout = QVBoxLayout(self.split_tab)
        io_group = QGroupBox("Input and Output")
        io_layout = QGridLayout(io_group)
        self.input_path_edit = QLineEdit()
        self.output_path_edit = QLineEdit()
        browse_in_btn = QPushButton("Browse")
        browse_in_btn.clicked.connect(self.browse_input)
        browse_out_btn = QPushButton("Browse")
        browse_out_btn.clicked.connect(self.browse_output)
        io_layout.addWidget(QLabel("Input PDF:"), 0, 0)
        io_layout.addWidget(self.input_path_edit, 0, 1)
        io_layout.addWidget(browse_in_btn, 0, 2)
        io_layout.addWidget(QLabel("Output Folder:"), 1, 0)
        io_layout.addWidget(self.output_path_edit, 1, 1)
        io_layout.addWidget(browse_out_btn, 1, 2)
        layout.addWidget(io_group)

        settings_group = QGroupBox("Settings")
        settings_layout = QGridLayout(settings_group)
        self.dpi_spinbox = QSpinBox()
        self.dpi_spinbox.setRange(100, 1200)
        self.dpi_spinbox.setValue(self.config.get("dpi", 300))
        self.workers_spinbox = QSpinBox()
        self.workers_spinbox.setRange(1, os.cpu_count() or 1)
        self.workers_spinbox.setValue(self.config.get("workers", os.cpu_count() or 1))
        self.keep_originals_chk = QCheckBox("Keep original split PDFs")
        self.keep_originals_chk.setChecked(self.config.get("keep_originals", False))
        self.dry_run_chk = QCheckBox("Dry Run (show what would happen)")
        self.dry_run_chk.setChecked(self.config.get("dry_run", False))
        self.flatten_output_chk = QCheckBox("Create flat output folder")
        self.flatten_output_chk.setChecked(self.config.get("flatten_output", True))
        settings_layout.addWidget(QLabel("DPI:"), 0, 0)
        settings_layout.addWidget(self.dpi_spinbox, 0, 1)
        settings_layout.addWidget(QLabel("Workers:"), 0, 2)
        settings_layout.addWidget(self.workers_spinbox, 0, 3)
        settings_layout.addWidget(self.keep_originals_chk, 1, 0, 1, 2)
        settings_layout.addWidget(self.dry_run_chk, 2, 0, 1, 2)
        settings_layout.addWidget(self.flatten_output_chk, 3, 0, 1, 4)

        self.rasterize_checkbox = QCheckBox("Rasterize after splitting")
        self.rasterize_checkbox.setChecked(self.config.get("rasterize", True))
        settings_layout.addWidget(self.rasterize_checkbox, 5, 0, 1, 2)

        self.limit_level_chk = QCheckBox("Limit splitting to bookmark level:")
        self.limit_level_chk.setChecked(True)
        self.max_level_spinbox = QSpinBox()
        self.max_level_spinbox.setRange(1, 20)
        self.max_level_spinbox.setValue(1)
        self.max_level_spinbox.setEnabled(True)
        self.limit_level_chk.toggled.connect(self.max_level_spinbox.setEnabled)
        settings_layout.addWidget(self.limit_level_chk, 4, 0, 1, 2)
        settings_layout.addWidget(self.max_level_spinbox, 4, 2, 1, 2)
        layout.addWidget(settings_group)

        button_layout = QHBoxLayout()
        self.split_start_btn = QPushButton("Start Processing")
        self.split_start_btn.clicked.connect(self.start_split_process)
        self.split_pause_resume_btn = QPushButton("Pause")
        self.split_pause_resume_btn.clicked.connect(self.toggle_pause_resume)
        self.split_cancel_btn = QPushButton("Cancel")
        self.split_cancel_btn.clicked.connect(self.cancel_process)
        button_layout.addWidget(self.split_start_btn)
        button_layout.addWidget(self.split_pause_resume_btn)
        button_layout.addWidget(self.split_cancel_btn)
        self.split_pause_resume_btn.hide()
        self.split_cancel_btn.hide()
        layout.addLayout(button_layout)

    def create_merge_tab_ui(self):
        layout = QVBoxLayout(self.merge_tab)
        input_group = QGroupBox("Input Directory")
        input_layout = QGridLayout(input_group)
        self.merge_dir_edit = QLineEdit()
        browse_merge_btn = QPushButton("Browse")
        browse_merge_btn.clicked.connect(self.browse_merge_dir)
        input_layout.addWidget(QLabel("Folder:"), 0, 0)
        input_layout.addWidget(self.merge_dir_edit, 0, 1)
        input_layout.addWidget(browse_merge_btn, 0, 2)
        layout.addWidget(input_group)

        self.recreate_bookmarks_chk = QCheckBox("Re-create bookmarks from original PDF")
        self.recreate_bookmarks_chk.setChecked(True)
        layout.addWidget(self.recreate_bookmarks_chk)

        button_layout = QHBoxLayout()
        self.merge_start_btn = QPushButton("Start Merging")
        self.merge_start_btn.clicked.connect(self.start_merge_process)
        self.merge_pause_resume_btn = QPushButton("Pause")
        self.merge_pause_resume_btn.clicked.connect(self.toggle_pause_resume)
        self.merge_cancel_btn = QPushButton("Cancel")
        self.merge_cancel_btn.clicked.connect(self.cancel_process)
        button_layout.addWidget(self.merge_start_btn)
        button_layout.addWidget(self.merge_pause_resume_btn)
        button_layout.addWidget(self.merge_cancel_btn)
        self.merge_pause_resume_btn.hide()
        self.merge_cancel_btn.hide()
        layout.addLayout(button_layout)
        layout.addStretch()

    def browse_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Input PDF", "", "PDF Files (*.pdf)"
        )
        if path:
            self.input_path_edit.setText(path)

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if path:
            self.output_path_edit.setText(path)

    def browse_merge_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Folder with Rasterized PDFs"
        )
        if path:
            self.merge_dir_edit.setText(path)

    def start_split_process(self):
        input_pdf = self.input_path_edit.text()
        output_dir = self.output_path_edit.text()

        if not input_pdf or not output_dir:
            self.show_error(
                "Input Error", "Please specify both input PDF and output folder."
            )
            return

        self.split_start_btn.hide()
        self.split_pause_resume_btn.setText("Pause")
        self.split_pause_resume_btn.show()
        self.split_cancel_btn.show()
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.show()

        # Save settings for next time
        self.config["input_path"] = input_pdf
        self.config["output_path"] = output_dir
        self.config["dpi"] = self.dpi_spinbox.value()
        self.config["rasterize"] = self.rasterize_checkbox.isChecked()
        self.config["keep_originals"] = self.keep_originals_chk.isChecked()
        self.config["flatten_output"] = self.flatten_output_chk.isChecked()
        self.config["max_split_level"] = (
            self.max_level_spinbox.value() if self.limit_level_chk.isChecked() else 0
        )

        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()

        worker = Worker(
            run_split_and_rasterize_wrapper,
            self.cancel_event,
            self.pause_event,
            input_pdf,
            output_dir,
            self.dpi_spinbox.value(),
            self.keep_originals_chk.isChecked(),
            self.dry_run_chk.isChecked(),
            self.workers_spinbox.value(),
            self.config,
            self.flatten_output_chk.isChecked(),
            self.max_level_spinbox.value() if self.limit_level_chk.isChecked() else 0,
            self.rasterize_checkbox.isChecked(),  # Pass the checkbox state
            self.config.get("gs_path", "gs"),  # Pass Ghostscript path
            self.config.get("magick_path", "magick"),  # Pass ImageMagick path
        )
        self.connect_worker_signals(worker)
        self.threadpool.start(worker)

    def start_merge_process(self):
        merge_dir = self.merge_dir_edit.text()
        if not merge_dir or not os.path.isdir(merge_dir):
            self.show_error("Input Error", "Please select a valid directory to merge.")
            return

        self.log_box.clear()
        self.update_log("Starting merge process...")
        self.progress_bar.show()
        self.merge_start_btn.hide()
        self.merge_pause_resume_btn.show()
        self.merge_cancel_btn.show()

        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()

        worker = Worker(
            run_merge_wrapper,
            self.cancel_event,
            self.pause_event,
            merge_dir,
            self.merge_dir_edit.text(),
            self.recreate_bookmarks_chk.isChecked(),
            self.dry_run_chk.isChecked(),
            self.config,
            self.config.get(
                "gs_path", "gs"
            ),  # Pass Ghostscript path (though not used in merge_pdfs directly, good practice)
            self.config.get(
                "magick_path", "magick"
            ),  # Pass ImageMagick path (though not used in merge_pdfs directly, good practice)
        )
        self.connect_worker_signals(worker)
        self.threadpool.start(worker)

    def connect_worker_signals(self, worker):
        worker.signals.log.connect(self.update_log)
        worker.signals.progress.connect(self.update_progress)
        worker.signals.error.connect(self.show_error)
        worker.signals.summary.connect(self.show_summary)
        worker.signals.cleanup_ui.connect(self.cleanup_ui)

    def update_log(self, message):
        self.log_box.append(message)

    def update_progress(self, value, text):
        """Update the progress bar and log the status message."""
        self.progress_bar.setValue(value)
        if text:
            self.update_log(text)

    def show_error(self, title, message):
        QMessageBox.critical(self, title, message)

    def show_summary(self, report_data):
        end_time = time.time()
        duration = end_time - report_data.get("start_time", end_time)
        summary_lines = [
            f"\n{'=' * 30}",
            "PROCESS SUMMARY",
            f"{'=' * 30}",
            f"Operation: {report_data.get('operation_type', 'N/A').replace('_', ' ').title()}",
            f"Duration: {duration:.2f} seconds",
            f"Total files processed: {report_data.get('total_files_processed', 0)}",
            f"Successful: {len(report_data.get('success', []))}",
            f"Failed: {len(report_data.get('failures', []))}",
            f"{'=' * 30}\n",
        ]
        self.update_log("\n".join(summary_lines))

    def cleanup_ui(self, operation_type):
        self.progress_bar.hide()
        if operation_type == "split":
            self.split_start_btn.show()
            self.split_pause_resume_btn.hide()
            self.split_cancel_btn.hide()
        elif operation_type == "merge":
            self.merge_start_btn.show()
            self.merge_pause_resume_btn.hide()
            self.merge_cancel_btn.hide()
        self.cancel_event.clear()

    def toggle_pause_resume(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.update_log("--- Resumed ---")
            self.split_pause_resume_btn.setText("Pause")
            self.merge_pause_resume_btn.setText("Pause")
        else:
            self.pause_event.set()
            self.update_log("--- Paused ---")
            self.split_pause_resume_btn.setText("Resume")
            self.merge_pause_resume_btn.setText("Resume")

    def cancel_process(self):
        if self.threadpool.activeThreadCount() > 0:
            self.update_log("--- Cancelling... ---")
            self.cancel_event.set()
            if not self.pause_event.is_set():
                self.pause_event.set()  # Unpause to allow cancellation to proceed


if __name__ == "__main__":
    app = QApplication(sys.argv)

    load_config()
    window = MainWindow()
    window.show()
    app.exec()
