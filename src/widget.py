"""PyQt6 overlay widget -- Gruvbox Dark Medium palette, elegant design."""

import logging
import math
import os
import time

import numpy as np
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, pyqtSlot, QRectF, QUrl,
)
from PyQt6.QtGui import (
    QPainter, QColor, QLinearGradient, QPen, QFont, QFontMetrics,
    QPainterPath, QBrush, QRadialGradient,
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtWidgets import QWidget, QApplication, QHBoxLayout, QVBoxLayout

log = logging.getLogger(__name__)

# -- Gruvbox Dark Medium palette --
GBX_BG       = QColor(40, 40, 40)       # #282828
GBX_BG1      = QColor(60, 56, 54)       # #3c3836
GBX_BG2      = QColor(80, 73, 69)       # #504945
GBX_FG       = QColor(235, 219, 178)    # #ebdbb2
GBX_FG4      = QColor(168, 153, 132)    # #a89984
GBX_AQUA     = QColor(142, 192, 124)    # #8ec07c
GBX_BLUE     = QColor(131, 165, 152)    # #83a598
GBX_GREEN    = QColor(184, 187, 38)     # #b8bb26
GBX_ORANGE   = QColor(254, 128, 25)     # #fe8019
GBX_YELLOW   = QColor(250, 189, 47)     # #fabd2f
GBX_RED      = QColor(251, 73, 52)      # #fb4934
GBX_GRAY     = QColor(146, 131, 116)    # #928374

# -- Layout constants --
NUM_BARS = 28
BAR_WIDTH = 3
BAR_GAP = 2
BAR_MAX_HEIGHT = 36
PILL_HEIGHT = 48
PILL_RADIUS = 24
BORDER_WIDTH = 1
TEXT_MAX_WIDTH = 600
TEXT_LINE_HEIGHT = 20
TEXT_MAX_LINES = 4
GLOW_MARGIN = 55


def _ease_out(t):
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _ease_in(t):
    t = max(0.0, min(1.0, t))
    return t ** 3


def _phase(elapsed, start, end):
    if elapsed <= start:
        return 0.0
    if elapsed >= end:
        return 1.0
    return (elapsed - start) / (end - start)


class SpectrogramWidget(QWidget):
    """Draws mirrored mel-scale frequency bars expanding up and down from center."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bars = np.zeros(NUM_BARS, dtype=np.float64)
        total_w = NUM_BARS * (BAR_WIDTH + BAR_GAP) - BAR_GAP
        self.setFixedSize(total_w, BAR_MAX_HEIGHT)
        self._opacity = 1.0

    def set_opacity(self, val):
        self._opacity = val
        self.update()

    def update_bars(self, bars):
        self._bars = np.clip(bars, 0.0, 1.0)
        self.update()

    def paintEvent(self, event):
        if self._opacity < 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._opacity)

        mid = BAR_MAX_HEIGHT / 2.0

        for i, val in enumerate(self._bars):
            half_h = max(1, int(val * mid))
            x = i * (BAR_WIDTH + BAR_GAP)

            # Top half: grows upward from center
            top_y = mid - half_h
            grad_up = QLinearGradient(x, mid, x, top_y)
            grad_up.setColorAt(0.0, GBX_BG2)
            grad_up.setColorAt(1.0, GBX_ORANGE)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad_up))
            p.drawRoundedRect(QRectF(x, top_y, BAR_WIDTH, half_h), 1.5, 1.5)

            # Bottom half: grows downward from center (mirrored)
            grad_down = QLinearGradient(x, mid, x, mid + half_h)
            grad_down.setColorAt(0.0, GBX_BG2)
            grad_down.setColorAt(1.0, GBX_ORANGE)
            p.setBrush(QBrush(grad_down))
            p.drawRoundedRect(QRectF(x, mid, BAR_WIDTH, half_h), 1.5, 1.5)

        p.end()


class TranscriptionLabel(QWidget):
    """White centered text with shadow, multi-line word wrap."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._words = []
        self._font = QFont("Sans", 11)
        self._font.setWeight(QFont.Weight.DemiBold)
        self.setFixedWidth(TEXT_MAX_WIDTH)
        self.setFixedHeight(TEXT_LINE_HEIGHT * TEXT_MAX_LINES)

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._tick_fade)

    def set_text(self, text):
        new_words = text.split() if text else []
        existing = [w["word"] for w in self._words]
        if new_words != existing:
            result = []
            for w in new_words:
                found = False
                for ew in self._words:
                    if ew["word"] == w and not found:
                        result.append(ew)
                        found = True
                if not found:
                    result.append({"word": w, "opacity": 0.0})
            self._words = result
            if not self._fade_timer.isActive():
                self._fade_timer.start()
        self.update()

    def clear_text(self):
        self._words = []
        self._fade_timer.stop()
        self.update()

    def _tick_fade(self):
        all_visible = True
        for w in self._words:
            if w["opacity"] < 1.0:
                w["opacity"] = min(1.0, w["opacity"] + 0.07)
                all_visible = False
        if all_visible:
            self._fade_timer.stop()
        self.update()

    def _layout_lines(self, fm):
        """Word-wrap into centered lines that fit within TEXT_MAX_WIDTH."""
        lines = []
        current_line = []
        current_w = 0
        space_w = fm.horizontalAdvance(" ")

        for w in self._words:
            word_w = fm.horizontalAdvance(w["word"])
            needed = (current_w + space_w + word_w) if current_line else word_w
            if needed > TEXT_MAX_WIDTH and current_line:
                lines.append(current_line)
                current_line = [w]
                current_w = word_w
            else:
                current_line.append(w)
                current_w = needed
        if current_line:
            lines.append(current_line)

        return lines[-TEXT_MAX_LINES:] if len(lines) > TEXT_MAX_LINES else lines

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self._font)

        fm = QFontMetrics(self._font)
        lines = self._layout_lines(fm)
        space_w = fm.horizontalAdvance(" ")

        shadow_color = QColor(0, 0, 0)
        text_color = QColor(255, 255, 255)

        # Draw from bottom up so latest text is closest to pill
        base_y = self.height() - 4

        for line_idx, line_words in enumerate(reversed(lines)):
            y = base_y - line_idx * TEXT_LINE_HEIGHT

            # Compute line width for centering
            line_w = sum(fm.horizontalAdvance(w["word"]) for w in line_words)
            line_w += space_w * max(0, len(line_words) - 1)
            x = (self.width() - line_w) // 2

            for w in line_words:
                alpha = w["opacity"]

                shadow_color.setAlphaF(alpha * 0.7)
                p.setPen(shadow_color)
                for dx, dy in ((1, 1), (1, 2), (2, 1)):
                    p.drawText(x + dx, y + dy, w["word"])

                text_color.setAlphaF(alpha)
                p.setPen(text_color)
                p.drawText(x, y, w["word"])

                x += fm.horizontalAdvance(w["word"]) + space_w

        p.end()


class MicIndicator(QWidget):
    """Pulsing mic circle indicator drawn with QPainter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self._opacity = 1.0
        self._glow = 0.0
        self._growing = True

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def start_pulse(self):
        self._timer.start()

    def stop_pulse(self):
        self._timer.stop()
        self._glow = 0.0
        self.update()

    def set_opacity(self, val):
        self._opacity = val
        self.update()

    def _tick(self):
        if self._growing:
            self._glow += 0.04
            if self._glow >= 1.0:
                self._glow = 1.0
                self._growing = False
        else:
            self._glow -= 0.04
            if self._glow <= 0.0:
                self._glow = 0.0
                self._growing = True
        self.update()

    def paintEvent(self, event):
        if self._opacity < 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._opacity)

        cx, cy, r = 14, 14, 10

        # Outer glow ring -- warm cream
        glow_color = QColor(GBX_FG)
        glow_color.setAlphaF(0.25 + 0.35 * self._glow)
        p.setPen(QPen(glow_color, 2.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # Inner filled circle -- light warm orange
        inner_r = 6
        fill = QColor(GBX_YELLOW)
        fill.setAlphaF(0.7 + 0.3 * self._glow)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawEllipse(cx - inner_r, cy - inner_r, inner_r * 2, inner_r * 2)

        # Bright center dot
        p.setBrush(QColor(255, 255, 255, 160 + int(95 * self._glow)))
        p.drawEllipse(cx - 2, cy - 2, 4, 4)

        p.end()


class ListenerWidget(QWidget):
    """Gruvbox-themed pill overlay with spectrogram, mic indicator, and transcription."""

    sig_show_widget = pyqtSignal()
    sig_hide_widget = pyqtSignal()
    sig_start_finishing = pyqtSignal()
    sig_loading_done = pyqtSignal()
    sig_update_spectrogram = pyqtSignal(object)
    sig_update_transcription = pyqtSignal(str)
    sig_start_responding = pyqtSignal()
    sig_update_tts_level = pyqtSignal(float)
    sig_cancel_clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(0.0)

        # Transcription (above pill)
        self._transcription = TranscriptionLabel()

        # Mic indicator
        self._mic = MicIndicator()

        # Spectrogram bars
        self._spectrogram = SpectrogramWidget()

        # Pill layout
        pill_layout = QHBoxLayout()
        pill_layout.setContentsMargins(14, 0, 18, 0)
        pill_layout.setSpacing(10)
        pill_layout.addWidget(self._mic, alignment=Qt.AlignmentFlag.AlignVCenter)
        pill_layout.addWidget(self._spectrogram, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._pill = QWidget()
        self._pill.setLayout(pill_layout)
        self._pill.setFixedHeight(PILL_HEIGHT)

        # Main layout -- margins give room for glow shadow to render
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 0, 30, GLOW_MARGIN)
        layout.setSpacing(14)
        layout.addWidget(self._transcription, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._pill, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.setLayout(layout)

        self.adjustSize()
        self._position_on_screen()

        # Widget state: 'hidden', 'loading', 'expanding', 'listening', 'finishing'
        self._widget_state = 'hidden'
        self._pill_width_ratio = 1.0   # 0.0 = circle, 1.0 = full pill
        self._loading_angle = 0.0

        # Staggered entrance/exit animation
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._tick_anim)
        self._anim_t0 = 0.0
        self._anim_mode = None

        # Loading spinner
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(16)
        self._loading_timer.timeout.connect(self._tick_loading)

        # Activation sound
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        sound_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "assets", "activate.mp3")
        self._player.setSource(QUrl.fromLocalFile(sound_path))

        # Pulsating shadow behind pill
        self._shadow_phase = 0.0
        self._shadow_timer = QTimer(self)
        self._shadow_timer.setInterval(30)
        self._shadow_timer.timeout.connect(self._tick_shadow)

        # Cancel button state
        self._cancel_visible = False
        self._cancel_opacity = 0.0
        self._cancel_fade_timer = QTimer(self)
        self._cancel_fade_timer.setInterval(16)
        self._cancel_fade_timer.timeout.connect(self._tick_cancel_fade)
        self._cancel_hide_timer = QTimer(self)
        self._cancel_hide_timer.setSingleShot(True)
        self._cancel_hide_timer.setInterval(3000)
        self._cancel_hide_timer.timeout.connect(self._hide_cancel_button)

        # TTS circle spectrogram state
        self._tts_level = 0.0
        self._tts_target = 0.0
        self._tts_phase = 0.0
        self._tts_timer = QTimer(self)
        self._tts_timer.setInterval(30)
        self._tts_timer.timeout.connect(self._tick_tts)

        # Signals
        self.sig_show_widget.connect(self._do_show)
        self.sig_hide_widget.connect(self._do_hide)
        self.sig_start_finishing.connect(self._do_start_finishing)
        self.sig_update_spectrogram.connect(self._do_update_spectrogram)
        self.sig_update_transcription.connect(self._do_update_transcription)
        self.sig_start_responding.connect(self._do_start_responding)
        self.sig_update_tts_level.connect(self._do_update_tts_level)

    def _position_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            w = max(self.sizeHint().width(), TEXT_MAX_WIDTH + 60)
            h = self.sizeHint().height()
            x = geo.x() + (geo.width() - w) // 2
            y = geo.y() + geo.height() - h - 25
            self.setGeometry(x, y, w, h)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        pill_rect = self._pill.geometry()
        full_rect = QRectF(pill_rect).adjusted(-1, -1, 1, 1)

        # Morph between circle (ratio=0) and full pill (ratio=1)
        if self._pill_width_ratio < 0.999:
            circle_w = full_rect.height()
            full_w = full_rect.width()
            current_w = circle_w + (full_w - circle_w) * self._pill_width_ratio
            center_x = full_rect.center().x()
            rect = QRectF(center_x - current_w / 2, full_rect.y(),
                          current_w, full_rect.height())
        else:
            rect = full_rect

        path = QPainterPath()
        path.addRoundedRect(rect, PILL_RADIUS, PILL_RADIUS)

        # Pulsating warm brown glow behind pill
        pulse = 0.5 + 0.5 * math.sin(self._shadow_phase)
        cx = rect.center().x()
        cy = rect.center().y()
        base_r = rect.height() * 1.4
        glow_r = base_r * (1.0 + 0.15 * pulse)
        core_alpha = int(70 + 50 * pulse)
        mid_alpha = int(35 + 30 * pulse)
        glow = QRadialGradient(cx, cy, glow_r)
        glow.setColorAt(0.0, QColor(80, 73, 69, core_alpha))
        glow.setColorAt(0.5, QColor(60, 56, 54, mid_alpha))
        glow.setColorAt(1.0, QColor(40, 40, 40, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        glow_rect = QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2)
        p.drawEllipse(glow_rect)

        # Fill: gruvbox bg
        bg = QColor(GBX_BG)
        bg.setAlpha(220)
        p.setBrush(bg)
        p.drawPath(path)

        # Subtle warm border
        border = QColor(GBX_BG2)
        border.setAlpha(160)
        p.setPen(QPen(border, BORDER_WIDTH))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        # Loading spinner inside circle
        if self._widget_state in ('loading', 'finishing'):
            spinner_r = (PILL_HEIGHT - 18) / 2
            sr = QRectF(cx - spinner_r, cy - spinner_r,
                        spinner_r * 2, spinner_r * 2)

            # Dim track ring
            track = QColor(GBX_BG2)
            track.setAlpha(100)
            p.setPen(QPen(track, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(sr)

            # Spinning arc
            arc_pen = QPen(QColor(GBX_FG), 2.5)
            arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(arc_pen)
            start = int(self._loading_angle * 16)
            p.drawArc(sr, start, 100 * 16)

        # TTS circle spectrogram (radial bars wobbling to audio)
        elif self._widget_state == 'responding':
            num_bars = 24
            inner_r = 8.0
            max_bar_h = 13.0
            for i in range(num_bars):
                angle = 2.0 * math.pi * i / num_bars
                # Multi-frequency variation for organic movement
                v1 = 0.3 + 0.7 * abs(math.sin(angle * 2 + self._tts_phase * 1.2))
                v2 = 0.5 + 0.5 * abs(math.sin(angle * 5 + self._tts_phase * 0.7))
                variation = v1 * v2
                # Audio level drives bar height; always show a gentle base
                level = 0.2 + 0.8 * self._tts_level
                h = max_bar_h * level * variation
                h = max(1.5, h)
                cos_a = math.cos(angle)
                sin_a = math.sin(angle)
                x1 = cx + inner_r * cos_a
                y1 = cy + inner_r * sin_a
                x2 = cx + (inner_r + h) * cos_a
                y2 = cy + (inner_r + h) * sin_a

                bar_alpha = 0.3 + 0.7 * min(1.0, h / max_bar_h)
                bar_color = QColor(GBX_ORANGE)
                bar_color.setAlphaF(bar_alpha)
                p.setPen(QPen(bar_color, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(int(x1), int(y1), int(x2), int(y2))

        # Cancel X button overlay (tap to reveal in all active states)
        if self._widget_state in ('listening', 'finishing', 'responding') and self._cancel_visible:
            self._draw_x_button(p, cx, cy, opacity=self._cancel_opacity)

        p.end()

    @pyqtSlot()
    def _do_show(self):
        # Keep previous transcription text visible during loading
        self.setWindowOpacity(0.0)
        self._mic.set_opacity(0.0)
        self._spectrogram.set_opacity(0.0)
        self._pill_width_ratio = 0.0
        self._loading_angle = 0.0
        self._widget_state = 'loading'
        self.show()
        self._position_on_screen()
        self._anim_mode = 'show'
        self._anim_t0 = time.monotonic()
        self._anim_timer.start()
        self._loading_timer.start()
        self._shadow_timer.start()
        self._player.setPosition(0)
        self._player.play()
        QTimer.singleShot(800, self._do_start_listening)
        log.debug("Widget shown (loading)")

    def _do_start_listening(self):
        if self._widget_state != 'loading':
            return
        self._widget_state = 'expanding'
        self._loading_timer.stop()
        self._transcription.clear_text()
        self.sig_loading_done.emit()
        self._anim_mode = 'expand'
        self._anim_t0 = time.monotonic()
        if not self._anim_timer.isActive():
            self._anim_timer.start()
        self._mic.start_pulse()
        log.debug("Transitioning to listening")

    @pyqtSlot()
    def _do_start_finishing(self):
        self._mic.stop_pulse()
        self._anim_mode = 'contract'
        self._anim_t0 = time.monotonic()
        if not self._anim_timer.isActive():
            self._anim_timer.start()
        log.debug("Widget finishing")

    @pyqtSlot()
    def _do_hide(self):
        self._loading_timer.stop()
        self._tts_timer.stop()
        self._mic.stop_pulse()
        if self._widget_state in ('finishing', 'responding'):
            self._anim_mode = 'fade_out'
        else:
            self._anim_mode = 'hide'
        self._anim_t0 = time.monotonic()
        self._anim_timer.start()
        log.debug("Widget hiding")

    def _on_fade_out_done(self):
        self.hide()
        self._shadow_timer.stop()
        self._tts_timer.stop()
        self._hide_cancel_button()
        self._widget_state = 'hidden'
        self._pill_width_ratio = 1.0
        self._transcription.clear_text()
        self._spectrogram.update_bars(np.zeros(NUM_BARS))
        log.debug("Widget hidden")

    def _tick_anim(self):
        elapsed = time.monotonic() - self._anim_t0

        if self._anim_mode == 'show':
            # Fade in window with circle shape (no mic/spec yet)
            pill = _ease_out(_phase(elapsed, 0.0, 0.35))
            self.setWindowOpacity(pill)
            self.update()
            if elapsed >= 0.35:
                self.setWindowOpacity(1.0)
                self._anim_timer.stop()
                self._anim_mode = None

        elif self._anim_mode == 'expand':
            # Circle morphs to pill, mic and spectrogram fade in
            self._pill_width_ratio = _ease_out(_phase(elapsed, 0.0, 0.40))
            mic = _ease_out(_phase(elapsed, 0.15, 0.38))
            spec = _ease_out(_phase(elapsed, 0.25, 0.55))
            self._mic.set_opacity(mic)
            self._spectrogram.set_opacity(spec)
            self.update()
            if elapsed >= 0.55:
                self._pill_width_ratio = 1.0
                self._mic.set_opacity(1.0)
                self._spectrogram.set_opacity(1.0)
                self._widget_state = 'listening'
                self._anim_timer.stop()
                self._anim_mode = None

        elif self._anim_mode == 'contract':
            # Pill contracts to circle, mic/spec fade out, then show spinner
            spec = 1.0 - _ease_in(_phase(elapsed, 0.0, 0.15))
            mic = 1.0 - _ease_in(_phase(elapsed, 0.05, 0.20))
            self._pill_width_ratio = 1.0 - _ease_in(_phase(elapsed, 0.0, 0.30))
            self._mic.set_opacity(mic)
            self._spectrogram.set_opacity(spec)
            self.update()
            if elapsed >= 0.30:
                self._pill_width_ratio = 0.0
                self._mic.set_opacity(0.0)
                self._spectrogram.set_opacity(0.0)
                self._widget_state = 'finishing'
                self._loading_timer.start()
                self._anim_timer.stop()
                self._anim_mode = None

        elif self._anim_mode == 'fade_out':
            # Already a circle, just fade out
            pill = 1.0 - _ease_in(_phase(elapsed, 0.0, 0.35))
            self.setWindowOpacity(pill)
            self.update()
            if elapsed >= 0.35:
                self.setWindowOpacity(0.0)
                self._anim_timer.stop()
                self._anim_mode = None
                self._on_fade_out_done()

        elif self._anim_mode == 'hide':
            # Full hide: contract pill + fade out
            spec = 1.0 - _ease_in(_phase(elapsed, 0.0, 0.12))
            mic = 1.0 - _ease_in(_phase(elapsed, 0.05, 0.18))
            self._pill_width_ratio = 1.0 - _ease_in(_phase(elapsed, 0.0, 0.25))
            pill = 1.0 - _ease_in(_phase(elapsed, 0.15, 0.45))
            self.setWindowOpacity(pill)
            self._mic.set_opacity(mic)
            self._spectrogram.set_opacity(spec)
            self.update()
            if elapsed >= 0.45:
                self.setWindowOpacity(0.0)
                self._mic.set_opacity(0.0)
                self._spectrogram.set_opacity(0.0)
                self._pill_width_ratio = 0.0
                self._anim_timer.stop()
                self._anim_mode = None
                self._on_fade_out_done()

    @pyqtSlot()
    def _do_start_responding(self):
        """Transition from finishing spinner to TTS circle spectrogram."""
        self._loading_timer.stop()
        self._tts_level = 0.0
        self._tts_target = 0.0
        self._tts_phase = 0.0
        self._widget_state = 'responding'
        self._tts_timer.start()
        self._transcription.clear_text()
        log.info("Widget state -> responding (TTS circle spectrogram)")

    @pyqtSlot(float)
    def _do_update_tts_level(self, level):
        """Update target TTS amplitude level."""
        self._tts_target = level
        if level > 0.01:
            log.debug("Widget TTS target level: %.3f (current: %.3f, state: %s)",
                       level, self._tts_level, self._widget_state)

    def _tick_tts(self):
        """Animate TTS circle spectrogram."""
        self._tts_level += (self._tts_target - self._tts_level) * 0.3
        self._tts_phase += 0.12
        if self._tts_phase > 2.0 * math.pi * 100:
            self._tts_phase -= 2.0 * math.pi * 100
        self.update()

    def _tick_loading(self):
        self._loading_angle += 5.0
        if self._loading_angle >= 360.0:
            self._loading_angle -= 360.0
        self.update()

    def _tick_shadow(self):
        self._shadow_phase += 0.04
        if self._shadow_phase > 2.0 * math.pi:
            self._shadow_phase -= 2.0 * math.pi
        self.update()

    def _draw_x_button(self, p, btn_cx, btn_cy, radius=10, opacity=1.0):
        """Draw a Gruvbox-themed circle with X cross."""
        if opacity < 0.01:
            return
        p.save()
        p.setOpacity(opacity)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(GBX_BG2))
        p.drawEllipse(QRectF(btn_cx - radius, btn_cy - radius, radius * 2, radius * 2))
        cross = radius * 0.45
        pen = QPen(QColor(GBX_FG), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(int(btn_cx - cross), int(btn_cy - cross),
                   int(btn_cx + cross), int(btn_cy + cross))
        p.drawLine(int(btn_cx + cross), int(btn_cy - cross),
                   int(btn_cx - cross), int(btn_cy + cross))
        p.restore()

    def _show_cancel_button(self):
        self._cancel_visible = True
        self._cancel_opacity = 0.0
        self._cancel_fade_timer.start()
        self._cancel_hide_timer.start(3000)

    def _hide_cancel_button(self):
        self._cancel_visible = False
        self._cancel_opacity = 0.0
        self._cancel_fade_timer.stop()
        self._cancel_hide_timer.stop()
        self.update()

    def _tick_cancel_fade(self):
        self._cancel_opacity = min(1.0, self._cancel_opacity + 0.1)
        if self._cancel_opacity >= 1.0:
            self._cancel_fade_timer.stop()
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        pos = event.position()
        pill_rect = self._pill.geometry()
        rect = QRectF(pill_rect).adjusted(-1, -1, 1, 1)
        cx = rect.center().x()
        cy = rect.center().y()

        if self._widget_state in ('listening', 'finishing', 'responding'):
            if self._cancel_visible:
                # Check if click is on X button (centered on shape)
                dist = ((pos.x() - cx) ** 2 + (pos.y() - cy) ** 2) ** 0.5
                if dist <= 14:
                    self._hide_cancel_button()
                    self.sig_cancel_clicked.emit()
                    return
                else:
                    self._hide_cancel_button()
                    return
            else:
                # First click on shape reveals X
                if self._widget_state == 'listening':
                    hit_r = rect.width() / 2.0
                else:
                    hit_r = rect.height() / 2.0
                dist = ((pos.x() - cx) ** 2 + (pos.y() - cy) ** 2) ** 0.5
                if dist <= hit_r + 4:
                    self._show_cancel_button()
                    return

    @pyqtSlot(object)
    def _do_update_spectrogram(self, bars):
        self._spectrogram.update_bars(bars)

    @pyqtSlot(str)
    def _do_update_transcription(self, text):
        self._transcription.set_text(text)
        self._position_on_screen()
