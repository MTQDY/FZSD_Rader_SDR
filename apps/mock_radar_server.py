#!/usr/bin/env python3
"""
mock_radar_server.py — 模拟雷达主程序服务器

独立运行，与 server_communication.py 对接：
  收: 接收端发来的裁判系统帧 (0x0A01~0x0A06)，解析并显示
  发: 收到 N 个当前等级干扰密钥后，自动下发下一级切换指令

  等级推进: 1级 → (收到2个密钥) → 2级 → (收到2个密钥) → 3级(信息波)

用法:
  python3 mock_radar_server.py                          # 默认每级收 2 个密钥后升级
  python3 mock_radar_server.py --keys-per-level 3        # 每级收 3 个密钥后升级
  python3 mock_radar_server.py --port 5001               # 自定义端口
"""

from __future__ import annotations

import argparse
import select
import socket
import sys
import time
from datetime import datetime


# ============================================================
# 协议常量 (与 protocol.py 一致，独立实现以保证可单独运行)
# ============================================================
SOF = 0xA5

CMD_0A01 = 0x0A01
CMD_0A02 = 0x0A02
CMD_0A03 = 0x0A03
CMD_0A04 = 0x0A04
CMD_0A05 = 0x0A05
CMD_0A06 = 0x0A06

CMD_NAMES = {
    CMD_0A01: "0x0A01 敌方坐标",
    CMD_0A02: "0x0A02 血量",
    CMD_0A03: "0x0A03 弹药",
    CMD_0A04: "0x0A04 金币/占点",
    CMD_0A05: "0x0A05 自定义",
    CMD_0A06: "0x0A06 干扰密钥",
}


# ============================================================
# CRC 校验 (与 rx_tools.py 一致)
# ============================================================
def crc8_maxim(data: bytes, init: int = 0xFF) -> int:
    crc = init & 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x01:
                crc = ((crc >> 1) ^ 0x8C) & 0xFF
            else:
                crc = (crc >> 1) & 0xFF
    return crc


def crc16_ibm(data: bytes, init: int = 0xFFFF) -> int:
    crc = init & 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = ((crc >> 1) ^ 0x8408) & 0xFFFF
            else:
                crc = (crc >> 1) & 0xFFFF
    return crc


# ============================================================
# 命令解码
# ============================================================
def u16_le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset: offset + 2], "little", signed=False)


def decode_cmd_0a01(data: bytes) -> str:
    names = ["英雄", "工程", "步兵3", "步兵4", "空中", "哨兵"]
    parts = []
    for idx, name in enumerate(names):
        x = u16_le(data, idx * 4)
        y = u16_le(data, idx * 4 + 2)
        parts.append(f"{name}=({x},{y})")
    return ", ".join(parts)


def decode_cmd_0a02(data: bytes) -> str:
    names = ["英雄HP", "工程HP", "步兵3HP", "步兵4HP", "保留", "哨兵HP"]
    parts = [f"{n}={u16_le(data, i * 2)}" for i, n in enumerate(names)]
    return ", ".join(parts)


def decode_cmd_0a03(data: bytes) -> str:
    names = ["英雄弹药", "步兵3弹药", "步兵4弹药", "空中弹药", "哨兵弹药"]
    parts = [f"{n}={u16_le(data, i * 2)}" for i, n in enumerate(names)]
    return ", ".join(parts)


def decode_cmd_0a04(data: bytes) -> str:
    left = u16_le(data, 0)
    total = u16_le(data, 2)
    occ = int.from_bytes(data[4:8], "little", signed=False) if len(data) >= 8 else 0
    return f"剩余金币={left}, 总金币={total}, 占点=0x{occ:08X}"


def decode_cmd_0a05(data: bytes) -> str:
    return f"len={len(data)} hex={data.hex().upper()}"


def decode_cmd_0a06(data: bytes) -> str:
    return f"密钥={data.decode('ascii', errors='replace')}"


def decode_frame(cmd_id: int, data: bytes) -> str:
    decoders = {
        CMD_0A01: decode_cmd_0a01,
        CMD_0A02: decode_cmd_0a02,
        CMD_0A03: decode_cmd_0a03,
        CMD_0A04: decode_cmd_0a04,
        CMD_0A05: decode_cmd_0a05,
        CMD_0A06: decode_cmd_0a06,
    }
    decoder = decoders.get(cmd_id)
    if decoder:
        return decoder(data)
    return f"len={len(data)} hex={data.hex().upper()}"


# ============================================================
# 帧解析
# ============================================================
def extract_frames(buf: bytearray) -> list[tuple[int, int, bytes]]:
    """从字节流中提取裁判帧。返回 [(cmd_id, seq, data), ...]"""
    out: list[tuple[int, int, bytes]] = []
    i = 0
    n = len(buf)

    while i + 5 <= n:
        sof_pos = buf.find(bytes([SOF]), i)
        if sof_pos < 0:
            break
        if sof_pos + 5 > n:
            i = sof_pos
            break

        hdr = buf[sof_pos: sof_pos + 5]
        if crc8_maxim(hdr[:4]) != hdr[4]:
            i = sof_pos + 1
            continue

        data_len = int.from_bytes(hdr[1:3], "little")
        if data_len > 256:
            i = sof_pos + 1
            continue

        frame_len = 5 + 2 + data_len + 2
        if sof_pos + frame_len > n:
            i = sof_pos
            break

        frame = buf[sof_pos: sof_pos + frame_len]
        if int.from_bytes(frame[-2:], "little") != crc16_ibm(frame[:-2]):
            i = sof_pos + 1
            continue

        seq = frame[3]
        cmd_id = int.from_bytes(frame[5:7], "little")
        data = bytes(frame[7:-2])
        out.append((cmd_id, seq, data))
        i = sof_pos + frame_len

    if i > 0:
        del buf[:i]
    return out


# ============================================================
# 模拟雷达服务器
# ============================================================
class MockRadarServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 5000, keys_per_level: int = 2):
        self.host = host
        self.port = port
        self.keys_per_level = keys_per_level
        self.server_sock: socket.socket | None = None
        self.client_sock: socket.socket | None = None
        self.running = False
        self.rx_buf = bytearray()
        self.frame_count = 0

        # 等级推进状态
        self.current_level = 1
        self.keys_at_level = 0

    def start(self) -> None:
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(1)
        self.server_sock.settimeout(1.0)
        self.running = True

        print(f"============================================")
        print(f"  模拟雷达主程序 — 自动等级推进模式")
        print(f"  监听 {self.host}:{self.port}")
        print(f"  每收到 {self.keys_per_level} 个密钥自动升级: 1→2→3")
        print(f"============================================")
        print(f"  按 q 退出")
        print()

        while self.running:
            # 检查键盘输入
            if sys.stdin in select.select([sys.stdin], [], [], 0.05)[0]:
                line = sys.stdin.readline().strip()
                if line == 'q':
                    self.running = False
                    break

            # 等待客户端连接
            if self.client_sock is None:
                print(f"[{self._ts()}] 等待客户端连接...")
                try:
                    self.client_sock, addr = self.server_sock.accept()
                    print(f"[{self._ts()}] ✓ 客户端已连接: {addr}")
                    self.rx_buf.clear()
                    self.current_level = 1
                    self.keys_at_level = 0
                    self._send_jam_level(1)  # 连接后立即下发初始等级
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        print(f"[{self._ts()}] ✗ 连接错误: {e}")
                    continue

            # 接收客户端数据
            if self.client_sock is not None:
                try:
                    self.client_sock.settimeout(0.1)
                    data = self.client_sock.recv(4096)
                    if not data:
                        print(f"[{self._ts()}] 客户端断开")
                        self.client_sock.close()
                        self.client_sock = None
                        continue

                    self.rx_buf.extend(data)
                    frames = extract_frames(self.rx_buf)
                    for cmd_id, seq, data_bytes in frames:
                        self.frame_count += 1
                        name = CMD_NAMES.get(cmd_id, f"0x{cmd_id:04X}")
                        decoded = decode_frame(cmd_id, data_bytes)
                        print(f"[{self._ts()}] #{self.frame_count:04d} {name} seq={seq:03d} | {decoded}")

                        # 收到 0x0A06 干扰密钥 → 计数 → 自动升级
                        if cmd_id == CMD_0A06:
                            self.keys_at_level += 1
                            if self.keys_at_level >= self.keys_per_level and self.current_level < 3:
                                self.current_level += 1
                                self.keys_at_level = 0
                                print(f"[{self._ts()}] ★ 已收满 {self.keys_per_level} 个 {self.current_level - 1} 级密钥 → 自动下发 {self.current_level} 级")
                                self._send_jam_level(self.current_level)

                except socket.timeout:
                    pass
                except Exception as e:
                    print(f"[{self._ts()}] 接收错误: {e}")
                    try:
                        self.client_sock.close()
                    except Exception:
                        pass
                    self.client_sock = None

    def _send_jam_level(self, level: int) -> None:
        """下发干扰等级切换指令: 0xFF + level + 0xFE"""
        if self.client_sock is None:
            return
        cmd = bytes([0xFF, level, 0xFE])
        try:
            self.client_sock.sendall(cmd)
        except Exception as e:
            print(f"[{self._ts()}] 下发失败: {e}")

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def stop(self) -> None:
        self.running = False
        if self.client_sock:
            try:
                self.client_sock.close()
            except Exception:
                pass
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass


# ============================================================
# 主入口
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="模拟雷达主程序服务器 — 自动等级推进")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="监听端口 (默认 5000)")
    parser.add_argument("--keys-per-level", type=int, default=2,
                        help="每级收到多少个密钥后自动升级 (默认 2)")
    args = parser.parse_args()

    server = MockRadarServer(host=args.host, port=args.port,
                             keys_per_level=args.keys_per_level)
    try:
        server.start()
    except KeyboardInterrupt:
        print(f"\n[{server._ts()}] 用户中断")
    finally:
        server.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
