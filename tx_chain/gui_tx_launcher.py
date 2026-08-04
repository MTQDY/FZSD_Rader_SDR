#!/usr/bin/env python3
"""
RM2026 无线电发射系统 GUI (PyQt5)

支持两种发射模式：
- 干扰波发射 (jam_tx_app)  — 0x0A06 干扰密钥帧
- 信息波发射 (tx_app)      — 0x0A01~0x0A05 信息帧
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]   # FZSD_RX_SDR/
TX_CHAIN_DIR = Path(__file__).resolve().parent        # FZSD_RX_SDR/tx_chain/
COMBAT_DIR = PROJECT_ROOT.parent / "CombatRadarSdr2026"  # CombatRadarSdr2026/
LAUNCH_DIR = COMBAT_DIR / "launch"
VENV_DIR = PROJECT_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python3"

# ---------------------------------------------------------------------------
# Profile 预设（来自 radio_profiles.py，避免跨项目依赖）
# ---------------------------------------------------------------------------
JAM_PROFILES: dict[str, dict] = {
    "red1":  {"center_freq": 432200000, "rf_bandwidth": 940000, "sensitivity": 2.8323, "tx_gain_db": -20.0},
    "red2":  {"center_freq": 432500000, "rf_bandwidth": 860000, "sensitivity": 2.5809, "tx_gain_db": -20.0},
    "red3":  {"center_freq": 432800000, "rf_bandwidth": 250000, "sensitivity": 0.6646, "tx_gain_db": -20.0},
    "blue1": {"center_freq": 434920000, "rf_bandwidth": 940000, "sensitivity": 2.8323, "tx_gain_db": -20.0},
    "blue2": {"center_freq": 434620000, "rf_bandwidth": 860000, "sensitivity": 2.5809, "tx_gain_db": -20.0},
    "blue3": {"center_freq": 434320000, "rf_bandwidth": 250000, "sensitivity": 0.6646, "tx_gain_db": -20.0},
}

INFO_PROFILES: dict[str, dict] = {
    "red1":  {"center_freq": 433200000, "rf_bandwidth": 540000, "sensitivity": 1.5756, "tx_gain_db": -25.0},
    "blue1": {"center_freq": 433920000, "rf_bandwidth": 540000, "sensitivity": 1.5756, "tx_gain_db": -25.0},
}

# Profile → 所属模式
JAM_PROFILE_NAMES = list(JAM_PROFILES.keys())
INFO_PROFILE_NAMES = list(INFO_PROFILES.keys())


# ---------------------------------------------------------------------------
# 主 GUI 类
# ---------------------------------------------------------------------------
class TxLauncherGUI(QtWidgets.QWidget):
    WINDOW_TITLE = "RM2026 无线电发射系统"

    sig_output = QtCore.pyqtSignal(str)  # 进程输出 → 状态区

    def __init__(self):
        super().__init__()
        self.setWindowTitle(self.WINDOW_TITLE)
        self.resize(820, 680)

        self._process: subprocess.Popen[str] | None = None
        self._running = False
        self._read_thread: threading.Thread | None = None

        self._create_ui()
        self.sig_output.connect(self._on_output)

    # ==================================================================
    # 样式表（浅蓝色主题）
    # ==================================================================
    def _apply_stylesheet(self):
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
                border: 2px solid #4a90d9;
                background: #f0f4fa;
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
            QComboBox {
                border: 1px solid #b0b0b0;
                border-radius: 6px;
                padding: 4px 8px;
                background: #fafafa;
            }
            QComboBox:focus {
                border: 2px solid #4a90d9;
            }
            QCheckBox, QRadioButton {
                spacing: 4px;
            }
        """)

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _create_ui(self):
        self._apply_stylesheet()
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setSpacing(10)

        # ---- 发射模式 ----
        mode_group = QtWidgets.QGroupBox("发射模式")
        mode_layout = QtWidgets.QHBoxLayout(mode_group)
        self.radio_jam = QtWidgets.QRadioButton("干扰波发射 (0x0A06)")
        self.radio_info = QtWidgets.QRadioButton("信息波发射 (0x0A01~0x0A05)")
        self.radio_jam.setChecked(True)
        self._mode_group = QtWidgets.QButtonGroup(self)
        self._mode_group.addButton(self.radio_jam, 0)
        self._mode_group.addButton(self.radio_info, 1)
        mode_layout.addWidget(self.radio_jam)
        mode_layout.addWidget(self.radio_info)
        mode_layout.addStretch()
        self._mode_group.buttonClicked.connect(self._on_mode_changed)
        main_layout.addWidget(mode_group)

        # ---- PlutoSDR 配置 ----
        sdr_group = QtWidgets.QGroupBox("PlutoSDR 配置")
        sdr_grid = QtWidgets.QGridLayout(sdr_group)

        sdr_grid.addWidget(QtWidgets.QLabel("发射器 IP:"), 0, 0)
        self.tx_ip_edit = QtWidgets.QLineEdit("192.168.2.1")
        sdr_grid.addWidget(self.tx_ip_edit, 0, 1)

        sdr_grid.addWidget(QtWidgets.QLabel("Profile:"), 0, 2)
        self.profile_combo = QtWidgets.QComboBox()
        self.profile_combo.currentTextChanged.connect(self._on_profile_changed)
        sdr_grid.addWidget(self.profile_combo, 0, 3)

        sdr_grid.addWidget(QtWidgets.QLabel("中心频率 (Hz):"), 1, 0)
        self.center_freq_edit = QtWidgets.QLineEdit("432200000")
        sdr_grid.addWidget(self.center_freq_edit, 1, 1)

        sdr_grid.addWidget(QtWidgets.QLabel("RF 带宽 (Hz):"), 1, 2)
        self.rf_bandwidth_edit = QtWidgets.QLineEdit("940000")
        sdr_grid.addWidget(self.rf_bandwidth_edit, 1, 3)

        sdr_grid.addWidget(QtWidgets.QLabel("灵敏度 (rad/sample):"), 2, 0)
        self.sensitivity_edit = QtWidgets.QLineEdit("2.8323")
        sdr_grid.addWidget(self.sensitivity_edit, 2, 1)

        sdr_grid.addWidget(QtWidgets.QLabel("TX 增益 (dB):"), 2, 2)
        self.tx_gain_edit = QtWidgets.QLineEdit("-20.0")
        sdr_grid.addWidget(self.tx_gain_edit, 2, 3)

        main_layout.addWidget(sdr_group)

        # ---- 通用参数 ----
        common_group = QtWidgets.QGroupBox("通用参数")
        common_grid = QtWidgets.QGridLayout(common_group)

        common_grid.addWidget(QtWidgets.QLabel("采样率 (S/s):"), 0, 0)
        self.sample_rate_edit = QtWidgets.QLineEdit("1000000")
        common_grid.addWidget(self.sample_rate_edit, 0, 1)

        common_grid.addWidget(QtWidgets.QLabel("SPS:"), 0, 2)
        self.sps_edit = QtWidgets.QLineEdit("47")
        common_grid.addWidget(self.sps_edit, 0, 3)

        common_grid.addWidget(QtWidgets.QLabel("BT:"), 1, 0)
        self.bt_edit = QtWidgets.QLineEdit("0.35")
        common_grid.addWidget(self.bt_edit, 1, 1)

        common_grid.addWidget(QtWidgets.QLabel("幅度:"), 1, 2)
        self.amplitude_edit = QtWidgets.QLineEdit("0.8")
        common_grid.addWidget(self.amplitude_edit, 1, 3)

        common_grid.addWidget(QtWidgets.QLabel("更新频率 (Hz):"), 2, 0)
        self.update_hz_edit = QtWidgets.QLineEdit("10.0")
        common_grid.addWidget(self.update_hz_edit, 2, 1)

        common_grid.addWidget(QtWidgets.QLabel("每缓冲包数:"), 2, 2)
        self.packets_per_buffer_edit = QtWidgets.QLineEdit("24")
        common_grid.addWidget(self.packets_per_buffer_edit, 2, 3)

        main_layout.addWidget(common_group)

        # ---- JAM 专属参数 ----
        self.jam_group = QtWidgets.QGroupBox("干扰波专属参数")
        jam_grid = QtWidgets.QGridLayout(self.jam_group)

        jam_grid.addWidget(QtWidgets.QLabel("密钥 (空=随机):"), 0, 0)
        self.key_edit = QtWidgets.QLineEdit("")
        jam_grid.addWidget(self.key_edit, 0, 1)

        jam_grid.addWidget(QtWidgets.QLabel("密钥轮换频率 (Hz):"), 0, 2)
        self.key_rotate_edit = QtWidgets.QLineEdit("0.0")
        jam_grid.addWidget(self.key_rotate_edit, 0, 3)

        jam_grid.addWidget(QtWidgets.QLabel("推送速率 (byte/s):"), 1, 0)
        self.push_rate_edit = QtWidgets.QLineEdit("1350")
        jam_grid.addWidget(self.push_rate_edit, 1, 1)

        main_layout.addWidget(self.jam_group)

        # ---- 控制栏 ----
        ctrl_layout = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("启动发射")
        self.stop_btn = QtWidgets.QPushButton("停止发射")
        self.stop_btn.setEnabled(False)

        self.status_label = QtWidgets.QLabel("未运行")
        self.status_label.setStyleSheet("color: gray; font-weight: bold;")

        ctrl_layout.addWidget(self.start_btn)
        ctrl_layout.addWidget(self.stop_btn)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.status_label)
        main_layout.addLayout(ctrl_layout)

        self.start_btn.clicked.connect(self._start_tx)
        self.stop_btn.clicked.connect(self._stop_tx)

        # ---- 输出（调试用） ----
        out_group = QtWidgets.QGroupBox("运行输出")
        out_layout = QtWidgets.QVBoxLayout(out_group)
        self.output_text = QtWidgets.QPlainTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setMaximumBlockCount(2000)
        self.output_text.setStyleSheet("font-family: monospace; font-size: 11px;")
        out_layout.addWidget(self.output_text)
        main_layout.addWidget(out_group, 1)

        # 初始化 profile 列表
        self._on_mode_changed()

    # ==================================================================
    # 模式切换
    # ==================================================================
    def _on_mode_changed(self):
        is_jam = self.radio_jam.isChecked()
        profiles = JAM_PROFILE_NAMES if is_jam else INFO_PROFILE_NAMES
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(["手动"] + profiles)
        self.profile_combo.setCurrentIndex(0)
        self.profile_combo.blockSignals(False)

        self.jam_group.setVisible(is_jam)
        # 从 profile 加载默认值
        default_profile = profiles[0]
        self._apply_profile(default_profile)

    def _on_profile_changed(self, name: str):
        if name == "手动":
            return
        self._apply_profile(name)

    def _apply_profile(self, name: str):
        is_jam = self.radio_jam.isChecked()
        profiles = JAM_PROFILES if is_jam else INFO_PROFILES
        preset = profiles.get(name)
        if preset is None:
            return
        self.center_freq_edit.setText(str(preset["center_freq"]))
        self.rf_bandwidth_edit.setText(str(preset["rf_bandwidth"]))
        self.sensitivity_edit.setText(str(preset["sensitivity"]))
        self.tx_gain_edit.setText(str(preset["tx_gain_db"]))

    # ==================================================================
    # 虚拟环境自动准备
    # ==================================================================
    def _ensure_venv(self) -> str:
        """确保 .venv 存在且安装了必要依赖，返回 python3 路径。"""
        if VENV_PYTHON.exists():
            return str(VENV_PYTHON)

        self.sig_output.emit("[GUI] 首次运行，正在创建虚拟环境...")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.sig_output.emit("[GUI] 虚拟环境创建成功，正在安装 pyadi-iio...")
            subprocess.run(
                [str(VENV_PYTHON), "-m", "pip", "install", "pyadi-iio"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.sig_output.emit("[GUI] pyadi-iio 安装完成，准备启动。")
            return str(VENV_PYTHON)
        except subprocess.CalledProcessError as exc:
            self.sig_output.emit(f"[GUI] 环境准备失败: {exc.stderr}")
            raise

    # ==================================================================
    # 启动 / 停止
    # ==================================================================
    def _start_tx(self):
        cmd = self._build_command()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # TX 脚本依赖 CombatRadarSdr2026 下的模块：phy, protocol, radio_profiles, message_value_generate
        pythonpath = os.pathsep.join([
            str(COMBAT_DIR),
            str(LAUNCH_DIR),
            str(PROJECT_ROOT.parent),
            env.get("PYTHONPATH", ""),
        ])
        env["PYTHONPATH"] = pythonpath

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(COMBAT_DIR),
                env=env,
                start_new_session=True,
            )
            self._running = True
            self._read_thread = threading.Thread(target=self._read_output, daemon=True)
            self._read_thread.start()
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self._set_params_enabled(False)
            self.status_label.setText("发射中")
            self.status_label.setStyleSheet("color: #4a90d9; font-weight: bold;")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "错误", f"启动失败: {exc}")

    def _read_output(self):
        """后台读取子进程 stdout，通过信号投递到主线程。"""
        try:
            assert self._process and self._process.stdout
            for line in iter(self._process.stdout.readline, ""):
                if not line:
                    break
                self.sig_output.emit(line.rstrip())
        except Exception:
            pass

    def _on_output(self, text: str):
        self.output_text.appendPlainText(text)

    def _stop_tx(self):
        if self._process is None or self._process.poll() is not None:
            self._reset_ui_state()
            return

        try:
            os.killpg(self._process.pid, signal.SIGTERM)
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self._process.pid, signal.SIGKILL)
            self._process.wait(timeout=5)
        except ProcessLookupError:
            pass

        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=2)
        self._reset_ui_state()

    def _reset_ui_state(self):
        self._running = False
        self._process = None
        self._read_thread = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_params_enabled(True)
        self.status_label.setText("已停止")
        self.status_label.setStyleSheet("color: gray; font-weight: bold;")

    def _set_params_enabled(self, enabled: bool):
        self.radio_jam.setEnabled(enabled)
        self.radio_info.setEnabled(enabled)

    # ==================================================================
    # 命令构建
    # ==================================================================
    def _build_command(self) -> list[str]:
        is_jam = self.radio_jam.isChecked()
        script = TX_CHAIN_DIR / ("jam_tx_app.py" if is_jam else "tx_app.py")

        profile = self.profile_combo.currentText()
        use_profile = profile != "手动"

        try:
            python_exe = self._ensure_venv()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "环境错误", f"虚拟环境准备失败:\n{exc}")
            raise

        cmd = [
            python_exe, "-u", str(script),
            "--tx-ip", self.tx_ip_edit.text().strip(),
            "--center-freq", self.center_freq_edit.text().strip(),
            "--sample-rate", self.sample_rate_edit.text().strip(),
            "--sps", self.sps_edit.text().strip(),
            "--bt", self.bt_edit.text().strip(),
            "--sensitivity", self.sensitivity_edit.text().strip(),
            "--rf-bandwidth", self.rf_bandwidth_edit.text().strip(),
            "--tx-gain-db", self.tx_gain_edit.text().strip(),
            "--amplitude", self.amplitude_edit.text().strip(),
            "--update-hz", self.update_hz_edit.text().strip(),
            "--packets-per-buffer", self.packets_per_buffer_edit.text().strip(),
        ]

        if use_profile:
            cmd += ["--profile", profile]

        if is_jam:
            key = self.key_edit.text().strip().upper()
            if key:
                cmd += ["--key", key]
            khz = self.key_rotate_edit.text().strip()
            if khz and float(khz) > 0:
                cmd += ["--key-rotate-hz", khz]
            cmd += ["--push-rate", self.push_rate_edit.text().strip()]

        return cmd

    # ==================================================================
    # 窗口关闭
    # ==================================================================
    def closeEvent(self, event):
        if self._running:
            reply = QtWidgets.QMessageBox.question(
                self, "确认",
                "发射程序仍在运行，确定关闭？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                self._stop_tx()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# ==================================================================
# 入口
# ==================================================================
def main():
    qapp = QtWidgets.QApplication(sys.argv)
    gui = TxLauncherGUI()
    gui.show()
    sys.exit(qapp.exec_())


if __name__ == "__main__":
    main()
