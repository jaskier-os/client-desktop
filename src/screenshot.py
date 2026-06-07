"""
Standalone screenshot capture with flash overlay effect.

Usage:
    python screenshot.py [--screen N] [--output-dir DIR]

Captures all screens (or a specific one), saves PNGs, shows a glowing
orange border flash on each captured screen, and prints JSON to stdout.
"""

import sys
import os
import json
import argparse
import math
from datetime import datetime, timezone

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QPixmap


GBX_ORANGE = QColor(254, 128, 25, 180)
BORDER_THICKNESS = 80
ANIM_INTERVAL_MS = 16

# Timeline (ms)
FADE_IN_END = 150
HOLD_END = 350
FADE_OUT_END = 700


def ease_in(t):
    """Quadratic ease-in: accelerating from zero."""
    return t * t


def ease_out(t):
    """Quadratic ease-out: decelerating to zero."""
    return 1.0 - (1.0 - t) * (1.0 - t)


class FlashOverlay(QWidget):
    """Fullscreen transparent overlay that shows a glowing orange border."""

    def __init__(self, screen):
        super().__init__()
        self.screen_geo = screen.geometry()
        self.opacity = 0.0
        self.elapsed = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(self.screen_geo)

    def start(self):
        self.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(ANIM_INTERVAL_MS)

    def _tick(self):
        self.elapsed += ANIM_INTERVAL_MS

        if self.elapsed <= FADE_IN_END:
            t = self.elapsed / FADE_IN_END
            self.opacity = ease_in(t)
        elif self.elapsed <= HOLD_END:
            self.opacity = 1.0
        elif self.elapsed <= FADE_OUT_END:
            t = (self.elapsed - HOLD_END) / (FADE_OUT_END - HOLD_END)
            self.opacity = ease_out(1.0 - t)
        else:
            self.opacity = 0.0
            self.timer.stop()
            self.close()
            return

        self.update()

    def paintEvent(self, event):
        if self.opacity <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        t = BORDER_THICKNESS

        def make_color(alpha_factor):
            c = QColor(GBX_ORANGE)
            c.setAlpha(int(c.alpha() * self.opacity * alpha_factor))
            return c

        solid = make_color(1.0)
        transparent = make_color(0.0)

        # Top edge
        grad = QLinearGradient(0, 0, 0, t)
        grad.setColorAt(0, solid)
        grad.setColorAt(1, transparent)
        painter.fillRect(QRect(0, 0, w, t), grad)

        # Bottom edge
        grad = QLinearGradient(0, h, 0, h - t)
        grad.setColorAt(0, solid)
        grad.setColorAt(1, transparent)
        painter.fillRect(QRect(0, h - t, w, t), grad)

        # Left edge
        grad = QLinearGradient(0, 0, t, 0)
        grad.setColorAt(0, solid)
        grad.setColorAt(1, transparent)
        painter.fillRect(QRect(0, 0, t, h), grad)

        # Right edge
        grad = QLinearGradient(w, 0, w - t, 0)
        grad.setColorAt(0, solid)
        grad.setColorAt(1, transparent)
        painter.fillRect(QRect(w - t, 0, t, h), grad)

        painter.end()


def capture_screens(app, screen_index=None, output_dir=None):
    """Capture screenshots and show flash overlays.

    Returns list of saved file paths.
    """
    if output_dir is None:
        output_dir = '/tmp/repository-ai-screenshots'
    os.makedirs(output_dir, exist_ok=True)

    screens = app.screens()
    if not screens:
        print('No screens found', file=sys.stderr)
        sys.exit(1)

    if screen_index is not None:
        if screen_index < 0 or screen_index >= len(screens):
            print(f'Screen index {screen_index} out of range (0-{len(screens) - 1})', file=sys.stderr)
            sys.exit(1)
        targets = [(screen_index, screens[screen_index])]
    else:
        targets = list(enumerate(screens))

    # Capture BEFORE showing flash
    captures = []
    for idx, screen in targets:
        pixmap = screen.grabWindow(0)
        filepath = os.path.join(output_dir, f'screen_{idx}.png')
        pixmap.save(filepath, 'PNG')
        captures.append(filepath)

    # Show flash overlays
    overlays = []
    for _, screen in targets:
        overlay = FlashOverlay(screen)
        overlays.append(overlay)

    alive = {'count': len(overlays)}

    def on_overlay_destroyed():
        alive['count'] -= 1
        if alive['count'] <= 0:
            app.quit()

    for overlay in overlays:
        overlay.destroyed.connect(on_overlay_destroyed)
        overlay.start()

    # Safety timeout in case overlays don't close properly
    QTimer.singleShot(FADE_OUT_END + 300, app.quit)

    app.exec()
    return captures


def main():
    parser = argparse.ArgumentParser(description='Capture screenshots with flash effect')
    parser.add_argument('--screen', type=int, default=None, help='Capture only screen N (default: all)')
    parser.add_argument('--output-dir', type=str, default=None, help='Output directory (default: /tmp/repository-ai-screenshots/)')
    args = parser.parse_args()

    app = QApplication(sys.argv)

    paths = capture_screens(app, screen_index=args.screen, output_dir=args.output_dir)

    result = {
        'screenshots': paths,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    print(json.dumps(result))


if __name__ == '__main__':
    main()
