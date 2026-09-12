from __future__ import annotations

from dataclasses import dataclass

from .gr_rx_utils import INFO_ACCESS_CODE, JAM_ACCESS_CODE


@dataclass(frozen=True)
class RadioProfile:
    center_freq: int
    rf_bandwidth: int
    sensitivity: float
    rx_gain_db: float
    access_code: int


INFO_PROFILE_CHOICES = ("red1", "blue1")
JAM_PROFILE_CHOICES = ("red1", "red2", "red3", "blue1", "blue2", "blue3")


# 接收增益按模式区分 (硬件范围 -1 ~ 73 dB):
#   - 信息波: 输入约 -60 dBm, 落到 ADC 后仅比 12 位量化底噪高约 10 dB,
#     因此拉满增益以换取信噪比。
#   - 干扰波: 输入约 -10 dBm, 高增益会贴近 AD9361 前端压缩点 (输入 P1dB
#     通常位于满量程下方约 10 dB), 且会阻塞同带的弱信息波, 故退 ~5 dB 留裕量。
INFO_PROFILES = {
    "red1": RadioProfile(433_200_000, 540_000, 1.5628, 73.0, INFO_ACCESS_CODE),
    "blue1": RadioProfile(433_920_000, 540_000, 1.5628, 73.0, INFO_ACCESS_CODE),
}


JAM_PROFILES = {
    "red1": RadioProfile(432_200_000, 940_000, 2.8194, 52.0, JAM_ACCESS_CODE),
    "red2": RadioProfile(432_500_000, 860_000, 2.5681, 52.0, JAM_ACCESS_CODE),
    "red3": RadioProfile(432_800_000, 250_000, 0.6517, 52.0, JAM_ACCESS_CODE),
    "blue1": RadioProfile(434_920_000, 940_000, 2.8194, 52.0, JAM_ACCESS_CODE),
    "blue2": RadioProfile(434_620_000, 860_000, 2.5681, 52.0, JAM_ACCESS_CODE),
    "blue3": RadioProfile(434_320_000, 250_000, 0.6517, 52.0, JAM_ACCESS_CODE),
}

# Backward-compatible alias used by some jam entrypoints.
PROFILE_CHOICES = JAM_PROFILE_CHOICES