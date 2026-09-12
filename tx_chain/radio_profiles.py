from __future__ import annotations

import math
from dataclasses import dataclass

from protocol import INFO_ACCESS_CODE, JAM_ACCESS_CODE

# ---------------------------------------------------------------------------
# 发射功率标定 (tx_chain 唯一来源)
# ---------------------------------------------------------------------------
# PlutoSDR 在「数字域 0 dBFS + 硬件衰减 0 dB」时的典型输出功率 (dBm)。
# 该值随器件/频段有差异，建议用频谱仪实测后校正。
TX_REF_POWER_DBM = 7.0
# AD9361 TX 硬件衰减范围 (dB)，0 为最大输出。
TX_ATTEN_MIN_DB = -89.75


def dbm_to_atten_db(tx_power_dbm: float, amplitude: float) -> float:
    """把目标物理输出功率 (dBm) 换算为 Pluto TX 硬件衰减 (dB)。

    数字域幅度 amplitude 折算为 20*log10(amplitude) dBFS，
    衰减 = 目标功率 - 满量程参考功率 - 数字域电平。
    例如 amplitude=1.0 (0 dBFS) 且目标 -10 dBm 时，衰减为 -17 dB。
    """
    if amplitude <= 0.0:
        raise ValueError(f"amplitude 必须为正数，当前 {amplitude!r}")
    digital_dbfs = 20.0 * math.log10(amplitude)
    return tx_power_dbm - TX_REF_POWER_DBM - digital_dbfs


def resolve_atten_db(tx_power_dbm: float, amplitude: float) -> float:
    """换算衰减并做范围钳位，超出能力时给出明确告警。"""
    atten_db = dbm_to_atten_db(tx_power_dbm, amplitude)
    if atten_db > 0.0:
        max_power_dbm = TX_REF_POWER_DBM + 20.0 * math.log10(amplitude)
        print(
            f"WARNING: 目标功率 {tx_power_dbm:.2f} dBm 超出当前数字幅度下的上限 "
            f"{max_power_dbm:.2f} dBm，衰减已钳位到 0 dB"
        )
        atten_db = 0.0
    elif atten_db < TX_ATTEN_MIN_DB:
        print(
            f"WARNING: 目标功率 {tx_power_dbm:.2f} dBm 需要 {atten_db:.2f} dB 衰减，"
            f"超出 AD9361 下限 {TX_ATTEN_MIN_DB} dB，已钳位"
        )
        atten_db = TX_ATTEN_MIN_DB
    return atten_db


@dataclass(frozen=True)
class RadioProfile:
    center_freq: int
    rf_bandwidth: int
    sensitivity: float
    tx_power_dbm: float
    access_code: int


INFO_PROFILE_CHOICES = ("red1", "blue1")
JAM_PROFILE_CHOICES = ("red1", "red2", "red3", "blue1", "blue2", "blue3")


# 信息波：目标 -60 dBm 物理输出
INFO_PROFILES = {
    "red1": RadioProfile(433_200_000, 540_000, 1.5756, -60.0, INFO_ACCESS_CODE),
    "blue1": RadioProfile(433_920_000, 540_000, 1.5756, -60.0, INFO_ACCESS_CODE),
}


# 干扰波：目标 -10 dBm 物理输出
JAM_PROFILES = {
    "red1": RadioProfile(432_200_000, 940_000, 2.8323, -10.0, JAM_ACCESS_CODE),
    "red2": RadioProfile(432_500_000, 860_000, 2.5809, -10.0, JAM_ACCESS_CODE),
    "red3": RadioProfile(432_800_000, 250_000, 0.6646, -10.0, JAM_ACCESS_CODE),
    "blue1": RadioProfile(434_920_000, 940_000, 2.8323, -10.0, JAM_ACCESS_CODE),
    "blue2": RadioProfile(434_620_000, 860_000, 2.5809, -10.0, JAM_ACCESS_CODE),
    "blue3": RadioProfile(434_320_000, 250_000, 0.6646, -10.0, JAM_ACCESS_CODE),
}

# Backward-compatible alias used by some jam entrypoints.
PROFILE_CHOICES = JAM_PROFILE_CHOICES
