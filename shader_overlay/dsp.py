"""零依赖时域低频分频 DSP 模块（供 TUI 预计算与实时音频分析共享）。"""
from __future__ import annotations

import math
from typing import Sequence


# 4 阶 Butterworth 级联两节二阶滤波器的标准品质因数 Q
_BUTTER4_Q1 = 0.5411961001461970
_BUTTER4_Q2 = 1.3065629648763766


class Biquad:
    """Direct Form II Transposed 二阶 IIR 滤波器（RBJ Audio EQ Cookbook）。"""

    __slots__ = ("b0", "b1", "b2", "a1", "a2", "z1", "z2")

    def __init__(self, b0: float, b1: float, b2: float, a1: float, a2: float) -> None:
        self.b0 = b0
        self.b1 = b1
        self.b2 = b2
        self.a1 = a1
        self.a2 = a2
        self.z1 = 0.0
        self.z2 = 0.0

    def reset(self) -> None:
        self.z1 = 0.0
        self.z2 = 0.0

    @classmethod
    def lowpass(cls, sample_rate: float, cutoff_hz: float, q: float = 0.7071067811865476) -> "Biquad":
        fc = max(1.0, min(sample_rate * 0.45, float(cutoff_hz)))
        w0 = math.tau * fc / float(sample_rate)
        cos_w0 = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * max(1e-4, float(q)))
        a0 = 1.0 + alpha
        inv_a0 = 1.0 / a0
        b1 = (1.0 - cos_w0) * inv_a0
        b0 = 0.5 * b1
        b2 = b0
        a1 = (-2.0 * cos_w0) * inv_a0
        a2 = (1.0 - alpha) * inv_a0
        return cls(b0, b1, b2, a1, a2)

    @classmethod
    def highpass(cls, sample_rate: float, cutoff_hz: float, q: float = 0.7071067811865476) -> "Biquad":
        fc = max(1.0, min(sample_rate * 0.45, float(cutoff_hz)))
        w0 = math.tau * fc / float(sample_rate)
        cos_w0 = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * max(1e-4, float(q)))
        a0 = 1.0 + alpha
        inv_a0 = 1.0 / a0
        b0 = (1.0 + cos_w0) * 0.5 * inv_a0
        b1 = -(1.0 + cos_w0) * inv_a0
        b2 = b0
        a1 = (-2.0 * cos_w0) * inv_a0
        a2 = (1.0 - alpha) * inv_a0
        return cls(b0, b1, b2, a1, a2)


class LowEndFilterBank:
    """低开销 4× 抗混叠降采样 + 4 阶 Biquad 低频分频器。

    将音频物理隔离为两条低频子带（均为未削顶原始线性能量）：
    - Sub-bass（20 ~ 62 Hz）：16 Hz 直流阻隔 + 4 阶 Butterworth 低通 @ 62 Hz
    - Kick Body（74 ~ 175 Hz）：4 阶 Butterworth 高通 @ 74 Hz + 4 阶 Butterworth 低通 @ 175 Hz
    """

    def __init__(
        self,
        sample_rate: float,
        decimate: int = 4,
        sub_cutoff_hz: float = 62.0,
        body_low_hz: float = 74.0,
        body_high_hz: float = 175.0,
    ) -> None:
        self.sample_rate = float(sample_rate)
        self.decimate = max(1, int(decimate))
        self.eff_sr = self.sample_rate / self.decimate

        # 16 Hz 一阶直流阻隔滤波器参数
        self._dc_r = math.exp(-math.tau * 16.0 / self.eff_sr)
        self._dc_x1 = 0.0
        self._dc_y1 = 0.0

        # Sub-bass 4 阶 Butterworth 低通
        self._sub_lp1 = Biquad.lowpass(self.eff_sr, sub_cutoff_hz, _BUTTER4_Q1)
        self._sub_lp2 = Biquad.lowpass(self.eff_sr, sub_cutoff_hz, _BUTTER4_Q2)

        # Kick Body 4 阶 Butterworth 高通 + 4 阶 Butterworth 低通（强力隔离 808 与 400Hz 军鼓主体）
        self._body_hp1 = Biquad.highpass(self.eff_sr, body_low_hz, _BUTTER4_Q1)
        self._body_hp2 = Biquad.highpass(self.eff_sr, body_low_hz, _BUTTER4_Q2)
        self._body_lp1 = Biquad.lowpass(self.eff_sr, body_high_hz, _BUTTER4_Q1)
        self._body_lp2 = Biquad.lowpass(self.eff_sr, body_high_hz, _BUTTER4_Q2)

        # 跨块降采样累加状态，防止块长非 decimate 整数倍时引入周期相位抖动
        self._dec_acc = 0.0
        self._dec_cnt = 0
        self._last_sub_rms = 0.0
        self._last_body_rms = 0.0
        self._last_body_peak = 0.0
        self._last_sub_peak = 0.0

    def reset(self) -> None:
        self._dc_x1 = 0.0
        self._dc_y1 = 0.0
        self._sub_lp1.reset()
        self._sub_lp2.reset()
        self._body_hp1.reset()
        self._body_hp2.reset()
        self._body_lp1.reset()
        self._body_lp2.reset()
        self._dec_acc = 0.0
        self._dec_cnt = 0
        self._last_sub_rms = 0.0
        self._last_body_rms = 0.0
        self._last_body_peak = 0.0
        self._last_sub_peak = 0.0

    def process_block(self, samples: Sequence[float]) -> tuple[float, float, float, float]:
        """处理一段归一化至 [-1, 1] 的 PCM 块，返回 (sub_rms, body_rms, body_peak, sub_peak)。"""
        dec = self.decimate
        inv_dec = 1.0 / dec
        dc_r = self._dc_r
        dc_x1 = self._dc_x1
        dc_y1 = self._dc_y1

        # 将各节滤波器系数与状态拉入局部变量以获得最佳纯 Python 循环速度
        s1 = self._sub_lp1
        s1_b0, s1_b1, s1_b2, s1_a1, s1_a2, s1_z1, s1_z2 = (
            s1.b0, s1.b1, s1.b2, s1.a1, s1.a2, s1.z1, s1.z2
        )
        s2 = self._sub_lp2
        s2_b0, s2_b1, s2_b2, s2_a1, s2_a2, s2_z1, s2_z2 = (
            s2.b0, s2.b1, s2.b2, s2.a1, s2.a2, s2.z1, s2.z2
        )

        h1 = self._body_hp1
        h1_b0, h1_b1, h1_b2, h1_a1, h1_a2, h1_z1, h1_z2 = (
            h1.b0, h1.b1, h1.b2, h1.a1, h1.a2, h1.z1, h1.z2
        )
        h2 = self._body_hp2
        h2_b0, h2_b1, h2_b2, h2_a1, h2_a2, h2_z1, h2_z2 = (
            h2.b0, h2.b1, h2.b2, h2.a1, h2.a2, h2.z1, h2.z2
        )
        bl1 = self._body_lp1
        bl1_b0, bl1_b1, bl1_b2, bl1_a1, bl1_a2, bl1_z1, bl1_z2 = (
            bl1.b0, bl1.b1, bl1.b2, bl1.a1, bl1.a2, bl1.z1, bl1.z2
        )
        bl2 = self._body_lp2
        bl2_b0, bl2_b1, bl2_b2, bl2_a1, bl2_a2, bl2_z1, bl2_z2 = (
            bl2.b0, bl2.b1, bl2.b2, bl2.a1, bl2.a2, bl2.z1, bl2.z2
        )

        dec_acc = self._dec_acc
        dec_cnt = self._dec_cnt

        sub_sq = 0.0
        body_sq = 0.0
        sub_pk = 0.0
        body_pk = 0.0
        out_n = 0

        for x in samples:
            dec_acc += x
            dec_cnt += 1
            if dec_cnt < dec:
                continue
            xd = dec_acc * inv_dec
            dec_acc = 0.0
            dec_cnt = 0

            # 直流阻隔
            xc = xd - dc_x1 + dc_r * dc_y1
            dc_x1 = xd
            dc_y1 = xc

            # Sub-bass 通道 (20 ~ 62 Hz)
            y = s1_b0 * xc + s1_z1
            s1_z1 = s1_b1 * xc - s1_a1 * y + s1_z2
            s1_z2 = s1_b2 * xc - s1_a2 * y

            ys = s2_b0 * y + s2_z1
            s2_z1 = s2_b1 * y - s2_a1 * ys + s2_z2
            s2_z2 = s2_b2 * y - s2_a2 * ys

            sub_sq += ys * ys
            ays = ys if ys >= 0.0 else -ys
            if ays > sub_pk:
                sub_pk = ays

            # Kick Body 通道 (74 ~ 175 Hz)
            y = h1_b0 * xc + h1_z1
            h1_z1 = h1_b1 * xc - h1_a1 * y + h1_z2
            h1_z2 = h1_b2 * xc - h1_a2 * y

            yb = h2_b0 * y + h2_z1
            h2_z1 = h2_b1 * y - h2_a1 * yb + h2_z2
            h2_z2 = h2_b2 * y - h2_a2 * yb

            ym = bl1_b0 * yb + bl1_z1
            bl1_z1 = bl1_b1 * yb - bl1_a1 * ym + bl1_z2
            bl1_z2 = bl1_b2 * yb - bl1_a2 * ym

            yk = bl2_b0 * ym + bl2_z1
            bl2_z1 = bl2_b1 * ym - bl2_a1 * yk + bl2_z2
            bl2_z2 = bl2_b2 * ym - bl2_a2 * yk

            body_sq += yk * yk
            ayk = yk if yk >= 0.0 else -yk
            if ayk > body_pk:
                body_pk = ayk

            out_n += 1

        self._dc_x1 = dc_x1
        self._dc_y1 = dc_y1
        s1.z1, s1.z2 = s1_z1, s1_z2
        s2.z1, s2.z2 = s2_z1, s2_z2
        h1.z1, h1.z2 = h1_z1, h1_z2
        h2.z1, h2.z2 = h2_z1, h2_z2
        bl1.z1, bl1.z2 = bl1_z1, bl1_z2
        bl2.z1, bl2.z2 = bl2_z1, bl2_z2
        self._dec_acc = dec_acc
        self._dec_cnt = dec_cnt

        if out_n == 0:
            return (
                self._last_sub_rms,
                self._last_body_rms,
                self._last_body_peak,
                self._last_sub_peak,
            )

        inv_n = 1.0 / out_n
        sub_rms = math.sqrt(sub_sq * inv_n)
        body_rms = math.sqrt(body_sq * inv_n)
        self._last_sub_rms = sub_rms
        self._last_body_rms = body_rms
        self._last_body_peak = body_pk
        self._last_sub_peak = sub_pk
        return sub_rms, body_rms, body_pk, sub_pk
