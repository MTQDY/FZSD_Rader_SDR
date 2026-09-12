"""
epy_power_probe.py — IQ 功率探针 

计算输入复数流的平均功率 (指数滑动平均), 支持读取 dBFS 值。
用于实时监测 SDR 接收到的信号强度。

用法:
    probe = PowerProbe(alpha=0.0001)
    self.connect(self.source, probe)
    ...
    power_dbfs = probe.level()   # 读取当前功率
"""

from __future__ import annotations

import numpy as np
from gnuradio import gr


class PowerProbe(gr.sync_block):
    """
    IQ 功率探针: 计算 |x|² 的指数滑动平均, 输出 dBFS。

    参数:
        alpha : EMA 系数 (越小平均越久, 默认 1e-4 ≈ 10ms @1MSps)
    """

    def __init__(self, alpha: float = 1e-4):
        gr.sync_block.__init__(
            self,
            name="PowerProbe",
            in_sig=[np.complex64],
            out_sig=None,  # 纯 sink
        )
        self.alpha = float(alpha)
        self._avg = 0.0

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        if n == 0:
            return 0

        # |x|² 的块内均值
        p = (x.real.astype(np.float64) ** 2 + x.imag.astype(np.float64) ** 2)
        block_mean = float(np.mean(p))

        # 按样本数换算等效 EMA 系数 (处理可变块大小)
        # beta = 1 - (1-alpha)^n : n 大时 beta→1 (直接用块均值)
        beta = 1.0 - (1.0 - self.alpha) ** n
        self._avg = (1.0 - beta) * self._avg + beta * block_mean

        self.consume(0, n)
        return 0

    def level(self) -> float:
        """返回当前平均功率 (dBFS)"""
        return 10.0 * np.log10(self._avg + 1e-15)

    def reset(self) -> None:
        """重置平均功率"""
        self._avg = 0.0

    def set_alpha(self, alpha: float) -> None:
        self.alpha = float(alpha)
