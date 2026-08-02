#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gr_gfsk_demod.py — GFSK 解调器 (GNU Radio Hierarchical Block)

将 FM 解调后的信号流转换为比特流，包含：
  1. FM 正交解调  (analog.quadrature_demod_cf)
  2. DC 阻断      (filter.dc_blocker_ff)
  3. 幅度归一化    (blocks.multiply_const_ff)
  4. 高斯匹配滤波  (filter.fir_filter_fff)
  5. 时钟恢复     (digital.clock_recovery_mm_ff)
  6. 二值硬判决   (digital.binary_slicer_fb)

数学对应关系 (与 CombatRadarSdr2026/phy.py 对比):
  fm_demod(iq, fs) ≡ [quad_demod(gain=fs/2π) → dc_blocker]
  slice_packet_candidates(inst, sps, bt, K) ≡ [normalize → fir(gaussian) → clock_recov → slicer]

接口保持: 如需以 numpy 批量方式调用, 可使用 gr_gfsk_demod_batch() 包装函数。
"""

from __future__ import annotations

import numpy as np

from gnuradio import gr
from gnuradio import analog
from gnuradio import blocks
from gnuradio import digital
from gnuradio import filter as grfilter

from ..gr_rx_utils import (
    fm_demod_gain,
    gaussian_taps,
    normalize_gain,
)


class GfskDemod(gr.hier_block2):
    """
    GFSK 流式解调器: complex64 IQ 输入 → uint8 比特流输出

    参数:
        sample_rate : SDR 采样率 (Hz), 默认 1e6
        sps         : 符号过采样倍数, 默认 47
        bt          : 高斯滤波器 BT 积, 默认 0.35
        sensitivity : GFSK 调制灵敏度, 默认 1.5628 (info) / 2.8194 (jam)
        dc_block_len: DC 阻断器长度 (符号数), 默认 32
    """

    def __init__(
        self,
        sample_rate: float = 1_000_000.0,
        sps: int = 47,
        bt: float = 0.35,
        sensitivity: float = 1.5628,
        dc_block_len: int = 32,
    ):
        """
        参数:
            sample_rate : SDR 采样率 (Hz)
            sps         : 符号过采样倍数
            bt          : 高斯滤波器 BT 积
            sensitivity : GFSK 调制灵敏度 (注意: 运行时由 profile 覆盖,
                          信息波=1.5628, 干扰波=0.6646~2.8194, 见 radio_profiles.py)
            dc_block_len: DC 阻断器长度 (符号数)
        """
        gr.hier_block2.__init__(
            self,
            "GfskDemod",
            gr.io_signature(1, 1, gr.sizeof_gr_complex),  # IQ complex input
            gr.io_signature(1, 1, gr.sizeof_char),         # bits (uint8) output
        )

        # ----- 1. FM 正交解调 -----
        # gain = fs / (2π), 将相位差转为瞬时频率 (Hz)
        quad_gain = fm_demod_gain(sample_rate)
        self.quad_demod = analog.quadrature_demod_cf(quad_gain)

        # ----- 2. DC 阻断 -----
        # 等效 fm_demod() 中的 `- np.mean(inst_freq)`
        # 使用长时 DC 阻断器, 长度 = dc_block_len 个符号对应的采样点
        self.dc_blocker = grfilter.dc_blocker_ff(int(dc_block_len * sps), True)

        # ----- 3. 幅度归一化 -----
        # norm[n] = inst_freq[n] * (2π) / (sensitivity * 1e6)
        norm_gain = normalize_gain(sensitivity)
        self.normalize = blocks.multiply_const_ff(norm_gain)

        # ----- 4. 高斯匹配滤波 -----
        # 使用与 phy.py:gaussian_taps() 相同的系数
        taps = gaussian_taps(sps=sps, bt=bt).astype(np.float32)
        self.matched_filter = grfilter.fir_filter_fff(1, taps)

        # ----- 5. 时钟恢复 (Mueller-Muller) -----
        # 从 sps 个采样点中恢复出 1 个符号
        self.clock_recovery = digital.clock_recovery_mm_ff(
            omega=float(sps),
            gain_omega=0.25 * 0.175 * 0.175,
            mu=0.5,
            gain_mu=0.175,
            omega_relative_limit=0.005,
        )

        # ----- 6. 二值硬判决 -----
        # b[k] = 1 if input >= 0 else 0
        self.binary_slicer = digital.binary_slicer_fb()

        # ----- 内部连接 -----
        self.connect(self, self.quad_demod)          # IQ → FM解调
        self.connect(self.quad_demod, self.dc_blocker)   # → DC阻断
        self.connect(self.dc_blocker, self.normalize)    # → 归一化
        self.connect(self.normalize, self.matched_filter) # → 匹配滤波
        self.connect(self.matched_filter, self.clock_recovery)  # → 时钟恢复
        self.connect(self.clock_recovery, self.binary_slicer)   # → 硬判决
        self.connect(self.binary_slicer, self)         # → 输出

    # ---- 运行时参数更新接口 (保持与现有代码相同的参数名) ----
    def set_sensitivity(self, sensitivity: float) -> None:
        """动态更新调制灵敏度, 对应 RadioProfile.sensitivity"""
        self.normalize.set_k(normalize_gain(sensitivity))

    def set_sps(self, sps: int) -> None:
        """动态更新过采样倍数"""
        self.clock_recovery.set_omega(float(sps))


# ============================================================
# 批量处理包装函数 — 保持与 phy.py:fm_demod() 相同的调用接口
# ============================================================
def fm_demod(iq: np.ndarray, sample_rate: int) -> np.ndarray:
    """
    参数与返回值与原版完全一致：
        iq:  复数 IQ 数组 (np.complex64)
        sample_rate: 采样率
        returns: 瞬时频率数组 (float)
    """
    from gnuradio import gr_unittest  # noqa: F811

    # 构建最小流图做一次性批量处理
    tb = gr.top_block("fm_demod_batch", catch_exceptions=True)

    src = blocks.vector_source_c(iq.tolist(), False, 1, [])
    quad = analog.quadrature_demod_cf(fm_demod_gain(sample_rate))
    dc = grfilter.dc_blocker_ff(32, True)
    snk = blocks.vector_sink_f(1)

    tb.connect(src, quad)
    tb.connect(quad, dc)
    tb.connect(dc, snk)
    tb.start()
    tb.wait()

    return np.array(snk.data(), dtype=np.float64)


def gfsk_demod_batch(
    iq: np.ndarray,
    sample_rate: int,
    sps: int,
    bt: float,
    sensitivity: float,
) -> np.ndarray:
    """
    批量 GFSK 解调: IQ → 比特流
    等效 slice_packet_candidates() 中的前几步 (归一化→滤波→判决),
    但只返回比特流, 不做包检测。

    returns: uint8 比特数组
    """
    tb = gr.top_block("gfsk_demod_batch", catch_exceptions=True)

    src = blocks.vector_source_c(iq.tolist(), False, 1, [])
    demod = GfskDemod(
        sample_rate=sample_rate,
        sps=sps,
        bt=bt,
        sensitivity=sensitivity,
    )
    snk = blocks.vector_sink_b(1)

    tb.connect(src, demod)
    tb.connect(demod, snk)
    tb.start()
    tb.wait()

    return np.array(snk.data(), dtype=np.uint8)
