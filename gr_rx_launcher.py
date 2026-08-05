#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gr_rx_launcher.py — GNU Radio 流式 RX 启动器

核心差异: 信号处理链 (FM解调→GFSK解调→接入码检测) 由 GNU Radio 流式 block 完成,
应用层逻辑 (置信度评分、协议帧重组、端序检测、服务器通信) 保持不变。

用法:
  直接去GUI启动最快。
  使用时需要安装虚拟环境！
  source /你的文件前路径/FZSD_RX_SDR/.venv/bin/activate
  pip install numpy pyadi-iio pylibiio
"""

from __future__ import annotations

import argparse
import json
import os
import select
import string
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

# ---- 从本地模块导入 (不再依赖 CombatRadarSdr2026) ----
try:
    from .gr_rx_utils import CMD_0A01, CMD_0A06, SOF
    from .rx_tools import crc8_maxim, crc16_ibm
    from .radio_profiles import INFO_PROFILES, JAM_PROFILES, RadioProfile
    from .server_communication import RadarServerComm
except ImportError as e:
    print(f"ERROR: Cannot import FZSD_RX_SDR modules.")
    print(f"  Details: {e}")
    sys.exit(1)

from .grc_rx_chain.gr_rx_chain import RxChain
from .gr_protocol_parser import (
    ParsedFrame,
    ProtocolStreamReassembler,
    decode_cmd,
)

# ---- 常量 (与 jam_rx_app.py 保持一致) ----
ALLOWED_JAM_CHARS = set(string.ascii_uppercase + string.digits)
AIR_PAYLOAD_BYTES = 15
JAM_KEY_DATA_BYTES = 6
JAM_FRAME_BYTES = 15
TEAM_CHOICES = ("red", "blue")
LEVEL_CHOICES = (1, 2, 3)
INFO_MODE_LEVEL = 3
RX_MODE_JAM = "jam"
RX_MODE_INFO = "info"
TEAM_LEVEL_TO_JAM_PROFILE = {
    ("red", 1): "red1", ("red", 2): "red2", ("red", 3): "red3",
    ("blue", 1): "blue1", ("blue", 2): "blue2", ("blue", 3): "blue3",
}
TEAM_TO_INFO_PROFILE = {"red": "red1", "blue": "blue1"}
INFO_FIELD_BOUNDS = {"x": 2800, "y": 1500}
PARSE_POLICY_CHOICES = ("default", "info_only", "onekey_then_info")
PROJECT_ROOT = Path(__file__).resolve().parents[1] / "CombatRadarSdr2026"
DEFAULT_RECORD_DIR = PROJECT_ROOT / "radio_logs"


# ---- 数据结构 ----
@dataclass
class ReceiverState:
    team: str
    level: int
    rx_mode: str
    profile_name: str
    center_freq: int
    rf_bandwidth: int
    sensitivity: float
    jam_frame_count: int = 0
    info_frame_count: int = 0
    last_key: str = "N/A"
    last_seq: int | None = None
    last_info_seq: int | None = None
    last_best_jam_dist: int | None = None
    last_best_info_dist: int | None = None
    last_scan_offset: int | None = None
    last_confidence: float | None = None
    last_frame_hex: str = ""
    last_frame_ts: float | None = None
    last_info_positions: dict[str, dict[str, int]] | None = None
    last_info_frame_hex: str = ""
    last_info_frame_ts: float | None = None
    no_packet_streak: int = 0
    last_buffer_power_dbfs: float | None = None
    last_buffer_packets: int = 0

    def to_status(self, rx_ip: str, server_connected: bool) -> dict[str, Any]:
        return {
            "kind": "jam_status", "ts": time.time(), "rx_ip": rx_ip,
            "team": self.team, "jam_level": self.level, "rx_mode": self.rx_mode,
            "profile": self.profile_name, "center_freq": self.center_freq,
            "rf_bandwidth": self.rf_bandwidth, "sensitivity": self.sensitivity,
            "jam_frame_count": self.jam_frame_count,
            "info_frame_count": self.info_frame_count,
            "last_key": self.last_key, "last_seq": self.last_seq,
            "last_info_seq": self.last_info_seq,
            "last_best_jam_dist": self.last_best_jam_dist,
            "last_best_info_dist": self.last_best_info_dist,
            "last_scan_offset": self.last_scan_offset,
            "last_confidence": self.last_confidence,
            "last_frame_hex": self.last_frame_hex,
            "last_frame_ts": self.last_frame_ts,
            "last_info_positions": self.last_info_positions,
            "last_info_frame_hex": self.last_info_frame_hex,
            "last_info_frame_ts": self.last_info_frame_ts,
            "no_packet_streak": self.no_packet_streak,
            "last_buffer_power_dbfs": self.last_buffer_power_dbfs,
            "last_buffer_packets": self.last_buffer_packets,
            "server_connected": server_connected,
        }


@dataclass
class StrictInfoCycleFilter:
    last_seq: int | None = None

    def accept(self, seq: int) -> bool:
        self.last_seq = seq
        return True


# ---- 工具函数 ----
def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def emit_error(message: str, **extra: Any) -> None:
    payload = {"kind": "jam_error", "ts": time.time(), "message": message}
    payload.update(extra)
    emit_json(payload)


def ascii_score(data: bytes) -> float:
    if len(data) != JAM_KEY_DATA_BYTES:
        return 0.0
    valid = sum(chr(b) in ALLOWED_JAM_CHARS for b in data)
    return valid / JAM_KEY_DATA_BYTES


def jam_confidence(best_jam_dist: int, crc_ok: bool, data: bytes) -> float:
    access_conf = max(0.0, 1.0 - (best_jam_dist / 64.0))
    crc_conf = 1.0 if crc_ok else 0.0
    key_conf = ascii_score(data)
    return 0.45 * access_conf + 0.40 * crc_conf + 0.15 * key_conf


def check_air_packet(packet: dict[str, Any], expected_kind: str) -> tuple[bool, str]:
    payload = packet.get("payload", b"")
    if packet.get("kind") != expected_kind:
        return False, f"not {expected_kind} packet"
    if int(packet.get("len1", -1)) != AIR_PAYLOAD_BYTES:
        return False, f"len1={packet.get('len1')} != 15"
    if int(packet.get("len2", -1)) != AIR_PAYLOAD_BYTES:
        return False, f"len2={packet.get('len2')} != 15"
    if len(payload) != AIR_PAYLOAD_BYTES:
        return False, f"payload_len={len(payload)} != 15"
    return True, "ok"


def extract_jam_0a06_frames(payload30: bytes) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if len(payload30) != AIR_PAYLOAD_BYTES * 2:
        return matches
    for start in range(len(payload30) - JAM_FRAME_BYTES + 1):
        if payload30[start] != SOF:
            continue
        frame = payload30[start: start + JAM_FRAME_BYTES]
        data_len = int.from_bytes(frame[1:3], "little")
        if data_len != JAM_KEY_DATA_BYTES:
            continue
        if crc8_maxim(frame[0:4]) != frame[4]:
            continue
        cmd_id = int.from_bytes(frame[5:7], "little")
        if cmd_id != CMD_0A06:
            continue
        crc16_expected = int.from_bytes(frame[13:15], "little")
        if crc16_ibm(frame[0:13]) != crc16_expected:
            continue
        matches.append({
            "start": start, "seq": frame[3], "cmd_id": cmd_id,
            "data": bytes(frame[7:13]), "frame_hex": frame.hex().upper(),
        })
    return matches


def info_positions_out_of_bounds(decoded: dict[str, Any]) -> bool:
    for value in decoded.values():
        if not isinstance(value, dict):
            continue
        x_value = value.get("x")
        y_value = value.get("y")
        if isinstance(x_value, int) and x_value > INFO_FIELD_BOUNDS["x"]:
            return True
        if isinstance(y_value, int) and y_value > INFO_FIELD_BOUNDS["y"]:
            return True
    return False


def encode_cmd_0a01_payload(decoded: dict[str, Any], payload_endian: str) -> bytes:
    ordered_names = (
        "enemy_hero", "enemy_engineer", "enemy_infantry3",
        "enemy_infantry4", "enemy_air", "enemy_sentinel",
    )
    body = bytearray()
    for name in ordered_names:
        item = decoded.get(name) or {}
        x_value = int(item.get("x", 0))
        y_value = int(item.get("y", 0))
        body.extend(x_value.to_bytes(2, payload_endian, signed=False))
        body.extend(y_value.to_bytes(2, payload_endian, signed=False))
    return bytes(body)


def should_use_info_mode(level: int, parse_policy: str, info_mode_locked: bool) -> bool:
    if parse_policy == "info_only":
        return True
    if parse_policy == "onekey_then_info":
        return info_mode_locked or level >= 2
    return level >= INFO_MODE_LEVEL


def get_receiver_profile(
    team: str, level: int, parse_policy: str = "default", info_mode_locked: bool = False,
) -> tuple[str, str, RadioProfile]:
    if should_use_info_mode(level, parse_policy, info_mode_locked):
        profile_name = TEAM_TO_INFO_PROFILE[team]
        return RX_MODE_INFO, profile_name, INFO_PROFILES[profile_name]
    profile_name = TEAM_LEVEL_TO_JAM_PROFILE[(team, level)]
    return RX_MODE_JAM, profile_name, JAM_PROFILES[profile_name]


# ---- 应用层回调处理器 ----
class ApplicationHandler:
    """
    处理从 GNU Radio 协议解析器收到的帧, 实现与 jam_rx_app.py 主循环
    完全相同的应用逻辑: 置信度评分、级别切换、服务器通信、JSON 输出。
    """

    def __init__(
        self,
        state: ReceiverState,
        args: argparse.Namespace,
        server_comm: RadarServerComm | None,
        rx_chain: RxChain,
    ):
        self.state = state
        self.args = args
        self.server_comm = server_comm
        self.rx_chain = rx_chain

        # Jam 模式状态
        self.prev_jam_payload: bytes | None = None
        self.last_emitted_jam_frame_hex: str | None = None
        self.last_status_emit: float = 0.0

        # Info 模式状态
        self.stream = ProtocolStreamReassembler(max_buffer=16384)
        self.cycle_filter = StrictInfoCycleFilter()
        self.current_payload_endian = "little" if args.payload_endian == "auto" else args.payload_endian

        # 级别切换
        self.pending_level = state.level
        self.info_mode_locked = (
            args.parse_policy == "onekey_then_info" and args.initial_level >= 2
        )

        # 自动重启: 5 秒无包时触发
        self.last_packet_time: float = time.time()
        self.restart_count: int = 0

    # ---- 空中包回调入口 ----
    def on_packets(self, packets: list[dict], ts: float) -> None:
        """GNU Radio 协议解析器回调: 收到一组空中包

        packets: 字典列表, 每项 {kind, payload, len1, len2, pos, best_info_dist, best_jam_dist}
        ts:      时间戳
        """
        now = ts if ts > 0 else time.time()
        self.last_packet_time = now  # 收到包就刷新时间戳（无论是否有效）

        # 诊断: 输出每次回调的包检测统计
        if not self.args.quiet and packets:
            jam_n = sum(1 for p in packets if p["kind"] == "JAM")
            info_n = sum(1 for p in packets if p["kind"] == "INFO")
            valid_n = sum(1 for p in packets if p["valid"])
            best_dist = min((p.get("best_info_dist", 64) for p in packets), default=64)
            emit_json({"kind": "diag_packets", "ts": now, "total": len(packets),
                       "jam": jam_n, "info": info_n, "valid": valid_n,
                       "best_access_dist": best_dist, "mode": self.state.rx_mode})

        if self.state.rx_mode == RX_MODE_JAM:
            self._handle_jam_packets(packets, now)
        else:
            self._handle_info_packets(packets, now)

        # 定期状态报告
        if now - self.last_status_emit >= self.args.status_interval:
            self.last_status_emit = now
            emit_json(self.state.to_status(
                self.args.rx_ip,
                server_connected=bool(self.server_comm and self.server_comm.connected),
            ))

    def _handle_jam_packets(self, packets: list[dict], now: float) -> None:
        """处理干扰波模式下的空中包 — 与 jam_rx_app.py jam 分支完全一致"""
        # 按 jam_count 选择最佳候选偏移策略
        # (流式模式下无法做多候选比较, 取 best_jam_dist 最小的)
        jam_packets = [p for p in packets if p["kind"] == "JAM" and p["valid"]]
        if not jam_packets:
            self.state.no_packet_streak += 1
            self.state.last_buffer_packets = 0
            self.prev_jam_payload = None
            return

        best_jam_dist = min(p["best_jam_dist"] for p in jam_packets)
        self.state.no_packet_streak = 0
        self.state.last_buffer_packets = len(jam_packets)
        self.state.last_best_jam_dist = best_jam_dist
        saw_valid_jam_payload = False

        for packet in jam_packets:
            air_ok, air_reason = check_air_packet(packet, expected_kind="JAM")
            if not air_ok:
                self.prev_jam_payload = None
                if not self.args.quiet:
                    emit_error("drop invalid jam air packet", reason=air_reason,
                               payload_hex=packet.get("payload", b"").hex().upper())
                continue

            saw_valid_jam_payload = True
            matches: list[dict[str, Any]] = []
            if self.prev_jam_payload is not None:
                matches = extract_jam_0a06_frames(self.prev_jam_payload + packet["payload"])
            self.prev_jam_payload = packet["payload"]

            for match in matches:
                if match["frame_hex"] == self.last_emitted_jam_frame_hex:
                    continue

                data = bytes(match["data"])
                confidence = jam_confidence(best_jam_dist, True, data)
                if confidence < self.args.confidence_threshold:
                    if not self.args.quiet:
                        emit_error("drop low confidence jam frame",
                                   confidence=round(confidence, 4),
                                   frame_hex=match["frame_hex"])
                    continue

                self.last_emitted_jam_frame_hex = match["frame_hex"]
                self.state.jam_frame_count += 1
                self.state.last_key = data.decode("ascii", errors="replace")
                self.state.last_seq = int(match["seq"])
                self.state.last_best_jam_dist = best_jam_dist
                self.state.last_scan_offset = int(match["start"])
                self.state.last_confidence = round(confidence, 4)
                self.state.last_frame_hex = match["frame_hex"]
                self.state.last_frame_ts = time.time()

                if self.server_comm is not None and self.server_comm.connected:
                    if not self.server_comm.send_jam_key(data) and not self.args.quiet:
                        emit_error("failed sending jam key to radar server")

                emit_json(self._build_jam_frame_payload(data, match, confidence, best_jam_dist))

        if not saw_valid_jam_payload:
            self.prev_jam_payload = None

    def _handle_info_packets(self, packets: list[dict], now: float) -> None:
        """处理信息波模式下的空中包 — 与 jam_rx_app.py info 分支完全一致"""
        info_packets = [p for p in packets if p["kind"] == "INFO" and p["valid"]]
        if not info_packets:
            self.state.no_packet_streak += 1
            self.state.last_buffer_packets = 0
            return

        self.state.no_packet_streak = 0
        self.state.last_buffer_packets = len(info_packets)

        best_info_dist = min(p["best_info_dist"] for p in info_packets)
        self.state.last_best_info_dist = best_info_dist

        for packet in info_packets:
            air_ok, air_reason = check_air_packet(packet, expected_kind="INFO")
            if not air_ok:
                if packet.get("kind") == "INFO" and not self.args.quiet:
                    emit_error("drop invalid info air packet", reason=air_reason,
                               payload_hex=packet.get("payload", b"").hex().upper())
                continue
            self.stream.append_payload(packet["payload"])

        for frame in self.stream.extract_frames(ts=now):
            if self.args.strict_cycle and not self.cycle_filter.accept(frame.seq):
                continue

            decoded_endian = self.current_payload_endian
            frame.decoded = decode_cmd(frame.cmd_id, frame.data, payload_endian=decoded_endian)

            # 自动端序检测
            if (
                frame.cmd_id == CMD_0A01
                and self.args.payload_endian == "auto"
                and decoded_endian == "little"
                and info_positions_out_of_bounds(frame.decoded)
            ):
                self.current_payload_endian = "big"
                decoded_endian = "big"
                frame.decoded = decode_cmd(frame.cmd_id, frame.data, payload_endian=decoded_endian)
                if not self.args.quiet:
                    emit_error(
                        "0x0A01 little-endian out of bounds, switched to big-endian",
                        cmd_id="0x0A01", payload_endian=decoded_endian,
                        data_hex=frame.data.hex().upper(),
                    )

            if frame.cmd_id != CMD_0A01:
                continue

            self.state.info_frame_count += 1
            self.state.last_info_seq = frame.seq
            self.state.last_info_positions = frame.decoded
            self.state.last_info_frame_hex = frame.data.hex().upper()
            self.state.last_info_frame_ts = frame.ts

            normalized_frame_data = (
                encode_cmd_0a01_payload(frame.decoded, "little")
                if decoded_endian == "big"
                else frame.data
            )
            if self.server_comm is not None and self.server_comm.connected:
                if not self.server_comm.send_command_data(CMD_0A01, normalized_frame_data) and not self.args.quiet:
                    emit_error("failed sending 0x0A01 to radar server")

            emit_json(self._build_info_frame_payload(frame, decoded_endian))

    # ---- 级别切换 ----
    def on_jam_level_change(self, level: int) -> None:
        self.pending_level = level
        if not self.args.quiet:
            emit_error("jam level change requested", jam_level=level)

    def apply_pending_level_change(self) -> bool:
        """应用待处理的级别变更。返回 True 表示发生了切换。"""
        if self.pending_level == self.state.level:
            return False

        if self.args.parse_policy == "onekey_then_info" and self.pending_level >= 2:
            self.info_mode_locked = True

        rx_mode, profile_name, profile = get_receiver_profile(
            self.state.team, self.pending_level,
            parse_policy=self.args.parse_policy,
            info_mode_locked=self.info_mode_locked,
        )

        prev_rx_mode = self.state.rx_mode
        self.state.level = self.pending_level
        self.state.rx_mode = rx_mode
        self.state.profile_name = profile_name
        self.state.center_freq = profile.center_freq
        self.state.rf_bandwidth = profile.rf_bandwidth
        self.state.sensitivity = profile.sensitivity

        should_reconfigure = (
            prev_rx_mode != self.state.rx_mode
            or self.state.center_freq != profile.center_freq
        )

        if should_reconfigure:
            self.state.last_best_jam_dist = None
            self.state.last_best_info_dist = None
            self.state.last_scan_offset = None
            self.state.no_packet_streak = 0
            self.current_payload_endian = "little" if self.args.payload_endian == "auto" else self.args.payload_endian
            self.prev_jam_payload = None
            self.last_emitted_jam_frame_hex = None
            self.stream = ProtocolStreamReassembler(max_buffer=16384)
            self.cycle_filter = StrictInfoCycleFilter()

            # 重配置 GNU Radio 链
            self.rx_chain.reconfigure_for_profile(
                center_freq=profile.center_freq,
                rf_bandwidth=profile.rf_bandwidth,
                sensitivity=profile.sensitivity,
            )
            self.rx_chain.set_allow_jam(rx_mode == RX_MODE_JAM)
            self.rx_chain.set_info_only(rx_mode == RX_MODE_INFO)

        emit_json({
            "kind": "jam_level_change", "ts": time.time(),
            "team": self.state.team, "jam_level": self.state.level,
            "rx_mode": self.state.rx_mode, "profile": self.state.profile_name,
            "center_freq": self.state.center_freq,
            "rf_bandwidth": self.state.rf_bandwidth,
            "sensitivity": self.state.sensitivity,
        })
        return True

    # ---- JSON 构建 ----
    def _build_jam_frame_payload(self, data: bytes, match: dict, confidence: float, best_jam_dist: int) -> dict:
        decoded = decode_cmd(CMD_0A06, data)
        return {
            "kind": "jam_frame", "ts": time.time(),
            "team": self.state.team, "jam_level": self.state.level,
            "rx_mode": self.state.rx_mode, "profile": self.state.profile_name,
            "cmd_id": "0x0A06", "seq": match["seq"],
            "data_hex": data.hex().upper(),
            "data_ascii": data.decode("ascii", errors="replace"),
            "decoded": decoded, "confidence": round(confidence, 4),
            "best_jam_dist": best_jam_dist,
            "scan_offset": match["start"],
            "frame_hex": match["frame_hex"],
            "jam_frame_count": self.state.jam_frame_count,
        }

    def _build_info_frame_payload(self, frame: ParsedFrame, payload_endian: str) -> dict:
        return {
            "kind": "info_frame", "ts": time.time(),
            "team": self.state.team, "jam_level": self.state.level,
            "rx_mode": self.state.rx_mode, "profile": self.state.profile_name,
            "cmd_id": "0x0A01", "seq": frame.seq,
            "data_hex": frame.data.hex().upper(),
            "decoded": frame.decoded,
            "payload_endian": payload_endian,
            "best_info_dist": frame.best_info_dist,
            "scan_offset": 0,
            "info_frame_count": self.state.info_frame_count,
        }


# ---- 录波器 ----
@dataclass
class WaveRecorder:
    record_dir: Path
    record_tag: str
    enabled: bool = False
    path: Path | None = None
    meta_path: Path | None = None
    handle: BinaryIO | None = None
    bytes_written: int = 0
    buffers_written: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def start(self, metadata: dict[str, Any]) -> tuple[Path, Path]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in self.record_tag.strip())
        cleaned = cleaned.strip("_") or "jam_rx"
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.record_dir / f"{cleaned}_{stamp}.c64"
        self.meta_path = self.record_dir / f"{cleaned}_{stamp}.json"
        self.handle = self.path.open("wb")
        self.metadata = dict(metadata)
        self.metadata.update({
            "record_path": str(self.path), "record_meta_path": str(self.meta_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "format": "raw_iq_complex64", "dtype": "complex64",
            "byteorder": sys.byteorder,
        })
        self._write_metadata(status="recording")
        return self.path, self.meta_path

    def write(self, iq: np.ndarray) -> None:
        if self.handle is None:
            return
        chunk = np.asarray(iq, dtype=np.complex64)
        self.handle.write(chunk.tobytes())
        self.bytes_written += chunk.nbytes
        self.buffers_written += 1

    def close(self, status: str, **extra: Any) -> None:
        if extra:
            self.metadata.update(extra)
        if self.handle is not None:
            self.handle.flush()
            self.handle.close()
            self.handle = None
        if self.path is not None and self.meta_path is not None:
            self._write_metadata(status=status)

    def summary(self) -> dict[str, Any]:
        payload = {
            "record_bytes": self.bytes_written,
            "record_buffers": self.buffers_written,
            "record_samples": self.bytes_written // np.dtype(np.complex64).itemsize,
        }
        if self.path is not None:
            payload["record_path"] = str(self.path)
        if self.meta_path is not None:
            payload["record_meta_path"] = str(self.meta_path)
        return payload

    def _write_metadata(self, status: str) -> None:
        if self.meta_path is None:
            return
        payload = dict(self.metadata)
        payload.update({
            "status": status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "record_bytes": self.bytes_written,
            "record_buffers": self.buffers_written,
            "record_samples": self.bytes_written // np.dtype(np.complex64).itemsize,
        })
        self.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ================================================================
# 主入口
# ================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="RM2026 GNU Radio streaming JAM/INFO RX")
    parser.add_argument("--rx-ip", default="192.168.2.1")
    parser.add_argument("--team", choices=TEAM_CHOICES, default="red")
    parser.add_argument("--initial-level", type=int, choices=LEVEL_CHOICES, default=1)
    parser.add_argument("--sample-rate", type=int, default=1_000_000)
    parser.add_argument("--sps", type=int, default=47)
    parser.add_argument("--bt", type=float, default=0.35)
    parser.add_argument("--rx-gain-db", type=float, default=50.0)
    parser.add_argument("--access-bit-errors", type=int, default=1)
    parser.add_argument("--confidence-threshold", type=float, default=0.85)
    parser.add_argument("--server-ip", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=5000)
    parser.add_argument("--no-server-comm", action="store_true")
    parser.add_argument("--status-interval", type=float, default=0.5)
    parser.add_argument("--strict-cycle", action="store_true")
    parser.add_argument("--parse-policy", choices=PARSE_POLICY_CHOICES, default="default")
    parser.add_argument("--payload-endian", choices=("little", "big", "auto"), default="little")
    parser.add_argument("--record-wave", action="store_true")
    parser.add_argument("--record-dir", default=str(DEFAULT_RECORD_DIR))
    parser.add_argument("--record-tag", default="jam_rx")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    # ---- 初始 profile ----
    rx_mode, profile_name, profile = get_receiver_profile(
        args.team, args.initial_level,
        parse_policy=args.parse_policy,
        info_mode_locked=(args.parse_policy == "onekey_then_info" and args.initial_level >= 2),
    )
    state = ReceiverState(
        team=args.team, level=args.initial_level, rx_mode=rx_mode,
        profile_name=profile_name, center_freq=profile.center_freq,
        rf_bandwidth=profile.rf_bandwidth, sensitivity=profile.sensitivity,
    )

    # ---- 服务器通信 ----
    server_comm: RadarServerComm | None = None
    if not args.no_server_comm:
        server_comm = RadarServerComm(
            server_ip=args.server_ip,
            server_port=args.server_port,
            on_jam_level_change=None,  # 稍后设置
        )
        if server_comm.connect():
            server_comm.start()
        else:
            emit_error("failed to connect radar server",
                       server_ip=args.server_ip, server_port=args.server_port)
            server_comm = None

    # ---- 录波器 ----
    wave_recorder: WaveRecorder | None = None
    record_summary: dict[str, Any] = {}
    if args.record_wave:
        wave_recorder = WaveRecorder(
            record_dir=Path(args.record_dir).expanduser(),
            record_tag=args.record_tag, enabled=True,
        )
        try:
            wave_recorder.start({
                "launcher_tag": args.record_tag, "rx_ip": args.rx_ip,
                "team": args.team, "initial_level": args.initial_level,
                "parse_policy": args.parse_policy,
                "payload_endian_mode": args.payload_endian,
                "sample_rate": args.sample_rate,
                "rx_gain_db": args.rx_gain_db, "sps": args.sps, "bt": args.bt,
                "center_freq": state.center_freq,
                "rf_bandwidth": state.rf_bandwidth,
                "profile": state.profile_name,
            })
            record_summary = wave_recorder.summary()
        except Exception as exc:
            emit_error("failed to start wave recording", detail=str(exc))
            wave_recorder.close(status="failed", failure_reason=str(exc))
            wave_recorder = None

    # ---- GNU Radio 流式链 ----
    handler = ApplicationHandler(state, args, server_comm, None)
    if server_comm is not None:
        server_comm.on_jam_level_change = handler.on_jam_level_change

    def _build_rx_chain():
        """创建 RxChain, 使用当前 state 中的参数"""
        return RxChain(
            rx_ip=args.rx_ip,
            center_freq=state.center_freq,
            sample_rate=args.sample_rate,
            rf_bandwidth=state.rf_bandwidth,
            rx_gain_db=args.rx_gain_db,
            sps=args.sps,
            bt=args.bt,
            sensitivity=state.sensitivity,
            max_access_bit_errors=args.access_bit_errors,
            allow_jam=(state.rx_mode == RX_MODE_JAM),
            info_only=(state.rx_mode == RX_MODE_INFO),
            on_packets=handler.on_packets,
        )

    rx_chain = _build_rx_chain()
    handler.rx_chain = rx_chain

    # ---- 启动 ----
    emit_json({
        "kind": "jam_started", "ts": time.time(),
        "rx_ip": args.rx_ip, "team": state.team,
        "jam_level": state.level, "rx_mode": state.rx_mode,
        "profile": state.profile_name,
        "center_freq": state.center_freq,
        "rf_bandwidth": state.rf_bandwidth,
        "sensitivity": state.sensitivity,
        "parse_policy": args.parse_policy,
        "payload_endian_mode": args.payload_endian,
        "engine": "gnuradio_streaming",
        **record_summary,
    })
    emit_json(state.to_status(args.rx_ip, server_connected=bool(server_comm and server_comm.connected)))

    final_status = "completed"
    try:
        rx_chain.start()

        # 主循环: 检查级别切换、键盘输入、状态上报、自动重启、保持存活
        print("按 1/2/3 模拟干扰等级切换, 按 q 退出。5秒无包自动重启流图。", flush=True)
        while True:
            now = time.time()

            # 检查键盘输入 (非阻塞)
            if sys.stdin in select.select([sys.stdin], [], [], 0.05)[0]:
                line = sys.stdin.readline().strip()
                if line == 'q':
                    break
                if line in ('1', '2', '3'):
                    handler.on_jam_level_change(int(line))

            # 检查服务器下发的级别切换
            if handler.pending_level != state.level:
                handler.apply_pending_level_change()

            # 定时状态上报
            if handler.last_status_emit == 0 or now - handler.last_status_emit >= args.status_interval:
                handler.last_status_emit = now
                emit_json(state.to_status(
                    args.rx_ip,
                    server_connected=bool(server_comm and server_comm.connected),
                ))

            # 自动重启: 5 秒无空中包 → execv 原地重启进程
            if now - handler.last_packet_time > 5.0:
                handler.restart_count += 1
                if not args.quiet:
                    emit_error("auto-restart process (execv)",
                               restart_count=handler.restart_count,
                               idle_seconds=round(now - handler.last_packet_time, 1))
                emit_json({"kind": "process_restarting", "ts": now,
                           "restart_count": handler.restart_count})
                sys.stdout.flush()
                # 重建命令行: 保持当前 profile 参数, 用 -m 运行
                new_argv = [
                    sys.executable, "-u", "-m", "FZSD_RX_SDR.gr_rx_launcher",
                    "--rx-ip", args.rx_ip,
                    "--team", state.team,
                    "--initial-level", str(state.level),
                    "--sample-rate", str(args.sample_rate),
                    "--sps", str(args.sps),
                    "--bt", str(args.bt),
                    "--rx-gain-db", str(args.rx_gain_db),
                    "--access-bit-errors", str(args.access_bit_errors),
                    "--confidence-threshold", str(args.confidence_threshold),
                    "--parse-policy", args.parse_policy,
                    "--payload-endian", args.payload_endian,
                ]
                if args.no_server_comm:
                    new_argv.append("--no-server-comm")
                if args.quiet:
                    new_argv.append("--quiet")
                os.execv(sys.executable, new_argv)

            time.sleep(0.05)

    except KeyboardInterrupt:
        final_status = "interrupted"
    except Exception as exc:
        final_status = "error"
        emit_error("gr rx exception", detail=str(exc),
                   error_type=type(exc).__name__,
                   traceback=traceback.format_exc(limit=12))
        return 1
    finally:
        rx_chain.stop()
        rx_chain.wait()
        if server_comm is not None:
            server_comm.stop()
        if wave_recorder is not None:
            try:
                wave_recorder.close(status=final_status, **record_summary)
                record_summary = wave_recorder.summary()
            except Exception as exc:
                emit_error("failed finalizing wave recording", detail=str(exc))
        emit_json({"kind": "jam_stopped", "ts": time.time(), **record_summary})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
