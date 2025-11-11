#!/usr/bin/env python3
import json
import logging
import multiprocessing
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, QStandardPaths
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

from . import pdf_split_rasterize


# Define a simple Args class to hold attributes like argparse.Namespace
# --- Configuration Management ---


class ConfigManager:
    def __init__(self, config_file_path):
        self.config_file = config_file_path
        self.config = self._load_defaults()

        self.load()

    def _load_defaults(self):
        return {"gs_path": "gs", "magick_path": "magick"}

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value

    def load(self):
        """Load configuration from a JSON file, with error handling."""
        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    user_config = json.load(f)
                    if isinstance(user_config, dict):
                        self.config.update(user_config)
                    else:
                        raise TypeError("Configuration is not a dictionary.")
            except (json.JSONDecodeError, TypeError) as e:
                logging.warning(f"Could not read config.json: {e}. Using default paths.")
                # Show a warning to the user
                msg_box = QMessageBox()
                msg_box.setIcon(QMessageBox.Icon.Warning)
                msg_box.setText("Could not read the configuration file.")
                msg_box.setInformativeText(
                    f"The file at {self.config_file} might be corrupted. "
                    "The application will proceed with default settings."
                )
                msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
                msg_box.exec()

    def save(self):
        """Save the current configuration to a JSON file."""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, "w") as f:
                json.dump(self.config, f, indent=4)
        except OSError as e:
            logging.error(f"Could not save configuration to {self.config_file}: {e}")
            # Optionally, inform the user about the failure
            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Icon.Critical)
            msg_box.setText("Failed to save settings.")
            msg_box.setInformativeText(
                f"Could not write to the configuration file at {self.config_file}."
            )
            msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg_box.exec()


def get_config_dir() -> Path:
    """Return the application's configuration directory."""
    return Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    )


CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
config_manager = ConfigManager(CONFIG_FILE)


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


def run_split_and_rasterize_wrapper(
    input_pdf,
    output_dir,
    dpi,
    keep_originals,
    dry_run,
    workers,
    flatten_output,
    max_split_level,
    rasterize,
    gs_path,
    magick_path,
    signals,
    cancel_event,
    pause_event,
):
    """Wrapper to call the main script's entry point for splitting and rasterizing."""

    # record start time so SummaryDialog can compute duration
    start_time = time.time()

    args = SimpleNamespace(
        input=Path(input_pdf),
        output=Path(output_dir),
        resolution=dpi,
        keep_originals=keep_originals,
        dry_run=dry_run,
        workers=workers,
        flatten_output=flatten_output,
        max_split_level=max_split_level,
        gs_path=gs_path,
        magick_path=magick_path,
        html_report=None,
        verbose=False,
    )

    _, _, report_data = pdf_split_rasterize.main_entry(
        args,
        lambda v, t: signals.progress.emit(v, t),
        cancel_event,
        pause_event,
        rasterize=rasterize,
    )
    # attach timing information so the summary dialog can show duration
    report_data = report_data or {}
    report_data.setdefault("start_time", start_time)
    report_data.setdefault("end_time", time.time())
    report_data.setdefault("elapsed_time", report_data["end_time"] - report_data["start_time"])

    if not cancel_event.is_set():
        signals.summary.emit(report_data)
    signals.cleanup_ui.emit("split")


def run_merge_wrapper(
    merge_dir,
    merge_output_path,
    recreate_bookmarks,
    dry_run,
    signals,
    cancel_event,
    pause_event,
    gs_path,
    magick_path,
):
    """Wrapper to call the main script's entry point for merging."""

    # record start time so SummaryDialog can compute duration
    start_time = time.time()

    args = SimpleNamespace(
        merge=Path(merge_dir),
        merge_output=None,
        dry_run=False,
        no_recreate_bookmarks=not recreate_bookmarks,
        gs_path=gs_path,
        magick_path=magick_path,
        verbose=False,
    )

    _, _, report_data = pdf_split_rasterize.main_entry(
        args, lambda v, t: signals.progress.emit(v, t), cancel_event, pause_event
    )
    # attach timing information
    report_data = report_data or {}
    report_data.setdefault("start_time", start_time)
    report_data.setdefault("end_time", time.time())
    report_data.setdefault("elapsed_time", report_data["end_time"] - report_data["start_time"])

    if not cancel_event.is_set():  # Only emit summary if not cancelled
        signals.summary.emit(report_data)
    signals.cleanup_ui.emit("merge")


class SettingsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        layout = QGridLayout(self)

        # Ghostscript Path
        self.gs_path_edit = QLineEdit(config_manager.get("gs_path", "gs"))
        layout.addWidget(QLabel("Ghostscript (gs) Path:"), 0, 0)
        layout.addWidget(self.gs_path_edit, 0, 1)
        gs_browse_btn = QPushButton("Browse")
        gs_browse_btn.clicked.connect(lambda: self.browse_file(self.gs_path_edit))
        layout.addWidget(gs_browse_btn, 0, 2)

        # ImageMagick Path
        self.magick_path_edit = QLineEdit(config_manager.get("magick_path", "magick"))
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
        config_manager.set("gs_path", self.gs_path_edit.text())
        config_manager.set("magick_path", self.magick_path_edit.text())
        config_manager.save()
        super().accept()


class SummaryDialog(QDialog):
    """A dialog to show the summary of a process."""

    def __init__(self, report_data, parent=None):
        super().__init__(parent)
        self.report_data = report_data
        self.setWindowTitle("Process Summary")
        self.setMinimumWidth(600)

        layout = QVBoxLayout(self)

        # Basic summary labels
        # Prefer an elapsed_time value provided by the worker/report_data.
        # Fall back to end_time - start_time, then to time.time() - start_time.
        elapsed = report_data.get("elapsed_time")
        if elapsed is None:
            start = report_data.get("start_time")
            end = report_data.get("end_time")
            if start is not None and end is not None:
                try:
                    elapsed = float(end) - float(start)
                except Exception:
                    elapsed = 0.0
            elif start is not None:
                try:
                    elapsed = time.time() - float(start)
                except Exception:
                    elapsed = 0.0
            else:
                elapsed = 0.0
        duration = elapsed
        summary_layout = QGridLayout()
        summary_layout.addWidget(QLabel("Operation:"), 0, 0)
        summary_layout.addWidget(
            QLabel(
                report_data.get("operation_type", "N/A").replace("_", " ").title()
            ),
            0,
            1,
        )
        summary_layout.addWidget(QLabel("Duration:"), 1, 0)
        summary_layout.addWidget(QLabel(f"{duration:.2f} seconds"), 1, 1)
        summary_layout.addWidget(QLabel("Total Files Processed:"), 2, 0)
        summary_layout.addWidget(
            QLabel(str(report_data.get("total_files_processed", 0))), 2, 1
        )
        summary_layout.addWidget(QLabel("Successful:"), 3, 0)
        summary_layout.addWidget(
            QLabel(str(len(report_data.get("success", [])))), 3, 1
        )
        summary_layout.addWidget(QLabel("Failed:"), 4, 0)
        summary_layout.addWidget(
            QLabel(str(len(report_data.get("failures", [])))), 4, 1
        )
        layout.addLayout(summary_layout)

        # Details for successes and failures
        if report_data.get("success") or report_data.get("failures"):
            details_group = QGroupBox("Details")
            details_layout = QVBoxLayout(details_group)

            if report_data.get("success"):
                success_button = QPushButton("Show Successful Files")
                success_button.clicked.connect(self.show_successes)
                details_layout.addWidget(success_button)

            if report_data.get("failures"):
                failures_button = QPushButton("Show Failed Files")
                failures_button.clicked.connect(self.show_failures)
                details_layout.addWidget(failures_button)

            layout.addWidget(details_group)

        # OK button
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        button_box.accepted.connect(self.accept)
        layout.addWidget(button_box)

    def show_successes(self):
        """Show a list of successfully processed files."""
        self.show_details_list("Successful Files", self.report_data.get("success", []))

    def show_failures(self):
        """Show a list of failed files and the reasons."""
        failures = self.report_data.get("failures", [])
        formatted_failures = [f"{path}: {error}" for path, error in failures]
        self.show_details_list("Failed Files", formatted_failures)

    def show_details_list(self, title, items):
        """A helper dialog to display a list of items."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(500, 300)
        layout = QVBoxLayout(dialog)
        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setText("\n".join(map(str, items)))
        layout.addWidget(text_edit)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        button_box.accepted.connect(dialog.accept)
        layout.addWidget(button_box)
        dialog.exec()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.threadpool = QThreadPool()
        self.cancel_event = multiprocessing.Event()
        self.pause_event = multiprocessing.Event()
        self.setWindowTitle("PDF Rasterize")
        self.setGeometry(100, 100, 700, 800)
        self.create_ui()
        self.input_path_edit.setText(config_manager.get("input_path", ""))
        self.output_path_edit.setText(config_manager.get("output_path", ""))
        self.merge_dir_edit.setText(config_manager.get("merge_dir", ""))
        self.set_running_state(False, "split")
        self.set_running_state(False, "merge")

    def set_running_state(self, running: bool, operation_type: str):
        """Show or hide buttons based on the running state."""
        if operation_type == "split":
            self.split_start_btn.setHidden(running)
            self.split_pause_resume_btn.setHidden(not running)
            self.split_cancel_btn.setHidden(not running)
        elif operation_type == "merge":
            self.merge_start_btn.setHidden(running)
            self.merge_pause_resume_btn.setHidden(not running)
            self.merge_cancel_btn.setHidden(not running)

    def create_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        self.create_menu()

        self.tabs = QTabWidget()
        self.split_tab = QWidget()
        self.merge_tab = QWidget()
        self.tabs.addTab(self.split_tab, "Split")
        self.tabs.addTab(self.merge_tab, "Merge")
        main_layout.addWidget(self.tabs)

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
        browse_in_btn.clicked.connect(
            lambda: self.browse(
                self.input_path_edit,
                "Select Input PDF",
                QFileDialog.FileMode.ExistingFile,
            )
        )
        browse_out_btn = QPushButton("Browse")
        browse_out_btn.clicked.connect(
            lambda: self.browse(
                self.output_path_edit,
                "Select Output Folder",
                QFileDialog.FileMode.Directory,
            )
        )
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
        self.dpi_spinbox.setValue(config_manager.get("dpi", 300))
        self.workers_spinbox = QSpinBox()
        self.workers_spinbox.setRange(1, os.cpu_count() or 1)
        self.workers_spinbox.setValue(config_manager.get("workers", os.cpu_count() or 1))
        self.keep_originals_chk = QCheckBox("Keep original split PDFs")
        self.keep_originals_chk.setChecked(config_manager.get("keep_originals", False))
        self.flatten_output_chk = QCheckBox("Create flat output folder")
        self.flatten_output_chk.setChecked(config_manager.get("flatten_output", True))
        settings_layout.addWidget(QLabel("DPI:"), 0, 0)
        settings_layout.addWidget(self.dpi_spinbox, 0, 1)
        settings_layout.addWidget(QLabel("Workers:"), 0, 2)
        settings_layout.addWidget(self.workers_spinbox, 0, 3)
        settings_layout.addWidget(self.keep_originals_chk, 1, 0, 1, 2)
        settings_layout.addWidget(self.flatten_output_chk, 3, 0, 1, 4)

        self.rasterize_checkbox = QCheckBox("Rasterize after splitting")
        self.rasterize_checkbox.setChecked(config_manager.get("rasterize", True))
        # Place rasterize checkbox above the bookmark-level controls
        settings_layout.addWidget(self.rasterize_checkbox, 4, 0, 1, 2)

        self.limit_level_chk = QCheckBox("Limit splitting to bookmark level:")
        self.limit_level_chk.setChecked(True)
        self.max_level_spinbox = QSpinBox()
        self.max_level_spinbox.setRange(1, 20)
        self.max_level_spinbox.setValue(1)
        self.max_level_spinbox.setEnabled(True)
        self.limit_level_chk.toggled.connect(self.max_level_spinbox.setEnabled)
        # Move bookmark level controls down one row
        settings_layout.addWidget(self.limit_level_chk, 5, 0, 1, 2)
        settings_layout.addWidget(self.max_level_spinbox, 5, 2, 1, 2)
        layout.addWidget(settings_group)

        self.split_start_btn = QPushButton("Start Processing")
        self.split_start_btn.clicked.connect(self.start_split_process)
        self.split_pause_resume_btn = QPushButton("Pause")
        self.split_pause_resume_btn.clicked.connect(self.toggle_pause_resume)
        self.split_cancel_btn = QPushButton("Cancel")
        self.split_cancel_btn.clicked.connect(self.cancel_process)
        # Create button layout and add the control buttons
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.split_start_btn)
        button_layout.addWidget(self.split_pause_resume_btn)
        button_layout.addWidget(self.split_cancel_btn)
        layout.addLayout(button_layout)

    def create_merge_tab_ui(self):
        layout = QVBoxLayout(self.merge_tab)
        input_group = QGroupBox("Input Directory")
        input_layout = QGridLayout(input_group)
        self.merge_dir_edit = QLineEdit()
        browse_merge_btn = QPushButton("Browse")
        browse_merge_btn.clicked.connect(
            lambda: self.browse(
                self.merge_dir_edit,
                "Select Folder with Rasterized PDFs",
                QFileDialog.FileMode.Directory,
            )
        )
        input_layout.addWidget(QLabel("Folder:"), 0, 0)
        input_layout.addWidget(self.merge_dir_edit, 0, 1)
        input_layout.addWidget(browse_merge_btn, 0, 2)
        layout.addWidget(input_group)

        self.recreate_bookmarks_chk = QCheckBox("Re-create bookmarks from original PDF")
        self.recreate_bookmarks_chk.setChecked(True)
        layout.addWidget(self.recreate_bookmarks_chk)

        self.merge_start_btn = QPushButton("Start Merging")
        self.merge_start_btn.clicked.connect(self.start_merge_process)
        self.merge_pause_resume_btn = QPushButton("Pause")
        self.merge_pause_resume_btn.clicked.connect(self.toggle_pause_resume)
        self.merge_cancel_btn = QPushButton("Cancel")
        self.merge_cancel_btn.clicked.connect(self.cancel_process)
        # Create button layout for merge controls
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.merge_start_btn)
        button_layout.addWidget(self.merge_pause_resume_btn)
        button_layout.addWidget(self.merge_cancel_btn)
        layout.addLayout(button_layout)
        layout.addStretch()

    def browse(self, line_edit: QLineEdit, caption: str, mode: QFileDialog.FileMode):
        """Open a file or directory dialog and set the path to the line edit."""
        if mode == QFileDialog.FileMode.Directory:
            path = QFileDialog.getExistingDirectory(self, caption)
        else:
            path, _ = QFileDialog.getOpenFileName(self, caption, "", "PDF Files (*.pdf)")

        if path:
            line_edit.setText(path)

    def start_split_process(self):
        input_pdf = self.input_path_edit.text()
        output_dir = self.output_path_edit.text()

        if not input_pdf or not output_dir:
            self.show_error(
                "Input Error", "Please specify both input PDF and output folder."
            )
            return

        self.set_running_state(True, "split")
        self.split_pause_resume_btn.setText("Pause")
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.show()

        # Save settings for next time
        config_manager.set("input_path", input_pdf)
        config_manager.set("output_path", output_dir)
        config_manager.set("dpi", self.dpi_spinbox.value())
        config_manager.set("rasterize", self.rasterize_checkbox.isChecked())
        config_manager.set("keep_originals", self.keep_originals_chk.isChecked())
        config_manager.set("flatten_output", self.flatten_output_chk.isChecked())
        config_manager.set(
            "max_split_level",
            self.max_level_spinbox.value() if self.limit_level_chk.isChecked() else 0,
        )
        config_manager.save()

        self._start_worker(
            run_split_and_rasterize_wrapper,
            input_pdf,
            output_dir,
            self.dpi_spinbox.value(),
            self.keep_originals_chk.isChecked(),
            False,
            self.workers_spinbox.value(),
            self.flatten_output_chk.isChecked(),
            self.max_level_spinbox.value() if self.limit_level_chk.isChecked() else 0,
            self.rasterize_checkbox.isChecked(),
            config_manager.get("gs_path", "gs"),
            config_manager.get("magick_path", "magick"),
        )

    def start_merge_process(self):
        merge_dir = self.merge_dir_edit.text()
        if not merge_dir or not os.path.isdir(merge_dir):
            self.show_error("Input Error", "Please select a valid directory to merge.")
            return

        self.log_box.clear()
        self.update_log("Starting merge process...")
        self.progress_bar.show()
        self.set_running_state(True, "merge")
        self.merge_pause_resume_btn.setText("Pause")

        # Save settings for next time
        config_manager.set("merge_dir", merge_dir)
        config_manager.save()

        self._start_worker(
            run_merge_wrapper,
            merge_dir,
            self.merge_dir_edit.text(),
            self.recreate_bookmarks_chk.isChecked(),
            False,
            config_manager.get("gs_path", "gs"),
            config_manager.get("magick_path", "magick"),
        )

    def connect_worker_signals(self, worker):
        worker.signals.log.connect(self.update_log)
        worker.signals.progress.connect(self.update_progress)
        worker.signals.error.connect(self.show_error)
        worker.signals.summary.connect(self.show_summary)
        worker.signals.cleanup_ui.connect(self.cleanup_ui)

    def _start_worker(self, target_func, *args):
        self.cancel_event = multiprocessing.Event()
        self.pause_event = multiprocessing.Event()

        worker = Worker(
            target_func,
            self.cancel_event,
            self.pause_event,
            *args,
        )
        self.connect_worker_signals(worker)
        self.threadpool.start(worker)

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
        summary_dialog = SummaryDialog(report_data, self)
        summary_dialog.exec()

    def cleanup_ui(self, operation_type):
        self.progress_bar.hide()
        self.set_running_state(False, operation_type)
        self.cancel_event.clear()

    def toggle_pause_resume(self):
        current_tab_index = self.tabs.currentIndex()
        is_split_tab = current_tab_index == 0
        
        button = self.split_pause_resume_btn if is_split_tab else self.merge_pause_resume_btn
        
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.update_log("--- Resumed ---")
            button.setText("Pause")
        else:
            self.pause_event.set()
            self.update_log("--- Paused ---")
            button.setText("Resume")

    def cancel_process(self):
        if self.threadpool.activeThreadCount() > 0:
            self.update_log("--- Cancelling... ---")
            self.cancel_event.set()
            if not self.pause_event.is_set():
                self.pause_event.set()  # Unpause to allow cancellation to proceed


def main():
    app = QApplication(sys.argv)

    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
