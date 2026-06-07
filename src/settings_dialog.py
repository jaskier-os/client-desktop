"""Settings dialog with Gruvbox dark styling."""

import logging
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QCheckBox, QPushButton,
)

from src import config

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
    """Config viewer dialog with Gruvbox dark theme.

    The orchestrator endpoint, model, TURN server, and optional TLS certificate
    are configured via environment variables / the `.env` file (see config.py).
    This dialog shows the active connection settings and manages launch-on-startup.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Settings")
        self.setFixedWidth(420)
        self.setStyleSheet(STYLESHEET)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        # Active orchestrator endpoint (read-only, configured via .env)
        layout.addWidget(QLabel("Orchestrator endpoint"))
        endpoint = QLabel(config.ORCHESTRATOR_WS_URL)
        endpoint.setWordWrap(True)
        layout.addWidget(endpoint)

        # Active model (read-only, configured via ORCHESTRATOR_MODEL)
        layout.addWidget(QLabel("Model"))
        model_name = MODEL_DISPLAY.get(config.ORCHESTRATOR_MODEL, config.ORCHESTRATOR_MODEL)
        layout.addWidget(QLabel(model_name))

        # TLS status (read-only)
        cert = config.resolve_tls_cert()
        tls_status = f"TLS CA: {cert}" if cert else "TLS: system trust / plain connection"
        layout.addWidget(QLabel(tls_status))

        layout.addWidget(QLabel("Edit the .env file to change connection settings."))

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

    def _save(self):
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
