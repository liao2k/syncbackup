"""用 QPainter 繪製圖示，不需要額外圖檔。"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF


def _draw_sync_arrows(p: QPainter, rect: QRectF, color: QColor, width: float):
    pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    span = 120
    for start in (40, 220):
        p.drawArc(rect, start * 16, span * 16)
        # 箭頭（沿逆時針方向）
        ang = math.radians(start + span)
        cx, cy = rect.center().x(), rect.center().y()
        rx, ry = rect.width() / 2, rect.height() / 2
        pt = QPointF(cx + rx * math.cos(ang), cy - ry * math.sin(ang))
        d = QPointF(-math.sin(ang), -math.cos(ang))
        n = QPointF(math.cos(ang), -math.sin(ang))
        a = width * 1.9
        tip = pt + d * a
        tri = QPolygonF([tip, pt - d * (a * 0.3) + n * a, pt - d * (a * 0.3) - n * a])
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPolygon(tri)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)


def tray_icon(state: str = "idle") -> QIcon:
    """選單列圖示（單色 template，可自動適應深色 / 淺色選單列）。
    state: idle / syncing / paused / error / waiting"""
    size = 44
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    black = QColor(0, 0, 0)
    _draw_sync_arrows(p, QRectF(7, 7, 30, 30), black, 3.6)

    if state != "idle":
        # 右下角狀態標記（挖空外框，讓標記更清楚）
        badge = QRectF(25, 25, 19, 19)
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.setBrush(black)
        p.setPen(Qt.NoPen)
        p.drawEllipse(badge.adjusted(-2, -2, 2, 2))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setBrush(black)
        if state == "paused":
            p.drawRoundedRect(QRectF(28, 27, 4.5, 15), 1.5, 1.5)
            p.drawRoundedRect(QRectF(36, 27, 4.5, 15), 1.5, 1.5)
        elif state == "syncing":
            p.drawEllipse(badge.adjusted(3, 3, -3, -3))
        elif state in ("error", "waiting"):
            path = QPainterPath()
            path.moveTo(34.5, 25)
            path.lineTo(44, 43)
            path.lineTo(25, 43)
            path.closeSubpath()
            p.drawPath(path)
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.drawRect(QRectF(33.5, 31, 2, 6))
            p.drawRect(QRectF(33.5, 38.5, 2, 2))
    p.end()
    icon = QIcon(pm)
    icon.setIsMask(True)
    return icon


def app_pixmap(size: int = 512) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 100
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0, QColor("#3d8bfd"))
    grad.setColorAt(1, QColor("#1f5fd1"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(8 * s, 8 * s, 84 * s, 84 * s), 20 * s, 20 * s)
    # 硬碟
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawRoundedRect(QRectF(24 * s, 62 * s, 52 * s, 16 * s), 4 * s, 4 * s)
    p.setBrush(QColor("#2a72e8"))
    p.drawEllipse(QRectF(64 * s, 67.5 * s, 5 * s, 5 * s))
    _draw_sync_arrows(p, QRectF(32 * s, 20 * s, 36 * s, 36 * s), QColor("white"), 5.5 * s)
    p.end()
    return pm


def app_icon() -> QIcon:
    return QIcon(app_pixmap(512))


def dot(color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(size * 0.4, size * 0.4, size * 1.2, size * 1.2))
    p.end()
    return QIcon(pm)
