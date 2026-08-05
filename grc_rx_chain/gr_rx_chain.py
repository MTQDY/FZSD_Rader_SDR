#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gr_rx_chain.py — GNU Radio 流式 RX 主链 (top_block)

完整接收链路:
  PlutoSDR → GfskDemod(IQ→bits) → ProtocolParserBlock(bits→air_packets)
                                                      ↓
                                            message port "packets"
                                                      ↓
                                          应用层回调 (launcher)

                               → ApplicationHandler (launcher)
"""

from __future__ import annotations

from typing import Callable

from gnuradio import gr
from gnuradio import soapy

from .epy_gfsk_demod import GfskDemod
from .epy_protocol_block import ProtocolParserBlock


class RxChain(gr.top_block):
    """
    流式 RX 主链。

    参数:
        rx_ip                  : PlutoSDR IP 地址 (默认 "192.168.2.1")
        center_freq            : 中心频率 (Hz)
        sample_rate            : 采样率 (Hz), 默认 1e6
        rf_bandwidth           : RF 带宽 (Hz)
        rx_gain_db             : 接收增益 (dB)
        sps                    : 符号过采样倍数, 默认 52
        bt                     : 高斯滤波器 BT 积, 默认 0.35
        sensitivity            : GFSK 调制灵敏度
        max_access_bit_errors  : 接入码允许最大比特错误, 默认 1
        allow_jam              : 是否启用干扰波检测
        info_only              : 是否仅检测信息波
        on_packets             : 空中包回调 callback(packets_list, timestamp)
    """

    def __init__(
        self,
        rx_ip: str = "192.168.2.1",
        center_freq: float = 433_200_000.0,
        sample_rate: float = 1_000_000.0,
        rf_bandwidth: float = 540_000.0,
        rx_gain_db: float = 50.0,
        sps: int = 47,
        bt: float = 0.35,
        sensitivity: float = 1.5628,
        max_access_bit_errors: int = 1,
        allow_jam: bool = True,
        info_only: bool = False,
        on_packets: Callable[[list[dict], float], None] | None = None,
    ):
        gr.top_block.__init__(self, "RxChain", catch_exceptions=True)

        # ----- 存储参数, 供运行时调整 -----
        self._rx_ip = rx_ip
        self._center_freq = center_freq
        self._sample_rate = sample_rate
        self._rf_bandwidth = rf_bandwidth
        self._rx_gain_db = rx_gain_db
        self._sps = sps
        self._bt = bt
        self._sensitivity = sensitivity

        # ----- 1. PlutoSDR 源 -----
        dev = f"driver=plutosdr,uri=ip:{rx_ip}"
        self.source = soapy.source(
            dev,           # device string
            "fc32",        # complex float 32
            1,             # 单通道
            "",            # dev_args
            "",            # stream_args
            [""],          # tune_args
            [""],          # other_settings
        )
        self.source.set_sample_rate(0, sample_rate)
        self.source.set_bandwidth(0, rf_bandwidth)
        self.source.set_frequency(0, center_freq)
        self.source.set_gain_mode(0, False)  # manual gain
        self.source.set_gain(0, min(max(rx_gain_db, 0.0), 73.0))

        # ----- 2. GFSK 解调器 -----
        self.gfsk_demod = GfskDemod(
            sample_rate=sample_rate,
            sps=sps,
            bt=bt,
            sensitivity=sensitivity,
        )

        # ----- 3. 协议解析器 -----
        self.protocol_parser = ProtocolParserBlock(
            max_access_bit_errors=max_access_bit_errors,
            allow_jam=allow_jam,
            info_only=info_only,
            on_packets=on_packets,
        )

        # ----- 连接流图 -----
        self.connect(self.source, self.gfsk_demod)
        self.connect(self.gfsk_demod, self.protocol_parser)


    # 运行时参数更新 (对应 jam_rx_app.py 中的 configure_receiver 逻辑)
    def set_center_freq(self, freq_hz: float) -> None:
        """切换中心频率 (干扰波/信息波切换时调用)"""
        self._center_freq = freq_hz
        self.source.set_frequency(0, freq_hz)

    def set_rf_bandwidth(self, bw_hz: float) -> None:
        """切换 RF 带宽"""
        self._rf_bandwidth = bw_hz
        self.source.set_bandwidth(0, bw_hz)

    def set_sensitivity(self, sensitivity: float) -> None:
        """更新调制灵敏度 (不同 profile 切换时调用)"""
        self._sensitivity = sensitivity
        self.gfsk_demod.set_sensitivity(sensitivity)

    def set_allow_jam(self, allow: bool) -> None:
        """启用/禁用干扰波检测"""
        self.protocol_parser.allow_jam = allow

    def set_info_only(self, info_only: bool) -> None:
        """设置为仅信息波模式"""
        self.protocol_parser.info_only = info_only

    def set_access_bit_errors(self, max_errors: int) -> None:
        """更新接入码允许错误数 (auto-relax 时调用)"""
        self.protocol_parser.max_access_bit_errors = max_errors

    def reconfigure_for_profile(
        self,
        center_freq: float,
        rf_bandwidth: float,
        sensitivity: float,
    ) -> None:
        """一键切换射频配置 (对应 jam_rx_app.py 切换 profile 时的操作)"""
        self.set_center_freq(center_freq)
        self.set_rf_bandwidth(rf_bandwidth)
        self.set_sensitivity(sensitivity)

    def clock_relock(self) -> None:
        """时钟恢复快速重锁 (丢包过多时调用)"""
        self.gfsk_demod.relock()

    def clock_gains_normal(self) -> None:
        """时钟恢复恢复正常增益"""
        self.gfsk_demod.set_gains_normal()


    # 统计信息
    @property
    def stats(self) -> dict:
        """获取解析器统计"""
        p = self.protocol_parser
        return {
            "total_bits_in": p.total_bits_in,
            "total_packets": p.total_packets,
            "total_frames": p.total_frames,
        }



# 创建链
def create_jam_rx_chain(
    rx_ip: str = "192.168.2.1",
    center_freq: float = 432_200_000.0,
    rf_bandwidth: float = 940_000.0,
    sensitivity: float = 2.8194,
    on_packets: Callable[[list[dict], float], None] | None = None,
    **kwargs,
) -> RxChain:
    """创建干扰波接收链"""
    return RxChain(
        rx_ip=rx_ip,
        center_freq=center_freq,
        rf_bandwidth=rf_bandwidth,
        sensitivity=sensitivity,
        allow_jam=True,
        info_only=False,
        on_packets=on_packets,
        **kwargs,
    )


def create_info_rx_chain(
    rx_ip: str = "192.168.2.1",
    center_freq: float = 433_200_000.0,
    rf_bandwidth: float = 540_000.0,
    sensitivity: float = 1.5756,
    on_packets: Callable[[list[dict], float], None] | None = None,
    **kwargs,
) -> RxChain:
    """创建信息波接收链"""
    return RxChain(
        rx_ip=rx_ip,
        center_freq=center_freq,
        rf_bandwidth=rf_bandwidth,
        sensitivity=sensitivity,
        allow_jam=False,
        info_only=True,
        on_packets=on_packets,
        **kwargs,
    )
