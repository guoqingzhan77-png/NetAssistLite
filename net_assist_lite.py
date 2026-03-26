#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NetAssist Lite - 轻量级 TCP 网络调试助手
功能：TCP Client、HEX/ASCII 双模显示与发送、断线自动重连、关键字自动回复
"""

import sys
import socket
import threading
import time
import json
import os
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox,
    QCheckBox, QSpinBox, QSplitter, QStatusBar, QMessageBox, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QDialog, QDialogButtonBox, QFormLayout, QTabWidget
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QTextCursor, QColor, QFontDatabase


# ═══════════════════════════════════════════════════════════════
#  信号桥：线程安全地从 socket 线程发送数据到 GUI
# ═══════════════════════════════════════════════════════════════
class SignalBridge(QObject):
    data_received = pyqtSignal(bytes)
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)
    error_occurred = pyqtSignal(str)


# ═══════════════════════════════════════════════════════════════
#  TCP 客户端线程
# ═══════════════════════════════════════════════════════════════
class TcpClientThread(threading.Thread):
    def __init__(self, host, port, signals):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.signals = signals
        self.sock = None
        self.running = False
        self._lock = threading.Lock()

    def run(self):
        self.running = True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(0.5)
            self.signals.connected.emit()

            while self.running:
                try:
                    data = self.sock.recv(4096)
                    if not data:
                        break
                    self.signals.data_received.emit(data)
                except socket.timeout:
                    continue
                except OSError:
                    break

            if self.running:
                self.signals.disconnected.emit("远程主机关闭了连接")
        except ConnectionRefusedError:
            self.signals.error_occurred.emit("连接被拒绝，请检查地址和端口")
        except socket.timeout:
            self.signals.error_occurred.emit("连接超时，服务器无响应")
        except Exception as e:
            self.signals.error_occurred.emit(str(e))
        finally:
            self._close_socket()

    def send_data(self, data: bytes) -> bool:
        with self._lock:
            if self.sock:
                try:
                    self.sock.sendall(data)
                    return True
                except Exception as e:
                    self.signals.error_occurred.emit(f"发送失败: {e}")
                    return False
        return False

    def stop(self):
        self.running = False
        self._close_socket()

    def _close_socket(self):
        with self._lock:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None


# ═══════════════════════════════════════════════════════════════
#  自动回复规则编辑对话框
# ═══════════════════════════════════════════════════════════════
class RuleEditDialog(QDialog):
    def __init__(self, parent=None, keyword="", response="", fmt="ASCII"):
        super().__init__(parent)
        self.setWindowTitle("编辑规则")
        self.setMinimumWidth(420)
        self._apply_style()

        layout = QFormLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        self.keyword_input = QLineEdit(keyword)
        self.keyword_input.setPlaceholderText("接收数据中包含此内容则触发")
        layout.addRow("匹配关键字:", self.keyword_input)

        self.response_input = QLineEdit(response)
        self.response_input.setPlaceholderText("触发后自动发送的内容")
        layout.addRow("回复内容:", self.response_input)

        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems(["ASCII", "HEX"])
        self.fmt_combo.setCurrentText(fmt)
        layout.addRow("数据格式:", self.fmt_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_values(self):
        return (
            self.keyword_input.text().strip(),
            self.response_input.text().strip(),
            self.fmt_combo.currentText()
        )

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog { background: #1e1e2e; color: #cdd6f4; }
            QLabel { color: #bac2de; font-size: 13px; }
            QLineEdit, QComboBox {
                background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 4px; padding: 6px 10px; font-size: 13px;
            }
            QLineEdit:focus, QComboBox:focus { border-color: #89b4fa; }
            QPushButton {
                background: #45475a; color: #cdd6f4; border: none;
                border-radius: 4px; padding: 6px 18px; font-size: 13px;
            }
            QPushButton:hover { background: #585b70; }
        """)


# ═══════════════════════════════════════════════════════════════
#  主窗口
# ═══════════════════════════════════════════════════════════════
class NetAssistLite(QMainWindow):
    def __init__(self):
        super().__init__()
        self.client_thread = None
        self.signals = SignalBridge()
        self.is_connected = False
        self.auto_reconnect = False
        self.reconnect_timer = QTimer()
        self.reconnect_timer.timeout.connect(self._try_reconnect)
        self.tx_count = 0
        self.rx_count = 0
        self.auto_reply_rules = []  # [{"keyword": ..., "response": ..., "format": ...}, ...]

        # 信号连接
        self.signals.data_received.connect(self._on_data_received)
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.error_occurred.connect(self._on_error)

        self._init_ui()
        self._apply_dark_theme()
        self.setWindowTitle("NetAssist Lite - TCP 网络调试助手")
        self.resize(960, 750)

    # ─────────────────────────────────────────────────────
    #  UI 构建
    # ─────────────────────────────────────────────────────
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 6)
        root.setSpacing(8)

        # ── 顶部：连接设置 ──
        conn_group = QGroupBox("连接设置")
        cl = QHBoxLayout(conn_group)
        cl.setSpacing(10)

        cl.addWidget(QLabel("服务器地址:"))
        self.host_input = QLineEdit("127.0.0.1")
        self.host_input.setFixedWidth(150)
        cl.addWidget(self.host_input)

        cl.addWidget(QLabel("端口:"))
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(8080)
        self.port_input.setFixedWidth(90)
        cl.addWidget(self.port_input)

        self.connect_btn = QPushButton("连  接")
        self.connect_btn.setFixedWidth(100)
        self.connect_btn.setObjectName("connectBtn")
        self.connect_btn.clicked.connect(self._toggle_connection)
        cl.addWidget(self.connect_btn)

        cl.addSpacing(12)
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #45475a;")
        cl.addWidget(sep)
        cl.addSpacing(12)

        self.auto_reconn_cb = QCheckBox("自动重连")
        self.auto_reconn_cb.toggled.connect(self._on_auto_reconnect_toggled)
        cl.addWidget(self.auto_reconn_cb)

        cl.addWidget(QLabel("间隔:"))
        self.reconn_interval = QSpinBox()
        self.reconn_interval.setRange(1, 60)
        self.reconn_interval.setValue(3)
        self.reconn_interval.setFixedWidth(55)
        self.reconn_interval.setSuffix(" 秒")
        cl.addWidget(self.reconn_interval)

        cl.addStretch()
        self.status_indicator = QLabel("● 未连接")
        self.status_indicator.setObjectName("statusDisconnected")
        cl.addWidget(self.status_indicator)

        root.addWidget(conn_group)

        # ── 中部 Tabs: 收发 + 自动回复 ──
        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")

        # ---------- Tab 1: 数据收发 ----------
        data_tab = QWidget()
        data_layout = QVBoxLayout(data_tab)
        data_layout.setContentsMargins(0, 8, 0, 0)
        data_layout.setSpacing(6)

        splitter = QSplitter(Qt.Vertical)

        # 接收区
        recv_widget = QWidget()
        recv_layout = QVBoxLayout(recv_widget)
        recv_layout.setContentsMargins(0, 0, 0, 0)
        recv_layout.setSpacing(4)

        recv_bar = QHBoxLayout()
        recv_bar.addWidget(QLabel("接收区"))
        recv_bar.addSpacing(16)
        recv_bar.addWidget(QLabel("显示格式:"))
        self.recv_fmt = QComboBox()
        self.recv_fmt.addItems(["ASCII", "HEX"])
        self.recv_fmt.setFixedWidth(80)
        recv_bar.addWidget(self.recv_fmt)

        self.show_time_cb = QCheckBox("显示时间戳")
        self.show_time_cb.setChecked(True)
        recv_bar.addWidget(self.show_time_cb)

        recv_bar.addStretch()
        self.clear_recv_btn = QPushButton("清空接收")
        self.clear_recv_btn.setObjectName("toolBtn")
        self.clear_recv_btn.clicked.connect(self._clear_recv)
        recv_bar.addWidget(self.clear_recv_btn)
        recv_layout.addLayout(recv_bar)

        self.recv_text = QTextEdit()
        self.recv_text.setReadOnly(True)
        self.recv_text.setObjectName("dataBox")
        recv_layout.addWidget(self.recv_text)
        splitter.addWidget(recv_widget)

        # 发送区
        send_widget = QWidget()
        send_layout = QVBoxLayout(send_widget)
        send_layout.setContentsMargins(0, 0, 0, 0)
        send_layout.setSpacing(4)

        send_bar = QHBoxLayout()
        send_bar.addWidget(QLabel("发送区"))
        send_bar.addSpacing(16)
        send_bar.addWidget(QLabel("发送格式:"))
        self.send_fmt = QComboBox()
        self.send_fmt.addItems(["ASCII", "HEX"])
        self.send_fmt.setFixedWidth(80)
        send_bar.addWidget(self.send_fmt)

        self.send_newline_cb = QCheckBox("追加换行")
        self.send_newline_cb.setChecked(True)
        send_bar.addWidget(self.send_newline_cb)

        send_bar.addStretch()
        self.clear_send_btn = QPushButton("清空发送")
        self.clear_send_btn.setObjectName("toolBtn")
        self.clear_send_btn.clicked.connect(self._clear_send)
        send_bar.addWidget(self.clear_send_btn)
        send_layout.addLayout(send_bar)

        send_bottom = QHBoxLayout()
        self.send_text = QTextEdit()
        self.send_text.setObjectName("dataBox")
        self.send_text.setMaximumHeight(100)
        self.send_text.setPlaceholderText("在此输入要发送的数据...")
        send_bottom.addWidget(self.send_text)

        self.send_btn = QPushButton("发  送")
        self.send_btn.setFixedSize(80, 60)
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.clicked.connect(self._send_data)
        send_bottom.addWidget(self.send_btn)

        send_layout.addLayout(send_bottom)
        splitter.addWidget(send_widget)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        data_layout.addWidget(splitter)

        self.tabs.addTab(data_tab, "数据收发")

        # ---------- Tab 2: 自动回复规则 ----------
        reply_tab = QWidget()
        reply_layout = QVBoxLayout(reply_tab)
        reply_layout.setContentsMargins(0, 8, 0, 0)
        reply_layout.setSpacing(8)

        reply_top = QHBoxLayout()
        self.auto_reply_cb = QCheckBox("启用自动回复")
        self.auto_reply_cb.setStyleSheet("font-size: 14px; font-weight: bold;")
        reply_top.addWidget(self.auto_reply_cb)
        reply_top.addStretch()

        self.add_rule_btn = QPushButton("+ 添加规则")
        self.add_rule_btn.setObjectName("toolBtn")
        self.add_rule_btn.clicked.connect(self._add_rule)
        reply_top.addWidget(self.add_rule_btn)

        self.edit_rule_btn = QPushButton("编辑")
        self.edit_rule_btn.setObjectName("toolBtn")
        self.edit_rule_btn.clicked.connect(self._edit_rule)
        reply_top.addWidget(self.edit_rule_btn)

        self.del_rule_btn = QPushButton("删除")
        self.del_rule_btn.setObjectName("toolBtn")
        self.del_rule_btn.clicked.connect(self._delete_rule)
        reply_top.addWidget(self.del_rule_btn)

        reply_layout.addLayout(reply_top)

        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setHorizontalHeaderLabels(["匹配关键字", "回复内容", "格式"])
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.rules_table.setColumnWidth(2, 70)
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rules_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.rules_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rules_table.setObjectName("rulesTable")
        reply_layout.addWidget(self.rules_table)

        self.tabs.addTab(reply_tab, "自动回复")

        root.addWidget(self.tabs, 1)

        # ── 底部状态栏 ──
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.tx_label = QLabel("发送: 0 字节")
        self.rx_label = QLabel("接收: 0 字节")
        self.tx_label.setStyleSheet("color: #a6e3a1; margin-right: 20px;")
        self.rx_label.setStyleSheet("color: #89b4fa; margin-right: 20px;")
        self.statusBar.addPermanentWidget(self.tx_label)
        self.statusBar.addPermanentWidget(self.rx_label)

    # ─────────────────────────────────────────────────────
    #  深色主题
    # ─────────────────────────────────────────────────────
    def _apply_dark_theme(self):
        self.setStyleSheet("""
            * {
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QMainWindow, QWidget {
                background: #1e1e2e;
                color: #cdd6f4;
            }
            QGroupBox {
                border: 1px solid #45475a;
                border-radius: 6px;
                margin-top: 10px;
                padding: 14px 10px 8px 10px;
                font-weight: bold;
                color: #bac2de;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 6px;
            }
            QLabel {
                color: #bac2de;
            }
            QLineEdit, QSpinBox {
                background: #313244;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 5px 8px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border-color: #89b4fa;
            }
            QComboBox {
                background: #313244;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QComboBox:focus { border-color: #89b4fa; }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox QAbstractItemView {
                background: #313244;
                color: #cdd6f4;
                selection-background-color: #45475a;
                border: 1px solid #585b70;
            }
            QCheckBox {
                color: #bac2de;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border-radius: 3px;
                border: 1px solid #585b70;
                background: #313244;
            }
            QCheckBox::indicator:checked {
                background: #89b4fa;
                border-color: #89b4fa;
            }

            /* 连接按钮 */
            #connectBtn {
                background: #89b4fa;
                color: #1e1e2e;
                border: none;
                border-radius: 5px;
                padding: 6px 12px;
                font-weight: bold;
                font-size: 13px;
            }
            #connectBtn:hover { background: #74c7ec; }
            #connectBtn:pressed { background: #89dceb; }

            /* 发送按钮 */
            #sendBtn {
                background: #a6e3a1;
                color: #1e1e2e;
                border: none;
                border-radius: 5px;
                font-weight: bold;
                font-size: 14px;
            }
            #sendBtn:hover { background: #94e2d5; }
            #sendBtn:pressed { background: #89dceb; }

            /* 工具按钮 */
            #toolBtn {
                background: #45475a;
                color: #cdd6f4;
                border: none;
                border-radius: 4px;
                padding: 5px 14px;
            }
            #toolBtn:hover { background: #585b70; }

            /* 数据框 */
            #dataBox {
                background: #11111b;
                color: #cdd6f4;
                border: 1px solid #313244;
                border-radius: 5px;
                padding: 6px;
                font-family: "Cascadia Code", "Consolas", "Courier New", monospace;
                font-size: 13px;
            }

            /* 状态标签 */
            #statusDisconnected { color: #f38ba8; font-weight: bold; font-size: 13px; }
            #statusConnected    { color: #a6e3a1; font-weight: bold; font-size: 13px; }
            #statusReconnecting { color: #fab387; font-weight: bold; font-size: 13px; }

            /* Tabs */
            #mainTabs::pane {
                border: 1px solid #45475a;
                border-radius: 4px;
                background: #1e1e2e;
            }
            QTabBar::tab {
                background: #313244;
                color: #bac2de;
                padding: 8px 24px;
                margin-right: 2px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QTabBar::tab:selected {
                background: #45475a;
                color: #cdd6f4;
            }
            QTabBar::tab:hover { background: #585b70; }

            /* 规则表格 */
            #rulesTable {
                background: #11111b;
                color: #cdd6f4;
                gridline-color: #313244;
                border: 1px solid #313244;
                border-radius: 4px;
                font-size: 13px;
            }
            #rulesTable QHeaderView::section {
                background: #313244;
                color: #bac2de;
                padding: 6px;
                border: none;
                border-bottom: 1px solid #45475a;
                font-weight: bold;
            }
            #rulesTable::item:selected {
                background: #45475a;
            }

            /* Splitter */
            QSplitter::handle {
                background: #45475a;
                height: 3px;
                margin: 3px 0;
                border-radius: 1px;
            }

            /* 状态栏 */
            QStatusBar {
                background: #181825;
                color: #6c7086;
                border-top: 1px solid #313244;
            }

            /* 滚动条 */
            QScrollBar:vertical {
                background: #1e1e2e; width: 10px; border: none;
            }
            QScrollBar::handle:vertical {
                background: #45475a; border-radius: 5px; min-height: 30px;
            }
            QScrollBar::handle:vertical:hover { background: #585b70; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal {
                background: #1e1e2e; height: 10px; border: none;
            }
            QScrollBar::handle:horizontal {
                background: #45475a; border-radius: 5px; min-width: 30px;
            }
            QScrollBar::handle:horizontal:hover { background: #585b70; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        """)

    # ─────────────────────────────────────────────────────
    #  连接 / 断开
    # ─────────────────────────────────────────────────────
    def _toggle_connection(self):
        if self.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        host = self.host_input.text().strip()
        port = self.port_input.value()
        if not host:
            self._log_system("请输入服务器地址")
            return

        self._log_system(f"正在连接 {host}:{port} ...")
        self.status_indicator.setText("● 连接中...")
        self.status_indicator.setObjectName("statusReconnecting")
        self.status_indicator.setStyleSheet(self._style_for("statusReconnecting"))
        self.connect_btn.setEnabled(False)

        self.signals = SignalBridge()
        self.signals.data_received.connect(self._on_data_received)
        self.signals.connected.connect(self._on_connected)
        self.signals.disconnected.connect(self._on_disconnected)
        self.signals.error_occurred.connect(self._on_error)

        self.client_thread = TcpClientThread(host, port, self.signals)
        self.client_thread.start()

    def _disconnect(self):
        self.reconnect_timer.stop()
        if self.client_thread:
            self.client_thread.stop()
            self.client_thread = None
        self.is_connected = False
        self._update_ui_disconnected()
        self._log_system("已断开连接")

    # ─────────────────────────────────────────────────────
    #  信号处理
    # ─────────────────────────────────────────────────────
    def _on_connected(self):
        self.is_connected = True
        self.reconnect_timer.stop()
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("断  开")
        self.connect_btn.setStyleSheet(
            "background: #f38ba8; color: #1e1e2e; border: none; "
            "border-radius: 5px; padding: 6px 12px; font-weight: bold; font-size: 13px;"
        )
        self.status_indicator.setText("● 已连接")
        self.status_indicator.setObjectName("statusConnected")
        self.status_indicator.setStyleSheet(self._style_for("statusConnected"))
        self.host_input.setEnabled(False)
        self.port_input.setEnabled(False)
        host = self.host_input.text().strip()
        port = self.port_input.value()
        self._log_system(f"成功连接到 {host}:{port}")

    def _on_disconnected(self, reason):
        if not self.is_connected:
            return
        self.is_connected = False
        self._update_ui_disconnected()
        self._log_system(f"连接断开: {reason}")
        if self.auto_reconnect:
            self._start_auto_reconnect()

    def _on_error(self, msg):
        was_connected = self.is_connected
        self.is_connected = False
        self._update_ui_disconnected()
        self._log_system(f"错误: {msg}")
        if self.auto_reconnect and not was_connected:
            self._start_auto_reconnect()
        elif self.auto_reconnect and was_connected:
            self._start_auto_reconnect()

    def _on_data_received(self, data: bytes):
        self.rx_count += len(data)
        self.rx_label.setText(f"接收: {self.rx_count} 字节")

        ts = ""
        if self.show_time_cb.isChecked():
            ts = datetime.now().strftime("[%H:%M:%S.%f")[:-3] + "] "

        fmt = self.recv_fmt.currentText()
        if fmt == "HEX":
            display = ts + "← " + " ".join(f"{b:02X}" for b in data)
        else:
            try:
                display = ts + "← " + data.decode("utf-8", errors="replace")
            except Exception:
                display = ts + "← " + data.hex()

        self._append_recv(display, is_recv=True)

        # 自动回复检查
        if self.auto_reply_cb.isChecked():
            self._check_auto_reply(data)

    # ─────────────────────────────────────────────────────
    #  发送数据
    # ─────────────────────────────────────────────────────
    def _send_data(self):
        if not self.is_connected or not self.client_thread:
            self._log_system("未连接，无法发送")
            return

        raw = self.send_text.toPlainText()
        if not raw.strip():
            return

        fmt = self.send_fmt.currentText()
        try:
            if fmt == "HEX":
                hex_str = raw.replace(" ", "").replace("\n", "").replace("\r", "")
                if len(hex_str) % 2 != 0:
                    self._log_system("HEX 数据长度必须为偶数")
                    return
                data = bytes.fromhex(hex_str)
            else:
                text = raw
                if self.send_newline_cb.isChecked():
                    text += "\r\n"
                data = text.encode("utf-8")
        except ValueError as e:
            self._log_system(f"数据格式错误: {e}")
            return

        if self.client_thread.send_data(data):
            self.tx_count += len(data)
            self.tx_label.setText(f"发送: {self.tx_count} 字节")

            ts = ""
            if self.show_time_cb.isChecked():
                ts = datetime.now().strftime("[%H:%M:%S.%f")[:-3] + "] "

            if fmt == "HEX":
                display = ts + "→ " + " ".join(f"{b:02X}" for b in data)
            else:
                display = ts + "→ " + raw
            self._append_recv(display, is_recv=False)

    def _send_bytes_silent(self, data: bytes, label: str = "自动回复"):
        """自动回复专用发送，不清空发送框"""
        if not self.is_connected or not self.client_thread:
            return
        if self.client_thread.send_data(data):
            self.tx_count += len(data)
            self.tx_label.setText(f"发送: {self.tx_count} 字节")

            ts = ""
            if self.show_time_cb.isChecked():
                ts = datetime.now().strftime("[%H:%M:%S.%f")[:-3] + "] "

            display_hex = " ".join(f"{b:02X}" for b in data)
            try:
                display_ascii = data.decode("utf-8", errors="replace")
            except Exception:
                display_ascii = display_hex

            fmt = self.recv_fmt.currentText()
            shown = display_hex if fmt == "HEX" else display_ascii
            self._append_recv(f"{ts}→ [{label}] {shown}", is_recv=False)

    # ─────────────────────────────────────────────────────
    #  自动回复
    # ─────────────────────────────────────────────────────
    def _check_auto_reply(self, data: bytes):
        # 同时检查 ASCII 和 HEX 表示
        try:
            text_repr = data.decode("utf-8", errors="replace")
        except Exception:
            text_repr = ""
        hex_repr = " ".join(f"{b:02X}" for b in data).upper()

        for rule in self.auto_reply_rules:
            keyword = rule["keyword"]
            if not keyword:
                continue

            matched = False
            if keyword.upper() in hex_repr:
                matched = True
            if keyword in text_repr:
                matched = True

            if matched:
                resp_fmt = rule.get("format", "ASCII")
                resp_str = rule["response"]
                try:
                    if resp_fmt == "HEX":
                        resp_data = bytes.fromhex(resp_str.replace(" ", ""))
                    else:
                        resp_data = resp_str.encode("utf-8")
                except Exception as e:
                    self._log_system(f"自动回复格式错误: {e}")
                    continue

                self._send_bytes_silent(resp_data, f"匹配: {keyword}")

    # ─────────────────────────────────────────────────────
    #  自动回复规则管理
    # ─────────────────────────────────────────────────────
    def _add_rule(self):
        dlg = RuleEditDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            kw, resp, fmt = dlg.get_values()
            if kw and resp:
                self.auto_reply_rules.append({"keyword": kw, "response": resp, "format": fmt})
                self._refresh_rules_table()

    def _edit_rule(self):
        row = self.rules_table.currentRow()
        if row < 0:
            self._log_system("请先选择要编辑的规则")
            return
        rule = self.auto_reply_rules[row]
        dlg = RuleEditDialog(self, rule["keyword"], rule["response"], rule.get("format", "ASCII"))
        if dlg.exec_() == QDialog.Accepted:
            kw, resp, fmt = dlg.get_values()
            if kw and resp:
                self.auto_reply_rules[row] = {"keyword": kw, "response": resp, "format": fmt}
                self._refresh_rules_table()

    def _delete_rule(self):
        row = self.rules_table.currentRow()
        if row < 0:
            self._log_system("请先选择要删除的规则")
            return
        self.auto_reply_rules.pop(row)
        self._refresh_rules_table()

    def _refresh_rules_table(self):
        self.rules_table.setRowCount(len(self.auto_reply_rules))
        for i, rule in enumerate(self.auto_reply_rules):
            self.rules_table.setItem(i, 0, QTableWidgetItem(rule["keyword"]))
            self.rules_table.setItem(i, 1, QTableWidgetItem(rule["response"]))
            self.rules_table.setItem(i, 2, QTableWidgetItem(rule.get("format", "ASCII")))

    # ─────────────────────────────────────────────────────
    #  自动重连
    # ─────────────────────────────────────────────────────
    def _on_auto_reconnect_toggled(self, checked):
        self.auto_reconnect = checked
        if not checked:
            self.reconnect_timer.stop()

    def _start_auto_reconnect(self):
        if not self.auto_reconnect:
            return
        interval = self.reconn_interval.value() * 1000
        self.status_indicator.setText("● 等待重连...")
        self.status_indicator.setObjectName("statusReconnecting")
        self.status_indicator.setStyleSheet(self._style_for("statusReconnecting"))
        self._log_system(f"将在 {self.reconn_interval.value()} 秒后尝试重连...")
        self.reconnect_timer.start(interval)

    def _try_reconnect(self):
        self.reconnect_timer.stop()
        if self.is_connected:
            return
        self._log_system("正在尝试重连...")
        self._connect()

    # ─────────────────────────────────────────────────────
    #  UI 辅助
    # ─────────────────────────────────────────────────────
    def _update_ui_disconnected(self):
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("连  接")
        self.connect_btn.setStyleSheet("")  # 恢复默认样式
        self.status_indicator.setText("● 未连接")
        self.status_indicator.setObjectName("statusDisconnected")
        self.status_indicator.setStyleSheet(self._style_for("statusDisconnected"))
        self.host_input.setEnabled(True)
        self.port_input.setEnabled(True)

    def _append_recv(self, text, is_recv=True):
        cursor = self.recv_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.recv_text.setTextCursor(cursor)

        color = "#89b4fa" if is_recv else "#a6e3a1"
        html = f'<span style="color:{color};">{self._escape_html(text)}</span><br>'
        self.recv_text.insertHtml(html)
        self.recv_text.ensureCursorVisible()

    def _log_system(self, msg):
        ts = datetime.now().strftime("[%H:%M:%S] ")
        cursor = self.recv_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.recv_text.setTextCursor(cursor)
        html = f'<span style="color:#fab387;">{self._escape_html(ts + "系统: " + msg)}</span><br>'
        self.recv_text.insertHtml(html)
        self.recv_text.ensureCursorVisible()

    def _clear_recv(self):
        self.recv_text.clear()
        self.rx_count = 0
        self.tx_count = 0
        self.rx_label.setText("接收: 0 字节")
        self.tx_label.setText("发送: 0 字节")

    def _clear_send(self):
        self.send_text.clear()

    @staticmethod
    def _escape_html(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(" ", "&nbsp;")

    @staticmethod
    def _style_for(name):
        styles = {
            "statusDisconnected": "color: #f38ba8; font-weight: bold; font-size: 13px;",
            "statusConnected":    "color: #a6e3a1; font-weight: bold; font-size: 13px;",
            "statusReconnecting": "color: #fab387; font-weight: bold; font-size: 13px;",
        }
        return styles.get(name, "")

    def closeEvent(self, event):
        self.reconnect_timer.stop()
        if self.client_thread:
            self.client_thread.stop()
        event.accept()


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = NetAssistLite()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
