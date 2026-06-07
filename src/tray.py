"""System tray icon with settings and quit actions."""

import logging
import os

from PyQt6.QtGui import QIcon, QAction
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

from src.settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


class TrayIcon:
    """Wraps QSystemTrayIcon with a settings/quit menu."""

    def __init__(self, app, listener):
        self._app = app
        self._listener = listener

        icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icon.png")
        self._tray = QSystemTrayIcon(QIcon(icon_path), app)

        menu = QMenu()

        settings_action = QAction("Settings", menu)
        settings_action.triggered.connect(self._open_settings)
        menu.addAction(settings_action)

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)

    def show(self):
        self._tray.show()

    def _open_settings(self):
        dialog = SettingsDialog()
        dialog.exec()

    def _quit(self):
        log.info("Quit requested from tray")
        self._listener.shutdown()
        self._app.quit()
