import sys
import os
import json
import time
import threading
import logging
from pathlib import Path

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QLineEdit,
                             QPushButton, QFileDialog, QVBoxLayout, QHBoxLayout,
                             QMessageBox, QTabWidget, QTextEdit, QCheckBox,
                             QSpinBox, QGroupBox, QProgressBar, QGridLayout,
                             QDialog, QDialogButtonBox)
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, QThreadPool
from PyQt6.QtGui import QFont, QAction, QIcon

try:
    import pdf_split_rasterize
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import pdf_split_rasterize

# --- Configuration Management ---
def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS attribute of sys
        base_path = sys._MEIPASS
    except Exception:
        # In development, the base path is the script's directory
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

CONFIG_FILE = resource_path("config.json")

CONFIG = {
    "gs_path": "gs",
    "magick_path": "magick"
}

def load_config():
    global CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                CONFIG.update(json.load(f))
        except (json.JSONDecodeError, TypeError):
            logging.warning("Could not read config.json. Using default paths.")

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(CONFIG, f, indent=4)

class WorkerSignals(QObject):
    """Defines signals available from a running worker thread."""
    finished = pyqtSignal()
    error = pyqtSignal(str, str)
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
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
            def emit(self, record):
                # Use the worker's signals object to emit logs
                self.signals.log.emit(self.format(record))

        # Each worker thread gets its own logger handler that emits signals
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        handler = QtLogHandler()
        handler.signals = self.signals # Give handler access to signals
        logger.handlers = [handler] # Replace existing handlers for this thread

        try:
            # Call the target function (e.g., run_split_and_rasterize_wrapper)
            # Pass all necessary objects to it.
            self.fn(*self.args, **self.kwargs, signals=self.signals,
                    cancel_event=self.cancel_event, pause_event=self.pause_event)
        except Exception as e:
            self.signals.error.emit("Worker Error", f"An unexpected error occurred in the worker: {e}")
        finally:
            logger.removeHandler(handler) # Clean up the handler
            self.signals.finished.emit() # Signal that the worker is done

def run_split_and_rasterize_wrapper(input_pdf, output_dir, dpi, keep_originals, dry_run, workers, config, flatten_output, max_split_level, signals, cancel_event, pause_event):
    """Wrapper to call the main script's entry point for splitting."""
    class Args: pass
    args = Args() # type: ignore
    args.input = Path(input_pdf) # type: ignore
    args.output = Path(output_dir) # type: ignore
    args.resolution = dpi # type: ignore
    args.keep_originals = keep_originals # type: ignore
    args.dry_run = dry_run # type: ignore
    args.workers = workers # type: ignore
    args.gs_path = config.get('gs_path', 'gs') # type: ignore
    args.magick_path = config.get('magick_path', 'magick') # type: ignore
    args.flatten_output = flatten_output # type: ignore
    args.max_split_level = max_split_level # type: ignore
    args.html_report = None # type: ignore

    _, _, report_data = pdf_split_rasterize.main_entry(args, lambda v, t: signals.progress.emit(v, t), cancel_event, pause_event)
    if not cancel_event.is_set():
        signals.summary.emit(report_data)
    signals.cleanup_ui.emit('split')

def run_merge_wrapper(merge_dir, recreate_bookmarks, config, signals, cancel_event, pause_event):
    """Wrapper to call the main script's entry point for merging."""
    class Args: pass
    args = Args() # type: ignore
    args.merge = Path(merge_dir) # type: ignore
    args.merge_output = None # type: ignore
    args.dry_run = False # type: ignore
    args.no_recreate_bookmarks = not recreate_bookmarks # type: ignore

    _, _, report_data = pdf_split_rasterize.main_entry(args, lambda v, t: signals.progress.emit(v, t), cancel_event, pause_event)
    if not cancel_event.is_set():
        signals.summary.emit(report_data)
    signals.cleanup_ui.emit('merge')

class SettingsWindow(QDialog):
    """Settings dialog to configure tool paths."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        layout = QGridLayout(self)

        self.gs_path_edit = QLineEdit(CONFIG.get("gs_path"))
        self.magick_path_edit = QLineEdit(CONFIG.get("magick_path"))

        layout.addWidget(QLabel("Ghostscript (gs) Path:"), 0, 0)
        layout.addWidget(self.gs_path_edit, 0, 1)
        gs_browse_btn = QPushButton("Browse")
        gs_browse_btn.clicked.connect(lambda: self.browse_file(self.gs_path_edit))
        layout.addWidget(gs_browse_btn, 0, 2)

        layout.addWidget(QLabel("ImageMagick (magick) Path:"), 1, 0)
        layout.addWidget(self.magick_path_edit, 1, 1)
        magick_browse_btn = QPushButton("Browse")
        magick_browse_btn.clicked.connect(lambda: self.browse_file(self.magick_path_edit))
        layout.addWidget(magick_browse_btn, 1, 2)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
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
        self.setWindowTitle("PDF Split & Rasterize")
        self.setGeometry(100, 100, 700, 650)
        self.threadpool = QThreadPool()
        self.cancel_event = None
        self.pause_event = None
        self.create_ui()
        load_config()

    def create_ui(self):
        self.create_menu()
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        tabs = QTabWidget()
        self.split_tab, self.merge_tab = QWidget(), QWidget()
        tabs.addTab(self.split_tab, "Split & Rasterize"), tabs.addTab(self.merge_tab, "Merge PDFs") # type: ignore
        main_layout.addWidget(tabs)

        self.create_split_tab_ui()
        self.create_merge_tab_ui()

        self.progress_bar = QProgressBar()
        self.progress_bar.hide()
        main_layout.addWidget(self.progress_bar)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 10))
        main_layout.addWidget(self.log_box)

    def create_menu(self):
        file_menu = self.menuBar().addMenu("&File") # type: ignore
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(lambda: SettingsWindow(self).exec())
        file_menu.addAction(settings_action) # type: ignore
        file_menu.addSeparator() # type: ignore
        exit_action = QAction("Exit", self)
        file_menu.addAction(exit_action) # type: ignore

        exit_action.triggered.connect(self.close)

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
        self.dpi_spinbox.setValue(300)
        self.workers_spinbox = QSpinBox()
        self.workers_spinbox.setRange(1, os.cpu_count() or 1)
        self.workers_spinbox.setValue(os.cpu_count() or 1)
        self.keep_originals_chk = QCheckBox("Keep original split PDFs")
        self.dry_run_chk = QCheckBox("Dry Run (show what would happen)")
        self.flatten_output_chk = QCheckBox("Create flat output folder")
        settings_layout.addWidget(QLabel("DPI:"), 0, 0)
        settings_layout.addWidget(self.dpi_spinbox, 0, 1)
        settings_layout.addWidget(QLabel("Workers:"), 0, 2)
        settings_layout.addWidget(self.workers_spinbox, 0, 3)
        settings_layout.addWidget(self.keep_originals_chk, 1, 0, 1, 2)
        settings_layout.addWidget(self.dry_run_chk, 2, 0, 1, 2)
        settings_layout.addWidget(self.flatten_output_chk, 3, 0, 1, 4)

        self.limit_level_chk = QCheckBox("Limit splitting to bookmark level:")
        self.max_level_spinbox = QSpinBox()
        self.max_level_spinbox.setRange(1, 20)
        self.max_level_spinbox.setValue(3)
        self.max_level_spinbox.setEnabled(False)
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
        path, _ = QFileDialog.getOpenFileName(self, "Select Input PDF", "", "PDF Files (*.pdf)")
        if path: self.input_path_edit.setText(path)

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if path: self.output_path_edit.setText(path)

    def browse_merge_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select Folder with Rasterized PDFs")
        if path: self.merge_dir_edit.setText(path)

    def start_split_process(self):
        input_pdf = self.input_path_edit.text()
        output_dir = self.output_path_edit.text()
        if not input_pdf or not os.path.isfile(input_pdf):
            self.show_error("Input Error", "Please select a valid input PDF file.")
            return
        if not output_dir or not os.path.isdir(output_dir):
            self.show_error("Output Error", "Please select a valid output folder.")
            return

        self.log_box.clear()
        self.update_log("Starting split & rasterize process...")
        self.progress_bar.show()
        self.split_start_btn.hide()
        self.split_pause_resume_btn.show()
        self.split_cancel_btn.show()

        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()

        max_split_level = 0
        if self.limit_level_chk.isChecked():
            max_split_level = self.max_level_spinbox.value()

        worker = Worker(run_split_and_rasterize_wrapper, self.cancel_event, self.pause_event,
                        input_pdf, output_dir, self.dpi_spinbox.value(),
                        self.keep_originals_chk.isChecked(), self.dry_run_chk.isChecked(),
                        self.workers_spinbox.value(), CONFIG, self.flatten_output_chk.isChecked(),
                        max_split_level) # type: ignore
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
        self.pause_event.set()

        worker = Worker(run_merge_wrapper, self.cancel_event, self.pause_event,
                        merge_dir, self.recreate_bookmarks_chk.isChecked(), CONFIG) # type: ignore
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

    def update_progress(self, value, total):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(value)

    def show_error(self, title, message):
        QMessageBox.critical(self, title, message)

    def show_summary(self, report_data):
        end_time = time.time()
        duration = end_time - report_data.get('start_time', end_time)
        summary_lines = [
            f"\n{'='*30}", "PROCESS SUMMARY", f"{'='*30}",
            f"Operation: {report_data.get('operation_type', 'N/A').replace('_', ' ').title()}",
            f"Duration: {duration:.2f} seconds",
            f"Total files processed: {report_data.get('total_files_processed', 0)}",
            f"Successful: {len(report_data.get('success', []))}",
            f"Failed: {len(report_data.get('failures', []))}",
            f"{'='*30}\n"
        ]
        self.update_log("\n".join(summary_lines))

    def cleanup_ui(self, operation_type):
        self.progress_bar.hide()
        if operation_type == 'split':
            self.split_start_btn.show()
            self.split_pause_resume_btn.hide()
            self.split_cancel_btn.hide()
        elif operation_type == 'merge':
            self.merge_start_btn.show()
            self.merge_pause_resume_btn.hide()
            self.merge_cancel_btn.hide()

    def toggle_pause_resume(self):
        if self.pause_event:
            if self.pause_event.is_set():
                self.pause_event.clear()
                self.split_pause_resume_btn.setText("Resume")
                self.merge_pause_resume_btn.setText("Resume")
                self.update_log("--- Paused ---")
            else:
                self.pause_event.set()
                self.split_pause_resume_btn.setText("Pause")
                self.merge_pause_resume_btn.setText("Pause")
                self.update_log("--- Resumed ---")

    def cancel_process(self):
        if self.cancel_event:
            self.update_log("--- Cancelling... ---")
            self.cancel_event.set()
            if self.pause_event and not self.pause_event.is_set():
                self.pause_event.set() # Unpause to allow cancellation to proceed

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
