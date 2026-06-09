"""Settings dialog with Gruvbox dark styling."""

import logging
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QComboBox, QCheckBox, QPushButton,
)

log = logging.getLogger(__name__)

AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "repository-listener.desktop")
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PYTHON = os.path.join(_BASE_DIR, "venv", "bin", "python3")
_MAIN_PY = os.path.join(_BASE_DIR, "main.py")
_EXEC = f"{_VENV_PYTHON} {_MAIN_PY}" if os.path.isfile(_VENV_PYTHON) else f"python3 {_MAIN_PY}"

DESKTOP_ENTRY = f"""\
[Desktop Entry]
Type=Application
Name=Repository Listener
Exec={_EXEC}
Hidden=false
X-GNOME-Autostart-enabled=true
"""

MODEL_OPTIONS = ["sonnet", "opus", "haiku"]
MODEL_DISPLAY = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}

STYLESHEET = """
QDialog {
    background-color: #282828;
    color: #ebdbb2;
}
QLabel {
    color: #ebdbb2;
    font-size: 13px;
}
QLineEdit, QComboBox {
    background-color: #3c3836;
    color: #ebdbb2;
    border: 1px solid #504945;
    border-radius: 4px;
    padding: 6px 8px;
    font-size: 13px;
    selection-background-color: #fe8019;
    selection-color: #282828;
}
QLineEdit:focus, QComboBox:focus {
    border-color: #fe8019;
}
QComboBox::drop-down {
    border: none;
    padding-right: 8px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #ebdbb2;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: #3c3836;
    color: #ebdbb2;
    border: 1px solid #504945;
    selection-background-color: #fe8019;
    selection-color: #282828;
}
QCheckBox {
    color: #ebdbb2;
    font-size: 13px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #504945;
    border-radius: 3px;
    background-color: #3c3836;
}
QCheckBox::indicator:checked {
    background-color: #fe8019;
    border-color: #fe8019;
}
QPushButton {
    font-size: 13px;
    padding: 7px 20px;
    border-radius: 4px;
    border: 1px solid #504945;
}
QPushButton#save_btn {
    background-color: #fe8019;
    color: #282828;
    border-color: #fe8019;
    font-weight: bold;
}
QPushButton#save_btn:hover {
    background-color: #d96b0a;
}
QPushButton#cancel_btn {
    background-color: #3c3836;
    color: #ebdbb2;
}
QPushButton#cancel_btn:hover {
    background-color: #504945;
}
"""


class SettingsDialog(QDialog):
    """Config editor dialog with Gruvbox dark theme."""

    def __init__(self, config_path, config, parent=None):
        super().__init__(parent)
        self._config_path = config_path
        self._config = config

        self.setWindowTitle("Settings")
        self.setFixedWidth(420)
        self.setStyleSheet(STYLESHEET)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        # Orchestrator URL
        layout.addWidget(QLabel("Orchestrator URL"))
        self._url_input = QLineEdit()
        self._url_input.setText(self._config.get("orchestrator", "api_url", fallback=""))
        layout.addWidget(self._url_input)

        # Remote transcription
        self._remote_check = QCheckBox("Use remote transcription service")
        self._remote_check.setChecked(self._config.getboolean("transcription", "remote", fallback=False))
        self._remote_check.toggled.connect(self._on_remote_toggled)
        layout.addWidget(self._remote_check)

        # Transcription URL
        layout.addWidget(QLabel("Transcription URL"))
        self._transcription_input = QLineEdit()
        self._transcription_input.setText(self._config.get("transcription", "url", fallback=""))
        self._transcription_input.setEnabled(self._remote_check.isChecked())
        layout.addWidget(self._transcription_input)

        # API Key
        layout.addWidget(QLabel("API Key"))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setText(self._config.get("orchestrator", "api_key", fallback=""))
        layout.addWidget(self._key_input)

        # Model
        layout.addWidget(QLabel("Model"))
        self._model_combo = QComboBox()
        for key in MODEL_OPTIONS:
            self._model_combo.addItem(MODEL_DISPLAY[key], key)
        current_model = self._config.get("orchestrator", "model", fallback="sonnet")
        idx = MODEL_OPTIONS.index(current_model) if current_model in MODEL_OPTIONS else 0
        self._model_combo.setCurrentIndex(idx)
        layout.addWidget(self._model_combo)

        # Launch on startup
        self._autostart_check = QCheckBox("Launch on startup")
        self._autostart_check.setChecked(os.path.isfile(AUTOSTART_FILE))
        layout.addWidget(self._autostart_check)

        layout.addSpacing(8)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("save_btn")
        save_btn.clicked.connect(self._save)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _on_remote_toggled(self, checked):
        self._transcription_input.setEnabled(checked)

    def _save(self):
        # Write values to config
        if not self._config.has_section("orchestrator"):
            self._config.add_section("orchestrator")
        self._config.set("orchestrator", "api_url", self._url_input.text())
        self._config.set("orchestrator", "api_key", self._key_input.text())
        self._config.set("orchestrator", "model", self._model_combo.currentData())

        if not self._config.has_section("transcription"):
            self._config.add_section("transcription")
        self._config.set("transcription", "remote", str(self._remote_check.isChecked()).lower())
        self._config.set("transcription", "url", self._transcription_input.text())

        with open(self._config_path, "w", encoding="utf-8") as f:
            self._config.write(f)
        log.info("Settings saved to %s", self._config_path)

        # Autostart
        if self._autostart_check.isChecked():
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
                f.write(DESKTOP_ENTRY)
            log.info("Autostart file created: %s", AUTOSTART_FILE)
        else:
            if os.path.isfile(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
                log.info("Autostart file removed: %s", AUTOSTART_FILE)

        self.accept()
