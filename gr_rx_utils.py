#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gr_rx_utils.py — 公共常量、数学辅助
兼容 GNU Radio 流式处理场景
"""

from __future__ import annotations

import numpy as np

# ============================================================
# 接入码常量（来自 protocol.py）
# ============================================================
INFO_ACCESS_CODE: int = 0x2F6F4C74B914492E
JAM_ACCESS_CODE: int = 0x16E8D377151C712D
SOF: int = 0xA5

CMD_0A01: int = 0x0A01
CMD_0A02: int = 0x0A02
CMD_0A03: int = 0x0A03
CMD_0A04: int = 0x0A04
CMD_0A05: int = 0x0A05
CMD_0A06: int = 0x0A06

# 接入码的 numpy 位数组（用于 popcount 距离计算）
INFO_BITS: np.ndarray = np.unpackbits(
    np.frombuffer(INFO_ACCESS_CODE.to_bytes(8, "big"), dtype=np.uint8),
    bitorder="big",
)
JAM_BITS: np.ndarray = np.unpackbits(
    np.frombuffer(JAM_ACCESS_CODE.to_bytes(8, "big"), dtype=np.uint8),
    bitorder="big",
)

# 空中包尺寸：8B接入码 + 4B头 + 15B载荷 = 27字节 = 216比特
AIR_PKT_BYTES: int = 27
AIR_PKT_BITS: int = AIR_PKT_BYTES * 8  # 216
ACCESS_CODE_BITS: int = 64
HEADER_BITS: int = 32
PAYLOAD_BITS: int = 120  # 15 * 8


# ============================================================
# 高斯滤波器系数生成（来自 phy.py）
# ============================================================
def gaussian_taps(sps: int, bt: float, span_symbols: int = 4) -> np.ndarray:
    """生成 GFSK 高斯成形滤波器的 FIR 系数"""
    n = np.arange(-span_symbols * sps, span_symbols * sps + 1, dtype=np.float64)
    t = n / float(sps)
    alpha = np.sqrt(np.log(2.0)) / (2.0 * np.pi * bt)
    h = np.exp(-(t * t) / (2.0 * alpha * alpha))
    h /= np.sum(h) + 1e-15
    return h


# ============================================================
# FM 解调增益计算
# ============================================================
def fm_demod_gain(sample_rate: float) -> float:
    """计算 quadrature_demod_cf 的 gain 参数，等效 fm_demod 中的 *fs/(2π)"""
    return sample_rate / (2.0 * np.pi)


# ============================================================
# 归一化系数
# ============================================================
def normalize_gain(sensitivity: float) -> float:
    """计算瞬时频率→归一化幅度的缩放系数: 2π/(K*1e6)"""
    return (2.0 * np.pi) / (sensitivity * 1e6)

