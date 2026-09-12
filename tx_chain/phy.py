from __future__ import annotations

import numpy as np


def bytes_to_bits_msb(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    return np.unpackbits(arr, bitorder="big").astype(np.float64)


def gaussian_taps(sps: int, bt: float, span_symbols: int = 4) -> np.ndarray:
    n = np.arange(-span_symbols * sps, span_symbols * sps + 1, dtype=np.float64)
    t = n / float(sps)
    alpha = np.sqrt(np.log(2.0)) / (2.0 * np.pi * bt)
    h = np.exp(-(t * t) / (2.0 * alpha * alpha))
    h /= np.sum(h) + 1e-15
    return h


# PlutoSDR (AD9361/AD9363) TX 数据通道满量程。
# 实测 IIO 报告 cf-ad9361-dds-core-lpc/voltage0 为 le:s16/16>>0（16 位有效），
# 且 pyadi-iio 的 tx() 直接 astype(int16) 下发，故 ±32767 即数字域 0 dBFS。
FULL_SCALE = 32767.0


def packet_to_iq(
    packet: bytes,
    sps: int,
    bt: float,
    sensitivity: float,
    amplitude: float,
) -> np.ndarray:
    """生成 2-GFSK 基带 IQ。

    amplitude: 归一化数字幅度，(0, 1] 区间，1.0 = 0 dBFS。
        信号为恒包络 (仅相位调制)，|I|、|Q| 峰值等于 amplitude * FULL_SCALE，
        因此 amplitude 必须 <= 1.0，否则 astype(int16) 会溢出翻转。
    """
    if not 0.0 < amplitude <= 1.0:
        raise ValueError(
            f"amplitude 必须在 (0, 1] 区间（1.0 = 0 dBFS），当前 {amplitude!r}"
        )
    bits = bytes_to_bits_msb(packet)
    nrz = 2.0 * bits - 1.0
    up = np.zeros(len(nrz) * sps, dtype=np.float64)
    up[::sps] = nrz
    shaped = np.convolve(up, gaussian_taps(sps=sps, bt=bt), mode="same")
    phase = np.cumsum(sensitivity * shaped)
    iq = (amplitude * FULL_SCALE) * np.exp(1j * phase)
    return iq.astype(np.complex64)
