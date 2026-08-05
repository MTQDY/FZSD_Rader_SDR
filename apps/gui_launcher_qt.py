#!/usr/bin/env python3
"""
RoboMaster 无线电接收系统 GUI
本GUI解析一、二级干扰波后转为解析信息波。
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PyQt5 import Qt, QtCore, QtWidgets

# ---------------------------------------------------------------------------
# 路径常量（与 gui_launcher.py 保持一致）
# ---------------------------------------------------------------------------
PYTHONPATH_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class RunSession:
    run_id: int
    started_at: datetime
    log_path: Path
    process: subprocess.Popen[str] | None = None
    read_thread: threading.Thread | None = None
    stop_requested: bool = False
    log_saved: bool = False
    log_lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 主 GUI 类
# ---------------------------------------------------------------------------
class JamRxGUI(QtWidgets.QWidget):
    """PyQt5 版干扰波接收系统 GUI，接口与 tkinter 版完全一致。"""

    LAUNCHER_NAME = "gui_launcher_qt"
    WINDOW_TITLE = "RoboMaster 无线电接收系统"
    INFO_POSITION_NAMES = (
        ("enemy_hero", "英雄1"),
        ("enemy_engineer", "工程2"),
        ("enemy_infantry3", "步兵3"),
        ("enemy_infantry4", "步兵4"),
        ("enemy_air", "空中6"),
        ("enemy_sentinel", "哨兵7"),
    )
    INITIAL_LEVEL = 1
    PARSE_POLICY = "default"
    PAYLOAD_ENDIAN = "little"

    # 6 种接收参数组合：id → (team, initial_level, label)
    DEBUG_CONFIGS: list[tuple[str, int, str]] = [
        ("red",  1, "红方 JAM1"),
        ("red",  2, "红方 JAM2"),
        ("red",  3, "红方 INFO"),
        ("blue", 1, "蓝方 JAM1"),
        ("blue", 2, "蓝方 JAM2"),
        ("blue", 3, "蓝方 INFO"),
    ]

    # ---- Qt 信号（线程安全） ----
    sig_log = QtCore.pyqtSignal(str, str)          # (message, tag)
    sig_update_display = QtCore.pyqtSignal()
    sig_process_stopped = QtCore.pyqtSignal(int)    # rc

    def __init__(self):
        super().__init__()
        self.setWindowTitle(self.WINDOW_TITLE)
        self.resize(1280, 720)

        # ---- 运行时状态（与 tkinter 版完全一致） ----
        self.active_session: RunSession | None = None
        self.next_run_id = 0
        self.ui_session_id: int | None = None
        self.running = False
        self.log_lock = threading.Lock()

        self.last_jam_key = "N/A"
        self.last_jam_level = "N/A"
        self.last_rx_mode = "N/A"
        self.jam_frame_count = 0
        self.info_frame_count = 0
        self.last_profile = "N/A"
        self.last_center_freq = "N/A"
        self.last_power = "N/A"
        self.last_status = "未运行"
        self.last_info_positions: dict[str, dict[str, int]] = {}

        self._create_ui()
        self._connect_signals()

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _apply_stylesheet(self):
        """全局圆角 + 选中高亮样式。"""
        self.setStyleSheet("""
            QGroupBox {
                border: 1px solid #b0b0b0;
                border-radius: 12px;
                margin-top: 12px;
                padding-top: 14px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLineEdit {
                border: 1px solid #b0b0b0;
                border-radius: 10px;
                padding: 4px 8px;
                background: #fafafa;
            }
            QLineEdit:focus {
                border: 2px solid #ff8c00;
                background: #fff8f0;
            }
            QPushButton {
                border: 1px solid #b0b0b0;
                border-radius: 10px;
                padding: 6px 20px;
                background: #e8e8e8;
            }
            QPushButton:hover {
                background: #dcdcdc;
            }
            QPushButton:pressed {
                background: #c8c8c8;
            }
            QPushButton:disabled {
                color: #999;
                background: #f0f0f0;
            }
            QPlainTextEdit {
                border: 1px solid #b0b0b0;
                border-radius: 10px;
                padding: 4px;
            }
            QCheckBox, QRadioButton {
                spacing: 4px;
            }
        """)

    def _create_ui(self):
        self._apply_stylesheet()
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setSpacing(10)

        # ---- 配置参数 ----
        cfg_group = QtWidgets.QGroupBox("配置参数")
        cfg_grid = QtWidgets.QGridLayout(cfg_group)

        cfg_grid.addWidget(QtWidgets.QLabel("PlutoSDR 接收器 IP:"), 0, 0)
        self.jam_ip_edit = QtWidgets.QLineEdit("192.168.2.1")
        cfg_grid.addWidget(self.jam_ip_edit, 0, 1)

        # 接收参数配置（6 种组合）
        cfg_grid.addWidget(QtWidgets.QLabel("接收参数配置:"), 0, 2)
        self._debug_radios: dict[int, QtWidgets.QRadioButton] = {}
        self._debug_group = QtWidgets.QButtonGroup(self)
        debug_widget = QtWidgets.QWidget()
        debug_grid = QtWidgets.QGridLayout(debug_widget)
        debug_grid.setContentsMargins(0, 0, 0, 0)
        debug_grid.setSpacing(4)
        for idx, (_team, _level, label) in enumerate(self.DEBUG_CONFIGS):
            row = idx // 3
            col = idx % 3
            rb = QtWidgets.QRadioButton(label.split(" ", 1)[1])  # "JAM1" / "JAM2" / "INFO"
            self._debug_radios[idx] = rb
            self._debug_group.addButton(rb, idx)
            debug_grid.addWidget(rb, row, col)
        # 红方行标题 / 蓝方行标题
        debug_grid.addWidget(QtWidgets.QLabel("红方"), 0, 3)
        debug_grid.addWidget(QtWidgets.QLabel("蓝方"), 1, 3)
        self._debug_radios[0].setChecked(True)  # 默认：红方 JAM1
        cfg_grid.addWidget(debug_widget, 0, 3, 1, 2)

        cfg_grid.addWidget(QtWidgets.QLabel("服务器 IP:"), 1, 0)
        self.server_ip_edit = QtWidgets.QLineEdit("127.0.0.1")
        cfg_grid.addWidget(self.server_ip_edit, 1, 1)

        cfg_grid.addWidget(QtWidgets.QLabel("服务器端口:"), 1, 2)
        self.server_port_edit = QtWidgets.QLineEdit("5000")
        cfg_grid.addWidget(self.server_port_edit, 1, 3)

        main_layout.addWidget(cfg_group)

        # ---- 控制栏 ----
        ctrl_layout = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("启动接收")
        self.stop_btn = QtWidgets.QPushButton("停止接收")
        self.stop_btn.setEnabled(False)
        self.chk_realtime = QtWidgets.QCheckBox("显示实时日志")
        self.chk_realtime.setChecked(True)

        self.status_label = QtWidgets.QLabel("未运行")
        self.status_label.setStyleSheet("color: gray; font-weight: bold;")

        ctrl_layout.addWidget(self.start_btn)
        ctrl_layout.addWidget(self.stop_btn)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.chk_realtime)
        ctrl_layout.addWidget(self.status_label)
        main_layout.addLayout(ctrl_layout)

        self.start_btn.clicked.connect(self._start_receiver)
        self.stop_btn.clicked.connect(self._stop_receiver)

        # ---- 接收状态 ----
        status_group = QtWidgets.QGroupBox("接收状态")
        status_grid = QtWidgets.QGridLayout(status_group)

        row = 0
        # 干扰波密钥
        status_grid.addWidget(QtWidgets.QLabel("干扰波密钥:"), row, 0)
        self.jam_key_label = QtWidgets.QLabel("N/A")
        self.jam_key_label.setStyleSheet("color: #ff8c00; font-size: 16px; font-family: monospace;")
        status_grid.addWidget(self.jam_key_label, row, 1)

        # 干扰等级
        status_grid.addWidget(QtWidgets.QLabel("干扰等级:"), row, 2)
        self.jam_level_label = QtWidgets.QLabel("N/A")
        self.jam_level_label.setStyleSheet("color: gray; font-size: 16px; font-weight: bold;")
        status_grid.addWidget(self.jam_level_label, row, 3)

        row = 1
        # 接收模式
        status_grid.addWidget(QtWidgets.QLabel("接收模式:"), row, 0)
        self.rx_mode_label = QtWidgets.QLabel("N/A")
        self.rx_mode_label.setStyleSheet("color: #ff8c00; font-size: 16px; font-weight: bold;")
        status_grid.addWidget(self.rx_mode_label, row, 1)

        # 干扰波帧数
        status_grid.addWidget(QtWidgets.QLabel("干扰波帧数:"), row, 2)
        self.jam_count_label = QtWidgets.QLabel("0")
        self.jam_count_label.setStyleSheet("color: #ff8c00; font-size: 16px;")
        status_grid.addWidget(self.jam_count_label, row, 3)

        row = 2
        # 信息波帧数
        status_grid.addWidget(QtWidgets.QLabel("信息波帧数:"), row, 0)
        self.info_count_label = QtWidgets.QLabel("0")
        self.info_count_label.setStyleSheet("color: #ff8c00; font-size: 16px;")
        status_grid.addWidget(self.info_count_label, row, 1)

        # 当前频段
        status_grid.addWidget(QtWidgets.QLabel("当前频段:"), row, 2)
        self.profile_label = QtWidgets.QLabel("N/A")
        self.profile_label.setStyleSheet("font-family: monospace;")
        status_grid.addWidget(self.profile_label, row, 3)

        row = 3
        # 中心频率
        status_grid.addWidget(QtWidgets.QLabel("中心频率:"), row, 0)
        self.freq_label = QtWidgets.QLabel("N/A")
        self.freq_label.setStyleSheet("font-family: monospace;")
        status_grid.addWidget(self.freq_label, row, 1)

        # 接收功率
        status_grid.addWidget(QtWidgets.QLabel("接收功率:"), row, 2)
        self.power_label = QtWidgets.QLabel("N/A")
        self.power_label.setStyleSheet("font-family: monospace;")
        status_grid.addWidget(self.power_label, row, 3)

        main_layout.addWidget(status_group)

        # ---- 信息波坐标 ----
        info_group = QtWidgets.QGroupBox("0x0A01 敌方机器人坐标")
        info_grid = QtWidgets.QGridLayout(info_group)

        self.info_position_labels: dict[str, QtWidgets.QLabel] = {}
        for index, (field_name, title) in enumerate(self.INFO_POSITION_NAMES):
            r = index // 2
            c = (index % 2) * 2
            info_grid.addWidget(QtWidgets.QLabel(f"{title}:"), r, c)
            label = QtWidgets.QLabel("N/A")
            label.setStyleSheet("color: black; font-family: monospace;")
            info_grid.addWidget(label, r, c + 1)
            self.info_position_labels[field_name] = label

        main_layout.addWidget(info_group)

        # ---- 日志输出 ----
        log_group = QtWidgets.QGroupBox("日志输出")
        log_layout = QtWidgets.QVBoxLayout(log_group)
        self.log_text = QtWidgets.QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_text)
        main_layout.addWidget(log_group, 1)

    def _connect_signals(self):
        self.sig_log.connect(self._on_log_signal)
        self.sig_update_display.connect(self._update_display)
        self.sig_process_stopped.connect(self._on_process_stopped)

    # ==================================================================
    # 辅助
    # ==================================================================
    def _current_config(self) -> tuple[str, int]:
        """返回当前选中的 (team, initial_level)。"""
        idx = self._debug_group.checkedId()
        if idx < 0:
            idx = 1  # 兜底：红方 JAM2
        team, level, _label = self.DEBUG_CONFIGS[idx]
        return team, level

    @property
    def team_var(self) -> str:
        return self._current_config()[0]

    @property
    def jam_ip(self) -> str:
        return self.jam_ip_edit.text().strip()

    @property
    def server_ip(self) -> str:
        return self.server_ip_edit.text().strip()

    @property
    def server_port_str(self) -> str:
        return self.server_port_edit.text().strip()

    @property
    def show_realtime(self) -> bool:
        return self.chk_realtime.isChecked()

    @property
    def debug_level(self) -> int:
        return self._current_config()[1]

    def _set_debug_radios_enabled(self, enabled: bool):
        for rb in self._debug_radios.values():
            rb.setEnabled(enabled)

    # ==================================================================
    # 会话 & 日志（与 tkinter 版完全一致）
    # ==================================================================
    def _new_run_session(self) -> RunSession:
        self.next_run_id += 1
        started_at = datetime.now()
        stamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
        return RunSession(
            run_id=self.next_run_id,
            started_at=started_at,
            log_path=LOG_DIR / f"{self.LAUNCHER_NAME}_{stamp}.log",
        )

    def _record_log_entry(self, session: RunSession, entry: str):
        if session.log_saved:
            return
        with self.log_lock:
            if session.log_saved:
                return
            session.log_lines.append(entry)

    def _on_log_signal(self, entry: str, tag: str):
        """在主线程中安全追加日志到界面。"""
        color_map = {
            "info":    "black",
            "success": "green",
            "warn":    "orange",
            "error":   "red",
            "jam":     "blue",
        }
        color = color_map.get(tag, "black")
        self.log_text.appendHtml(
            f'<span style="color:{color};">{self._html_escape(entry)}</span>'
        )

    @staticmethod
    def _html_escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _log(
        self,
        message: str,
        tag: str = "info",
        show: bool = True,
        session: RunSession | None = None,
    ):
        entry = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        target_session = session or self.active_session
        if target_session is not None:
            self._record_log_entry(target_session, entry)
        if not show:
            return
        if session is not None and session.run_id != self.ui_session_id:
            return
        # 通过信号安全投递到主线程
        self.sig_log.emit(entry, tag)

    def _persist_log_session(self, session: RunSession, exit_reason: str):
        with self.log_lock:
            if session.log_saved:
                return
            lines = list(session.log_lines)
        footer_time = datetime.now().strftime("%H:%M:%S")
        started_at = session.started_at.strftime("%Y-%m-%d %H:%M:%S")
        payload = [
            f"# gui_launcher_qt run started at {started_at}",
            *lines,
            f"[{footer_time}] 会话结束: {exit_reason}",
        ]
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            session.log_path.write_text("\n".join(payload) + "\n", encoding="utf-8")
        except Exception as exc:
            self._log(f"写入日志文件失败: {exc}", "error")
            return
        session.log_saved = True

    def _reset_runtime_state(self):
        self.last_jam_key = "N/A"
        self.last_jam_level = "N/A"
        self.last_rx_mode = "N/A"
        self.jam_frame_count = 0
        self.info_frame_count = 0
        self.last_profile = "N/A"
        self.last_center_freq = "N/A"
        self.last_power = "N/A"
        self.last_status = "未运行"
        self.last_info_positions = {}
        self.sig_update_display.emit()

    # ==================================================================
    # 子进程管理与命令构建（与 tkinter 版完全一致）
    # ==================================================================
    def _build_command(self) -> list[str]:
        server_port = int(self.server_port_str)
        if shutil.which("conda"):
            runner = ["conda", "run", "--no-capture-output", "-n", "radio", "python3"]
        else:
            runner = [sys.executable]
        return runner + [
            "-u", "-m", "FZSD_RX_SDR.gr_rx_launcher",
            "--rx-ip", self.jam_ip,
            "--team", self.team_var,
            "--initial-level", str(self.debug_level),
            "--server-ip", self.server_ip,
            "--server-port", str(server_port),
            "--parse-policy", self.PARSE_POLICY,
            "--payload-endian", self.PAYLOAD_ENDIAN,
            "--record-wave",
            "--record-tag", self.LAUNCHER_NAME,
        ]

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[str], sig: int):
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    def _start_receiver(self):
        try:
            int(self.server_port_str)
        except ValueError:
            QtWidgets.QMessageBox.critical(self, "错误", "服务器端口必须是整数")
            return

        self._reset_runtime_state()
        session = self._new_run_session()
        self.active_session = session
        self.ui_session_id = session.run_id
        cmd = self._build_command()

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONPATH"] = str(PYTHONPATH_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(PYTHONPATH_ROOT),
                env=env,
                start_new_session=True,
            )
            session.process = process
            self.running = True
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self._set_debug_radios_enabled(False)
            self.status_label.setText("运行中")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
            self.last_status = "运行中"
            self._log("接收程序已启动", "success", session=session)
            self._log(f"阵营={self.team_var} 接收器IP={self.jam_ip}", "info", session=session)
            self._log(f"服务器={self.server_ip}:{self.server_port_str}", "info", session=session)
            self._log(f"本次运行日志将在结束后写入 {session.log_path}", "info", session=session)

            session.read_thread = threading.Thread(
                target=self._read_output_for_session, args=(session,), daemon=True
            )
            session.read_thread.start()
        except Exception as exc:
            self._log(f"启动失败: {exc}", "error", session=session)
            self.running = False
            self.active_session = None
            self.ui_session_id = None
            self._persist_log_session(session, "启动失败")

    def _stop_receiver(self):
        session = self.active_session
        if session is None:
            return

        session.stop_requested = True
        self.ui_session_id = None

        if session.process is not None:
            try:
                self._signal_process_group(session.process, signal.SIGTERM)
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_process_group(session.process, signal.SIGKILL)
                session.process.wait(timeout=5)
            except Exception as exc:
                self._log(f"停止失败: {exc}", "error")

        self.running = False
        if session.read_thread is not None and session.read_thread.is_alive():
            session.read_thread.join(timeout=2)
        if session.read_thread is not None and session.read_thread.is_alive():
            self._log("读取线程未及时退出，旧会话输出将被忽略", "warn")
            self._persist_log_session(session, "手动停止")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_debug_radios_enabled(True)
        self.status_label.setText("已停止")
        self.status_label.setStyleSheet("color: red; font-weight: bold;")
        self.last_status = "已停止"
        self.sig_update_display.emit()

    # ==================================================================
    # JSON 消息处理（与 tkinter 版完全一致）
    # ==================================================================
    def _handle_json_message(self, data: dict, session: RunSession):
        if session.run_id != self.ui_session_id:
            return
        kind = data.get("kind")
        if kind == "jam_started":
            self.last_jam_level = str(data.get("jam_level", "N/A"))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.last_profile = str(data.get("profile", "N/A"))
            freq = data.get("center_freq")
            self.last_center_freq = f"{freq} Hz" if freq is not None else "N/A"
            self._log(
                f"接收已启动: level={self.last_jam_level} mode={self.last_rx_mode} "
                f"profile={self.last_profile} freq={self.last_center_freq}",
                "success",
            )
            record_path = data.get("record_path")
            if record_path:
                self._log(f"录波文件: {record_path}", "info")
        elif kind == "jam_level_change":
            self.last_jam_level = str(data.get("jam_level", "N/A"))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.last_profile = str(data.get("profile", "N/A"))
            freq = data.get("center_freq")
            self.last_center_freq = f"{freq} Hz" if freq is not None else "N/A"
            self._log(
                f"干扰等级切换到 {self.last_jam_level}，模式={self.last_rx_mode}，"
                f"当前频段 {self.last_profile} @ {self.last_center_freq}",
                "jam",
            )
        elif kind == "jam_status":
            self.last_jam_level = str(data.get("jam_level", self.last_jam_level))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.jam_frame_count = int(data.get("jam_frame_count", self.jam_frame_count))
            self.info_frame_count = int(data.get("info_frame_count", self.info_frame_count))
            self.last_jam_key = str(data.get("last_key", self.last_jam_key))
            self.last_profile = str(data.get("profile", self.last_profile))
            positions = data.get("last_info_positions")
            if isinstance(positions, dict):
                self.last_info_positions = positions
            freq = data.get("center_freq")
            if freq is not None:
                self.last_center_freq = f"{freq} Hz"
            power = data.get("last_buffer_power_dbfs")
            self.last_power = f"{power} dBFS" if power is not None else "N/A"
        elif kind == "jam_frame":
            self.last_jam_level = str(data.get("jam_level", self.last_jam_level))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.last_jam_key = str(data.get("data_ascii", self.last_jam_key))
            self.jam_frame_count = int(data.get("jam_frame_count", self.jam_frame_count))
            self.last_profile = str(data.get("profile", self.last_profile))
            self._log(
                f"收到干扰波: level={self.last_jam_level} key={self.last_jam_key} "
                f"seq={data.get('seq', 'N/A')}",
                "jam",
                show=self.show_realtime,
            )
        elif kind == "info_frame":
            self.last_jam_level = str(data.get("jam_level", self.last_jam_level))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.info_frame_count = int(data.get("info_frame_count", self.info_frame_count))
            self.last_profile = str(data.get("profile", self.last_profile))
            decoded = data.get("decoded")
            if isinstance(decoded, dict):
                self.last_info_positions = decoded
            payload_endian = str(data.get("payload_endian", self.PAYLOAD_ENDIAN))
            self._log(
                f"收到信息波 0x0A01: endian={payload_endian} "
                f"seq={data.get('seq', 'N/A')} "
                f"hero={self._format_position(decoded, 'enemy_hero')}",
                "success",
                show=self.show_realtime,
            )
        elif kind == "jam_error":
            message = str(data.get("message", "unknown jam error"))
            detail = data.get("detail")
            error_type = data.get("error_type")
            if detail:
                if error_type:
                    message = f"{message}: {error_type}: {detail}"
                else:
                    message = f"{message}: {detail}"
            tag = "warn" if "no jam packets" in message else "error"
            self._log(message, tag)
        elif kind == "jam_stopped":
            self._log("接收进程已退出", "info")
            record_path = data.get("record_path")
            if record_path:
                record_bytes = data.get("record_bytes")
                if record_bytes is not None:
                    self._log(f"录波完成: {record_path} ({record_bytes} bytes)", "info")
                else:
                    self._log(f"录波完成: {record_path}", "info")

        self.sig_update_display.emit()

    # ==================================================================
    # 子进程输出读取（后台线程）
    # ==================================================================
    def _read_output_for_session(self, session: RunSession):
        try:
            process = session.process
            assert process is not None
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    self._log(line, "info", show=self.show_realtime, session=session)
                    continue
                self._handle_json_message(data, session)

            rc = process.wait()
            if session.stop_requested and session.run_id == self.ui_session_id:
                self._log("接收程序已停止", "success", session=session)
            exit_reason = (
                "手动停止" if session.stop_requested
                else ("已停止" if rc == 0 else f"异常退出({rc})")
            )
            self._persist_log_session(session, exit_reason)
            self.sig_process_stopped.emit(rc)
        except Exception as exc:
            self._log(f"读取输出错误: {exc}", "error", session=session)
            self._persist_log_session(session, "读取输出错误")

    def _on_process_stopped(self, rc: int):
        """子进程退出后的 UI 恢复（主线程）。"""
        self.running = False
        self.active_session = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_debug_radios_enabled(True)
        if rc == 0:
            self.status_label.setText("已停止")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.last_status = "已停止"
        else:
            self.status_label.setText(f"异常退出({rc})")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.last_status = f"异常退出({rc})"
        self.sig_update_display.emit()

    # ==================================================================
    # 显示更新
    # ==================================================================
    def _update_display(self):
        self.jam_key_label.setText(self.last_jam_key)

        level_text = self.last_jam_level
        level_color = "gray"
        if self.last_jam_level == "1":
            level_text = "1 级"
            level_color = "green"
        elif self.last_jam_level == "2":
            level_text = "2 级"
            level_color = "orange"
        elif self.last_jam_level == "3":
            level_text = "3 级"
            level_color = "red"
        self.jam_level_label.setText(level_text)
        self.jam_level_label.setStyleSheet(
            f"color: {level_color}; font-size: 16px; font-weight: bold;"
        )

        if self.last_rx_mode == "jam":
            rx_text, rx_color = "干扰波", "#ff8c00"
        elif self.last_rx_mode == "info":
            rx_text, rx_color = "信息波", "#ff8c00"
        else:
            rx_text, rx_color = self.last_rx_mode, "gray"
        self.rx_mode_label.setText(rx_text)
        self.rx_mode_label.setStyleSheet(
            f"color: {rx_color}; font-size: 16px; font-weight: bold;"
        )

        self.jam_count_label.setText(str(self.jam_frame_count))
        self.info_count_label.setText(str(self.info_frame_count))
        self.profile_label.setText(self.last_profile)
        self.freq_label.setText(self.last_center_freq)
        self.power_label.setText(self.last_power)

        for field_name, _title in self.INFO_POSITION_NAMES:
            self.info_position_labels[field_name].setText(
                self._format_position(self.last_info_positions, field_name)
            )

    @staticmethod
    def _format_position(decoded: object, field_name: str) -> str:
        if not isinstance(decoded, dict):
            return "N/A"
        entry = decoded.get(field_name)
        if not isinstance(entry, dict):
            return "N/A"
        x = entry.get("x")
        y = entry.get("y")
        if x is None or y is None:
            return "N/A"
        return f"({x}, {y}) cm"

    # ==================================================================
    # 窗口关闭
    # ==================================================================
    def closeEvent(self, event):
        if self.running:
            reply = QtWidgets.QMessageBox.question(
                self, "确认",
                "接收程序仍在运行，确定关闭？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                session = self.active_session
                self._stop_receiver()
                if session is not None:
                    self._persist_log_session(session, "GUI关闭")
                event.accept()
            else:
                event.ignore()
        else:
            if self.active_session is not None:
                self._persist_log_session(self.active_session, "GUI关闭")
            event.accept()


# ==================================================================
# 入口
# ==================================================================
def main():
    qapp = QtWidgets.QApplication(sys.argv)
    gui = JamRxGUI()
    gui.show()
    rc = qapp.exec_()
    if gui.active_session is not None:
        gui._persist_log_session(gui.active_session, "GUI退出")
    sys.exit(rc)


if __name__ == "__main__":
    main()
