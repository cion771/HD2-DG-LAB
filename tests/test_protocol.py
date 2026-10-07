"""协议与波形编码测试（纯离线，不需要硬件）。"""

from __future__ import annotations

import re
import unittest

from hd2coyote import waves
from hd2coyote.device.dglab_socket import cmd_clear, cmd_pulse, cmd_strength
from hd2coyote.waves import PRESETS, build

HEX16 = re.compile(r"^[0-9A-F]{16}$")


class TestProtocol(unittest.TestCase):
    def test_strength_absolute(self) -> None:
        # 官方示例：A 通道设为 35 -> strength-1+2+35
        self.assertEqual(cmd_strength("A", 35, 2), "strength-1+2+35")
        self.assertEqual(cmd_strength("B", 0, 2), "strength-2+2+0")

    def test_strength_delta(self) -> None:
        self.assertEqual(cmd_strength("A", 5, 1), "strength-1+1+5")
        self.assertEqual(cmd_strength("B", 20, 0), "strength-2+0+20")

    def test_strength_clamped(self) -> None:
        self.assertEqual(cmd_strength("A", 999, 2), "strength-1+2+200")
        self.assertEqual(cmd_strength("A", -5, 2), "strength-1+2+0")

    def test_clear(self) -> None:
        self.assertEqual(cmd_clear("A"), "clear-1")
        self.assertEqual(cmd_clear("B"), "clear-2")

    def test_pulse(self) -> None:
        self.assertEqual(cmd_pulse("A", ["0A0A0A0A64646464"]),
                         'pulse-A:["0A0A0A0A64646464"]')
        self.assertEqual(cmd_pulse("B", ["0A0A0A0A00000000", "0A0A0A0A64646464"]),
                         'pulse-B:["0A0A0A0A00000000","0A0A0A0A64646464"]')


class TestWaveEncoding(unittest.TestCase):
    def test_unit_layout(self) -> None:
        # 官方文档示例：10Hz / 100% => 0A0A0A0A64646464
        self.assertEqual(waves.unit(10, 100), "0A0A0A0A64646464")
        self.assertEqual(waves.unit(240, 0), "F0F0F0F000000000")

    def test_clamp(self) -> None:
        self.assertEqual(waves.clamp_freq(1), 10)
        self.assertEqual(waves.clamp_freq(9999), 240)
        self.assertEqual(waves.clamp_intensity(200), 100)  # 超 100 会整段静音，必须夹住
        self.assertEqual(waves.clamp_intensity(-3), 0)

    def test_all_presets_are_legal(self) -> None:
        for name in PRESETS:
            units = build(name, 1500)
            self.assertTrue(units, name)
            for u in units:
                self.assertRegex(u, HEX16, f"{name} 产生了非法单元")
                ints = [int(u[i:i + 2], 16) for i in range(8, 16, 2)]
                freqs = [int(u[i:i + 2], 16) for i in range(0, 8, 2)]
                self.assertTrue(all(0 <= i <= 100 for i in ints), f"{name} 强度越界 {u}")
                self.assertTrue(all(10 <= f <= 240 for f in freqs), f"{name} 频率越界 {u}")

    def test_duration(self) -> None:
        self.assertEqual(len(build("pinch", 400)), 4)
        self.assertEqual(len(build("death", 4000)), 40)

    def test_chunk_limit(self) -> None:
        units = build("breath", 15000)  # 150 单元
        chunks = list(waves.chunk(units, max_units=100))
        self.assertEqual([len(c) for c in chunks], [100, 50])

    def test_unknown_preset(self) -> None:
        with self.assertRaises(KeyError):
            build("does-not-exist", 500)


if __name__ == "__main__":
    unittest.main()
