from __future__ import annotations

import numpy as np

"""工具函数 包含CRC校验与比特操作函数"""


'''CRC 校验（来自 protocol.py）'''

# ---------------------------------------------------------------------------
# CRC-8/MAXIM: 多项式 0x8C (x^8+x^5+x^4+1), reflected
# ---------------------------------------------------------------------------
_CRC8_POLY = 0x8C

def _generate_crc8_table(poly: int) -> list[int]:
    """动态生成 CRC-8 查找表（256 项）"""
    table = [0] * 256
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 0x01:
                crc = ((crc >> 1) ^ poly) & 0xFF
            else:
                crc = (crc >> 1) & 0xFF
        table[i] = crc
    return table

_CRC8_TABLE = _generate_crc8_table(_CRC8_POLY)

def crc8_maxim(data: bytes, init: int = 0xFF) -> int:
    """CRC-8/MAXIM: 查表法"""
    crc = init & 0xFF
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


# ---------------------------------------------------------------------------
# CRC-16/IBM: 多项式 0x8408 (x^16+x^15+x^2+1), reflected
# ---------------------------------------------------------------------------
_CRC16_POLY = 0x8408

def _generate_crc16_table(poly: int) -> list[int]:
    """动态生成 CRC-16 查找表（256 项）"""
    table = [0] * 256
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 0x0001:
                crc = ((crc >> 1) ^ poly) & 0xFFFF
            else:
                crc = (crc >> 1) & 0xFFFF
        table[i] = crc
    return table

_CRC16_TABLE = _generate_crc16_table(_CRC16_POLY)

def crc16_ibm(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/IBM: 查表法"""
    crc = init & 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _CRC16_TABLE[(crc ^ byte) & 0xFF]
    return crc


# ---------------------------------------------------------------------------
# 比特操作工具
# ---------------------------------------------------------------------------

def bits_to_u16(bits: np.ndarray) -> int:
    """将 numpy 位数组 (MSB first) 转为 uint16"""
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def bits_to_bytes(bits: np.ndarray) -> bytes:
    """将 numpy 位数组 pack 为 bytes"""
    if len(bits) % 8 != 0:
        return b""
    return np.packbits(bits.astype(np.uint8), bitorder="big").tobytes()


def access_code_to_bytes(ac: int) -> bytes:
    """将 64-bit 接入码整数转为 8 字节（大端序），供 correlate_access_code_bb 使用"""
    return ac.to_bytes(8, "big")


def popcount_mismatch(a: np.ndarray, b: np.ndarray) -> int:
    """计算两个位数组的汉明距离"""
    return int(np.count_nonzero(a ^ b))