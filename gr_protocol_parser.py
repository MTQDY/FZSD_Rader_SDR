#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gr_protocol_parser.py — 协议解析器 (GNU Radio Custom Python Block)

功能: 从 GFSK 解调后的比特流中检测空中包、重组协议帧、解析命令。
这是 CombatRadarSdr2026/parser/gnuradio_frame_parser.py 的 GNU Radio 流式改写,
保留了全部核心算法逻辑。

数据流:
  比特流 (uint8) 输入
    → 滑动窗口接入码检测 (popcount XOR)
    → 空中包切片 (access_code + header + payload)
    → 协议帧重组 (SOF → CRC8 → CRC16 → cmd_id + data)
    → 命令解码 (decode_cmd)
    → 通过 message port "frames" 输出

保留的接口/类名:
  - ProtocolStreamReassembler  (完全保留)
  - ParsedFrame               (完全保留)
  - decode_cmd()              (完全保留)
  - parse_air_packets()       (完全保留)
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .gr_rx_utils import (
    ACCESS_CODE_BITS,
    AIR_PKT_BITS,
    AIR_PKT_BYTES,
    CMD_0A01,
    CMD_0A02,
    CMD_0A03,
    CMD_0A04,
    CMD_0A05,
    CMD_0A06,
    HEADER_BITS,
    INFO_BITS,
    JAM_BITS,
    PAYLOAD_BITS,
    SOF,
    
)
from .rx_tools import (
    bits_to_bytes,
    bits_to_u16,
    crc16_ibm,
    crc8_maxim,
    popcount_mismatch,
)


# 命令解码
def u16_le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset: offset + 2], "little", signed=False)


def u16_be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset: offset + 2], "big", signed=False)


def u16_value(data: bytes, offset: int, payload_endian: str) -> int:
    if payload_endian == "big":
        return u16_be(data, offset)
    return u16_le(data, offset)


def u32_value(data: bytes, offset: int, payload_endian: str) -> int:
    return int.from_bytes(data[offset: offset + 4], payload_endian, signed=False)


def decode_cmd_0a01(data: bytes, payload_endian: str = "little") -> dict:
    names = [
        "enemy_hero", "enemy_engineer",
        "enemy_infantry3", "enemy_infantry4",
        "enemy_air", "enemy_sentinel",
    ]
    return {
        name: {
            "x": u16_value(data, idx * 4, payload_endian),
            "y": u16_value(data, idx * 4 + 2, payload_endian),
        }
        for idx, name in enumerate(names)
    }


def decode_cmd_0a02(data: bytes, payload_endian: str = "little") -> dict:
    names = [
        "enemy_hero_hp", "enemy_engineer_hp",
        "enemy_infantry3_hp", "enemy_infantry4_hp",
        "reserved", "enemy_sentinel_hp",
    ]
    return {name: u16_value(data, idx * 2, payload_endian) for idx, name in enumerate(names)}


def decode_cmd_0a03(data: bytes, payload_endian: str = "little") -> dict:
    names = [
        "enemy_hero_ammo", "enemy_infantry3_ammo",
        "enemy_infantry4_ammo", "enemy_air_ammo",
        "enemy_sentinel_ammo",
    ]
    return {name: u16_value(data, idx * 2, payload_endian) for idx, name in enumerate(names)}


def decode_cmd_0a04(data: bytes, payload_endian: str = "little") -> dict:
    return {
        "left_coins": u16_value(data, 0, payload_endian),
        "total_coins": u16_value(data, 2, payload_endian),
        "occupation_status": u32_value(data, 4, payload_endian) if len(data) >= 8 else 0,
    }


def decode_cmd_0a05(data: bytes) -> dict:
    return {"len": len(data), "hex": data.hex().upper()}


def decode_cmd_0a06(data: bytes) -> dict:
    return {"key": data.decode("ascii", errors="replace"), "len": len(data)}


def decode_cmd(cmd_id: int, data: bytes, payload_endian: str = "little") -> dict:
    """命令分发解码 — 与 gnuradio_frame_parser.py 接口完全一致"""
    if cmd_id == CMD_0A01:
        return decode_cmd_0a01(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A02:
        return decode_cmd_0a02(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A03:
        return decode_cmd_0a03(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A04:
        return decode_cmd_0a04(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A05:
        return decode_cmd_0a05(data)
    if cmd_id == CMD_0A06:
        return decode_cmd_0a06(data)
    return {"len": len(data), "hex": data.hex().upper()}



# 数据结构
@dataclass
class ParsedFrame:
    """解析后的协议帧"""
    ts: float
    seq: int
    cmd_id: int
    data: bytes
    decoded: dict | None = None
    # 附加信息（由接入码检测阶段填入）
    kind: str = "INFO"
    best_info_dist: int = 64
    best_jam_dist: int = 64



# 流重组器
@dataclass
class ProtocolStreamReassembler:
    """
    协议流重组器: 接收 15 字节空中包载荷,
    在字节流中搜索 SOF(0xA5), 校验 CRC8/CRC16, 提取完整协议帧。
    """
    max_buffer: int = 16384

    def __post_init__(self) -> None:
        self.buf = bytearray()

    def clone(self) -> "ProtocolStreamReassembler":
        cp = ProtocolStreamReassembler(max_buffer=self.max_buffer)
        cp.buf = bytearray(self.buf)
        return cp

    def append_payload(self, payload15: bytes) -> None:
        self.buf.extend(payload15)
        if len(self.buf) > self.max_buffer:
            del self.buf[: len(self.buf) - self.max_buffer]

    def extract_frames(self, ts: float) -> list[ParsedFrame]:
        out: list[ParsedFrame] = []
        i = 0
        n = len(self.buf)

        while i + 5 <= n:
            sof_pos = self.buf.find(bytes([SOF]), i)
            if sof_pos < 0:
                break
            if sof_pos + 5 > n:
                i = sof_pos
                break

            hdr = self.buf[sof_pos: sof_pos + 5]
            if crc8_maxim(hdr[:4]) != hdr[4]:
                i = sof_pos + 1
                continue

            data_len = int.from_bytes(hdr[1:3], "little")
            if data_len > 256:
                i = sof_pos + 1
                continue

            frame_len = 5 + 2 + data_len + 2  # hdr + cmd + data + crc16
            if sof_pos + frame_len > n:
                i = sof_pos
                break

            frame = self.buf[sof_pos: sof_pos + frame_len]
            if int.from_bytes(frame[-2:], "little") != crc16_ibm(frame[:-2]):
                i = sof_pos + 1
                continue

            seq = frame[3]
            cmd_id = int.from_bytes(frame[5:7], "little")
            data = bytes(frame[7:-2])
            out.append(ParsedFrame(
                ts=ts,
                seq=seq,
                cmd_id=cmd_id,
                data=data,
                decoded=decode_cmd(cmd_id, data),
            ))
            i = sof_pos + frame_len

        if i > 0:
            del self.buf[:i]
        return out



# 接入码检测与空中包切片
def parse_air_packets(
    bits: np.ndarray,
    max_access_bit_errors: int = 0,
    allow_jam: bool = False,
    info_only: bool = False,
) -> list[dict]:
    """
    滑动窗口接入码检测 + 空中包切片。

    数学:
      对每个位置 i, 取 64-bit 窗口, 计算与 INFO/JAM 接入码的汉明距离。
      d <= max_access_bit_errors → 匹配成功 → 提取 216-bit 空中包。
    """
    out: list[dict] = []
    i = 0
    n = len(bits)

    while i + AIR_PKT_BITS <= n:
        win = bits[i: i + ACCESS_CODE_BITS]
        d_info = popcount_mismatch(win, INFO_BITS)
        d_jam = popcount_mismatch(win, JAM_BITS)

        if d_info <= max_access_bit_errors:
            kind = "INFO"
        elif (not info_only) and allow_jam and d_jam <= max_access_bit_errors and d_jam < d_info:
            kind = "JAM"
        else:
            i += 1
            continue

        pkt = bits[i: i + AIR_PKT_BITS]
        hdr = pkt[ACCESS_CODE_BITS: ACCESS_CODE_BITS + HEADER_BITS]
        l1 = bits_to_u16(hdr[:16])
        l2 = bits_to_u16(hdr[16:32])
        payload_bits = pkt[ACCESS_CODE_BITS + HEADER_BITS: ACCESS_CODE_BITS + HEADER_BITS + PAYLOAD_BITS]
        payload = bits_to_bytes(payload_bits)

        out.append({
            "pos": i,
            "kind": kind,
            "len1": l1,
            "len2": l2,
            "payload": payload,
            "valid": (l1 == 15 and l2 == 15),
            "best_info_dist": d_info,
            "best_jam_dist": d_jam,
        })
        i += AIR_PKT_BITS

    return out


def pmt_to_air_packets(pmt_msg) -> tuple[list[dict], float]:
    """
    从 PMT 消息中提取空中包列表和时间戳。
    返回: (packets_list, ts) 其中 packets_list 是字典列表,
          每项包含 {kind, payload, len1, len2, pos, best_info_dist, best_jam_dist}
    """
    import pmt

    try:
        d = pmt.to_python(pmt_msg)
        if not isinstance(d, dict):
            return [], 0.0

        ts = float(d.get("ts", 0.0))
        raw_packets = d.get("packets", [])
        packets = []
        for p in raw_packets:
            if not isinstance(p, dict):
                continue
            payload_raw = p.get("payload", [])
            packets.append({
                "kind": str(p.get("kind", "INFO")),
                "payload": bytes(payload_raw) if isinstance(payload_raw, list) else payload_raw,
                "len1": int(p.get("len1", 0)),
                "len2": int(p.get("len2", 0)),
                "pos": int(p.get("pos", 0)),
                "best_info_dist": int(p.get("best_info_dist", 64)),
                "best_jam_dist": int(p.get("best_jam_dist", 64)),
            })
        return packets, ts
    except Exception:
        return [], 0.0


#从 PMT 消息中还原 ParsedFrame (供应用层使用) — 保留接口兼容
def pmt_to_parsed_frame(pmt_msg) -> ParsedFrame | None:
    import pmt

    try:
        d = pmt.to_python(pmt_msg)
        if not isinstance(d, dict):
            return None

        data = bytes(d.get("data", []))
        return ParsedFrame(
            ts=float(d.get("ts", 0.0)),
            seq=int(d.get("seq", 0)),
            cmd_id=int(d.get("cmd_id", 0)),
            data=data,
            decoded=decode_cmd(int(d.get("cmd_id", 0)), data),
            kind=str(d.get("kind", "INFO")),
            best_info_dist=int(d.get("best_info_dist", 64)),
            best_jam_dist=int(d.get("best_jam_dist", 64)),
        )
    except Exception:
        return None
