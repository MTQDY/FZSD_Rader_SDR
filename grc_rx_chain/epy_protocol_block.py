#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gr_protocol_block.py — GNU Radio 流式协议解析 Block

从 gr_protocol_parser.py 中独立出来的 ProtocolParserBlock,
只负责在 GNU Radio 调度器中将比特流转换为空中包。

依赖关系:
  比特流 (uint8) 输入                        import: numpy, time, gnuradio.gr, pmt
    → 内部缓冲                               import: .gr_rx_utils → AIR_PKT_BITS
    → parse_air_packets()                    import: .gr_protocol_parser → parse_air_packets
    → message port "packets" 输出
"""

from __future__ import annotations

import time
from typing import Callable

import numpy as np
import pmt
from gnuradio import gr

from ..gr_protocol_parser import parse_air_packets
from ..gr_rx_utils import AIR_PKT_BITS


class ProtocolParserBlock(gr.basic_block):
    """
    GNU Radio 自定义 block: 比特流 → 空中包检测

    输入:  uint8 比特流
    输出:  无直接流输出, 通过 message port "packets" 发布空中包字典列表

    与原始代码的对应:
      parse_air_packets() 的滑动窗口逻辑在此 block 中完成。
      流重组 (ProtocolStreamReassembler) 和协议帧提取在应用层 (launcher) 完成,
      以保持与 jam_rx_app.py 相同的架构。

    参数:
        max_access_bit_errors : 接入码允许的最大比特错误数
        allow_jam             : 是否允许检测干扰波包
        info_only             : 是否仅检测信息波
        on_packets            : 可选回调 callback(packets_list, timestamp)
    """

    def __init__(
        self,
        max_access_bit_errors: int = 1,
        allow_jam: bool = True,
        info_only: bool = False,
        on_packets: Callable[[list[dict], float], None] | None = None,
    ):
        gr.basic_block.__init__(
            self,
            name="ProtocolParserBlock",
            in_sig=[np.uint8],
            out_sig=None,  # 用 message port 输出
        )

        self.max_access_bit_errors = max_access_bit_errors
        self.allow_jam = allow_jam
        self.info_only = info_only
        self._on_packets = on_packets

        # 内部比特缓冲 (滑动窗口用)
        self._bit_buf = np.array([], dtype=np.uint8)
        self._max_bit_buf = 65536  # 约 300 个空中包

        # 消息端口
        self.message_port_register_out(pmt.intern("packets"))
        self.message_port_register_out(pmt.intern("status"))

        # 统计
        self.total_bits_in = 0
        self.total_packets = 0

    def forecast(self, noutput_items, ninputs):
        """告诉调度器每次至少需要多少输入 (GNU Radio 3.10+: 返回列表)"""
        ninput_items_required = [0] * ninputs
        ninput_items_required[0] = max(4096, AIR_PKT_BITS * 2)
        return ninput_items_required

    def general_work(self, input_items, output_items):
        """主处理函数: 滑动窗口接入码检测"""
        in_bits = input_items[0]
        n_in = len(in_bits)

        if n_in == 0:
            return 0

        # 追加到内部缓冲
        self._bit_buf = np.concatenate([self._bit_buf, in_bits])
        self.total_bits_in += n_in

        # 限制缓冲大小
        if len(self._bit_buf) > self._max_bit_buf:
            drop = len(self._bit_buf) - self._max_bit_buf
            self._bit_buf = self._bit_buf[drop:]

        # 解析空中包
        packets = parse_air_packets(
            self._bit_buf,
            max_access_bit_errors=self.max_access_bit_errors,
            allow_jam=self.allow_jam,
            info_only=self.info_only,
        )

        if packets:
            now = time.time()
            # 清除已处理区域
            last_pos = packets[-1]["pos"] + AIR_PKT_BITS
            self._bit_buf = self._bit_buf[last_pos:]

            valid_packets = [p for p in packets if p["valid"]]
            if valid_packets:
                self.total_packets += len(valid_packets)
                # 通过 message port 发送空中包列表
                self.message_port_pub(pmt.intern("packets"), _packets_to_pmt(valid_packets, now))
                # 直接回调 (绕过 top_block 的 set_msg_handler 兼容性问题)
                if self._on_packets is not None:
                    self._on_packets(valid_packets, now)
        else:
            # 保留窗口, 丢弃前面的
            keep = AIR_PKT_BITS * 2
            if len(self._bit_buf) > keep:
                self._bit_buf = self._bit_buf[-keep:]

        self.consume(0, n_in)
        return 0


# ============================================================
# PMT 转换 (从 ProtocolParserBlock 中提取为模块级函数)
# ============================================================
def _packets_to_pmt(packets: list[dict], ts: float):
    """将空中包列表转为 PMT 消息 — 供 ProtocolParserBlock 内部使用"""
    pmt_list = pmt.PMT_NIL
    for pkt in reversed(packets):
        d = pmt.make_dict()
        d = pmt.dict_add(d, pmt.intern("kind"), pmt.intern(str(pkt["kind"])))
        d = pmt.dict_add(d, pmt.intern("payload"), pmt.init_u8vector(len(pkt["payload"]), list(pkt["payload"])))
        d = pmt.dict_add(d, pmt.intern("len1"), pmt.from_long(pkt["len1"]))
        d = pmt.dict_add(d, pmt.intern("len2"), pmt.from_long(pkt["len2"]))
        d = pmt.dict_add(d, pmt.intern("pos"), pmt.from_long(pkt["pos"]))
        d = pmt.dict_add(d, pmt.intern("best_info_dist"), pmt.from_long(pkt.get("best_info_dist", 64)))
        d = pmt.dict_add(d, pmt.intern("best_jam_dist"), pmt.from_long(pkt.get("best_jam_dist", 64)))
        pmt_list = pmt.list_add(pmt_list, d)
    outer = pmt.make_dict()
    outer = pmt.dict_add(outer, pmt.intern("packets"), pmt_list)
    outer = pmt.dict_add(outer, pmt.intern("ts"), pmt.from_double(ts))
    return outer
