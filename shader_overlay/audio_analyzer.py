"""音频节拍/多频段 FFT 分析器与毫秒级锁相时钟（支持 808 稳态超低频与底鼓瞬态双通道解耦）。"""
from __future__ import annotations

import array
import math
import random
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Any

from .dsp import FFT_N as _DSP_FFT_N, LowEndFilterBank, rfft256_mags_ring


def _cos_tension(x: float) -> float:
    """非线性余弦张力曲线: 0.5 * (1 - cos(pi * x)), x in [0, 1]。"""
    x = max(0.0, min(1.0, x))
    return 0.5 * (1.0 - math.cos(math.pi * x))


def _log_lin(x: float, gain: float = 18.0) -> float:
    """未削顶对数 dB 域映射（避免满格 808 削平叠加底鼓的瞬态峰值）。"""
    return math.log1p(max(0.0, x) * gain)


def _sub_sustain_env(sub_rms: float, ref_sub: float) -> float:
    """稳态超低频（20~62Hz）声压包络：长时慢参考归一化 + 绝对声压保底，防止持续 808 消失。"""
    floor_log = math.log1p(0.012 * 16.0)  # ~0.175
    abs_ceiling = math.log1p(0.26 * 16.0)  # ~1.641
    sub_log = math.log1p(max(0.0, sub_rms) * 16.0)
    ref_log = math.log1p(max(0.08, ref_sub) * 16.0)

    if sub_log <= floor_log:
        return 0.0
    s_norm = max(0.0, min(1.0, (sub_log - floor_log) / max(0.32, ref_log - floor_log)))
    s_abs = max(0.0, min(1.0, (sub_log - floor_log) / max(0.32, abs_ceiling - floor_log)))
    x_sub = max(0.0, min(1.0, 0.62 * s_norm + 0.38 * s_abs))
    return _cos_tension(max(0.0, min(1.0, (x_sub - 0.08) / 0.92)))


def _kick_flux_score(
    *,
    body_e: float,
    sub_e: float,
    click_e: float,
    base_body_log: float,
    base_sub_log: float,
    base_click_log: float,
    mid_e: float,
    high_e: float,
) -> float:
    """计算 808 免疫 + 抗军鼓误触的底鼓瞬态通量得分。"""
    flux_body = max(0.0, _log_lin(body_e, 18.0) - base_body_log) if body_e >= 0.018 else 0.0
    flux_sub = max(0.0, _log_lin(sub_e, 16.0) - base_sub_log) if sub_e >= 0.028 else 0.0
    flux_click = max(0.0, _log_lin(click_e, 22.0) - base_click_log)

    # 鼓槌敲击 click 只有在 body 也有正向瞬态支撑时才提供加分，防止纯镲片/纯军鼓误触发
    click_assist = min(flux_body * 1.65, flux_click) * 0.45
    # 持续 808 稳定后 flux_sub -> 0；新拍起头瞬间 flux_sub 提供适度辅助
    score = 0.78 * flux_body + 0.28 * flux_sub + click_assist

    # 底噪与纯中高频/军鼓抑制
    if body_e < 0.022 and sub_e < 0.032:
        return 0.0
    # 军鼓/镲片排异：当 74~175Hz 鼓体较弱、无有效超低频支撑、且中高频或 1.6~4.5kHz 宽带噪声占主导时强力抑制
    if body_e < 0.068 and sub_e < 0.035 and (
        click_e > body_e * 0.42 or (high_e + mid_e) > body_e * 7.5
    ):
        score *= 0.14
    elif mid_e > 0.25 and body_e < mid_e * 0.10 and sub_e < 0.04:
        score *= 0.22
    return score


def _adaptive_threshold(values: list[float], min_floor: float = 0.085, mad_k: float = 1.55) -> tuple[float, float]:
    """基于滑动窗口中位数与平均绝对偏差（MAD）计算自适应瞬态门限与局部 95% 峰值参考。"""
    if not values:
        return min_floor, 0.25
    s = sorted(values)
    n = len(s)
    med = s[n // 2]
    mad = sum(abs(v - med) for v in s) / n
    p95 = s[int(0.95 * (n - 1))]
    thresh = max(min_floor, med + mad_k * mad)
    return thresh, max(thresh + 0.12, p95)


class SyncedClock:
    """单调锁相 PLL 播放时钟（与桌面歌词共享毫秒级时间对齐）。"""

    def __init__(self) -> None:
        self.playing: bool = False
        self.paused: bool = False
        self.buffering: bool = False
        self.speed: float = 1.0
        self.seq: int = -1
        self.anchor_pos: float = 0.0
        self.anchor_mono: float = 0.0

    def is_running(self) -> bool:
        return self.playing and not self.paused and not self.buffering

    def update_from_state(self, msg: dict[str, Any], now: float | None = None) -> None:
        t_now = time.monotonic() if now is None else now
        was_running = self.is_running()
        playing = bool(msg.get("playing", False))
        paused = bool(msg.get("paused", False))
        buffering = bool(msg.get("buffering", False))
        speed = max(0.1, float(msg.get("speed", 1.0) or 1.0))
        seq = int(msg.get("seq", 0) or 0)
        raw_pos = max(0.0, float(msg.get("time_pos", 0.0) or 0.0))
        pos_at = float(msg.get("time_pos_at", 0.0) or 0.0)

        now_running = playing and not paused and not buffering
        if now_running and pos_at > 0.0:
            ipc_age = max(0.0, min(0.5, t_now - pos_at))
            target = raw_pos + ipc_age * speed
        else:
            target = raw_pos

        if not was_running or not now_running or seq != self.seq or self.anchor_mono <= 0.0:
            self.anchor_pos = target
            self.anchor_mono = t_now
        else:
            pred = self.anchor_pos + max(0.0, t_now - self.anchor_mono) * self.speed
            err = target - pred
            if abs(err) > 0.08:
                self.anchor_pos = target
                self.anchor_mono = t_now
            elif abs(err) > 0.0015:
                blended = pred + err * 0.35
                if err < 0 and blended < pred:
                    blended = max(pred - 0.002, blended)
                self.anchor_pos = max(0.0, blended)
                self.anchor_mono = t_now

        self.playing = playing
        self.paused = paused
        self.buffering = buffering
        self.speed = speed
        self.seq = seq

    def position(self, now: float | None = None) -> float:
        t_now = time.monotonic() if now is None else now
        if self.is_running() and self.anchor_mono > 0.0:
            return max(0.0, self.anchor_pos + max(0.0, t_now - self.anchor_mono) * self.speed)
        return max(0.0, self.anchor_pos)


class LookaheadPeakTimeline:
    """前瞻式 25Hz 多频段 FFT + 低频分频时间轴：长时稳态 808 + 自适应瞬态峰值底鼓检测。"""

    STRIDE = 17

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.track_id: int | None = None
        self.fps: float = 25.0
        # 扁平 float32 数组，每帧固定 17 个 float:
        # (kick_vis, punch, mid, rms, high, b0..b7, sub_rms, body_rms, body_peak, click_lin)
        self._data: array.array = array.array("f")
        self._kick_flux: array.array = array.array("f")
        self._stats_cache_key: tuple[int, int] | None = None
        self._stats_cache_val: tuple[float, float, float, float, float] = (
            0.12,
            0.12,
            0.26,
            0.085,
            0.25,
        )

    def _compute_flux_at(self, idx: int) -> float:
        d = self._data
        off = idx * self.STRIDE
        mid_e = d[off + 2]
        high_e = d[off + 4]
        sub_e = d[off + 13]
        body_e = 0.55 * d[off + 14] + 0.45 * d[off + 15]
        click_e = d[off + 16]

        # 基底取前 2~6 帧（80~240ms 之前）：避开底鼓自身 1 帧起坡，同时完全吸收持续 808 稳态
        b_hi = max(0, idx - 1)
        b_lo = max(0, idx - 6)
        if b_hi <= b_lo:
            if idx == 0:
                return 0.0
            b_lo, b_hi = 0, idx

        cnt = b_hi - b_lo
        sum_body = 0.0
        sum_sub = 0.0
        sum_click = 0.0
        for j in range(b_lo, b_hi):
            joff = j * self.STRIDE
            sum_body += _log_lin(0.55 * d[joff + 14] + 0.45 * d[joff + 15], 18.0)
            sum_sub += _log_lin(d[joff + 13], 16.0)
            sum_click += _log_lin(d[joff + 16], 22.0)
        inv = 1.0 / cnt
        return _kick_flux_score(
            body_e=body_e,
            sub_e=sub_e,
            click_e=click_e,
            base_body_log=sum_body * inv,
            base_sub_log=sum_sub * inv,
            base_click_log=sum_click * inv,
            mid_e=mid_e,
            high_e=high_e,
        )

    def _rebuild_flux_from(self, start_idx: int) -> None:
        n = len(self._data) // self.STRIDE
        cur_f = len(self._kick_flux)
        if cur_f < n:
            self._kick_flux.extend([0.0] * (n - cur_f))
        elif cur_f > n:
            del self._kick_flux[n:]
        lo = max(0, start_idx)
        for i in range(lo, n):
            self._kick_flux[i] = self._compute_flux_at(i)

    def apply_ipc(self, msg: dict[str, Any]) -> None:
        tid = msg.get("track_id")
        fps = float(msg.get("fps", 25.0) or 25.0)
        start = int(msg.get("start", 0) or 0)
        reset = bool(msg.get("reset", False))
        raw_frames = msg.get("frames") or []
        flat = array.array("f")
        for f in raw_frames:
            if not isinstance(f, (list, tuple)) or len(f) < 4:
                continue
            kick = float(f[0])
            punch = float(f[1])
            mid = float(f[2])
            rms = float(f[3])
            if len(f) >= 13:
                high = float(f[4])
                b8 = (
                    float(f[5]),
                    float(f[6]),
                    float(f[7]),
                    float(f[8]),
                    float(f[9]),
                    float(f[10]),
                    float(f[11]),
                    float(f[12]),
                )
            else:
                high = float(f[4]) if len(f) >= 5 else min(1.0, mid * 0.72 + punch * 0.25)
                b8 = (kick, punch, (punch + mid) * 0.5, mid, mid, high, high, high * 0.9)
            if len(f) >= 17:
                sub_rms = float(f[13])
                body_rms = float(f[14])
                body_peak = float(f[15])
                click_lin = float(f[16])
            else:
                sub_rms = kick * 0.22
                body_rms = punch * 0.20
                body_peak = punch * 0.28
                click_lin = high * 0.16
            flat.extend((kick, punch, mid, rms, high, *b8, sub_rms, body_rms, body_peak, click_lin))

        s = self.STRIDE
        parsed_n = len(flat) // s
        with self._lock:
            self._stats_cache_key = None
            cur_n = len(self._data) // s
            if reset or tid != self.track_id:
                self.track_id = tid
                self.fps = max(1.0, fps)
                self._data = flat
                self._kick_flux = array.array("f")
                self._rebuild_flux_from(0)
                return
            if start == cur_n:
                self._data.extend(flat)
                self._rebuild_flux_from(cur_n)
            elif start < cur_n:
                self._data[start * s : (start + parsed_n) * s] = flat
                self._rebuild_flux_from(start)

    def sample_aligned(
        self, pos_sec: float, lookahead_sec: float = 0.075
    ) -> tuple[float, float, float, float, float, float, float, float, float, tuple[float, ...], float] | None:
        """在 pos + lookahead 处采样，返回 11 元组:
        (energy, sub_env, body_env, mid, high_env, warp_tension, kick_onset, sparkle, centroid, bands8, kick_peak_strength)。
        """
        target_sec = max(0.0, pos_sec + lookahead_sec)
        s = self.STRIDE
        with self._lock:
            d = self._data
            n = len(d) // s
            if n < 3:
                return None
            fps = self.fps
            idx_f = target_sec * fps
            idx0 = int(idx_f)
            if idx0 >= n:
                return None

            frac = idx_f - idx0

            def interp(i: int) -> list[float]:
                ia = max(0, min(n - 1, i)) * s
                ib = max(0, min(n - 1, i + 1)) * s
                return [d[ia + k] + (d[ib + k] - d[ia + k]) * frac for k in range(17)]

            f_prev2 = interp(idx0 - 2)
            f_prev = interp(idx0 - 1)
            f_curr = interp(idx0)
            f_next = interp(idx0 + 1)

            sm = [0.25 * f_prev[k] + 0.50 * f_curr[k] + 0.25 * f_next[k] for k in range(17)]
            kick_vis, punch, mid, rms, high = sm[0], sm[1], sm[2], sm[3], sm[4]
            bands8_raw = sm[5:13]
            sub_rms = sm[13]
            body_rms = sm[14]

            high_flux = max(0.0, f_curr[4] - (0.55 * f_prev[4] + 0.45 * f_prev2[4]))

            sub_half = int(5.0 * fps)
            s_hi = min(n, idx0 + sub_half + 1)
            cache_key = (idx0, s_hi)
            if self._stats_cache_key == cache_key:
                ref_sub, ref_body, ref_high, flux_thresh, flux_p95 = self._stats_cache_val
            else:
                s_lo = max(0, idx0 - sub_half)
                sub_samples = sorted(d[j * s + 13] for j in range(s_lo, s_hi, 2))
                ref_sub = sub_samples[int(0.92 * (len(sub_samples) - 1))] if sub_samples else 0.12

                win_half = int(1.25 * fps)
                lo = max(0, idx0 - win_half)
                hi = min(n, idx0 + win_half + 1)
                local_bodies = sorted(d[j * s + 14] for j in range(lo, hi))
                local_highs = sorted(d[j * s + 4] for j in range(lo, hi))
                ref_body = local_bodies[int(0.92 * (len(local_bodies) - 1))] if local_bodies else 0.12
                ref_high = local_highs[int(0.88 * (len(local_highs) - 1))] if local_highs else 0.26

                flux_half = int(0.55 * fps)
                fl_lo = max(0, idx0 - flux_half)
                fl_hi = min(n, idx0 + flux_half + 1)
                flux_thresh, flux_p95 = _adaptive_threshold(
                    list(self._kick_flux[fl_lo:fl_hi]), min_floor=0.085, mad_k=1.55
                )
                self._stats_cache_key = cache_key
                self._stats_cache_val = (ref_sub, ref_body, ref_high, flux_thresh, flux_p95)

            f0 = self._kick_flux[idx0]
            f_m1 = self._kick_flux[max(0, idx0 - 1)]
            f_p1 = self._kick_flux[min(n - 1, idx0 + 1)]
            curr_flux = f0 + (f_p1 - f0) * frac

        # 稳态 Sub-bass 包络
        sub_env = _sub_sustain_env(sub_rms, ref_sub)

        # 底鼓主体相对包络（与 808 物理隔离）
        body_log = _log_lin(body_rms, 16.0)
        ref_body_log = _log_lin(max(0.08, ref_body), 16.0)
        body_floor = _log_lin(0.012, 16.0)
        x_body = max(0.0, min(1.0, (body_log - body_floor) / max(0.30, ref_body_log - body_floor)))
        body_env = _cos_tension(x_body)

        # 底鼓瞬态通量与自适应峰值拾取
        x_onset = max(
            0.0,
            min(1.0, (curr_flux - flux_thresh * 0.65) / max(0.14, flux_p95 - flux_thresh * 0.65)),
        )
        kick_onset = _cos_tension(x_onset)

        # 精确局部极大值触发（仅在峰顶帧释放冲击事件）
        kick_peak_strength = 0.0
        if f0 >= flux_thresh and f0 >= f_m1 and f0 >= f_p1 and (f0 > f_m1 or f0 > f_p1):
            peak_norm = max(0.0, min(1.0, (f0 - flux_thresh * 0.60) / max(0.14, flux_p95 - flux_thresh * 0.60)))
            kick_peak_strength = max(0.35, min(1.0, _cos_tension(peak_norm) * 0.72 + peak_norm * 0.35))

        # 整体节拍粘滞张力主要由底鼓主体与瞬态驱动（不再被纯 808 拉死）
        warp_tension = _cos_tension(min(1.0, x_body * 0.68 + x_onset * 0.55))

        x_high = max(0.0, min(1.0, (high - 0.025) / max(0.13, ref_high - 0.025)))
        high_env = _cos_tension(min(1.0, x_high * 0.78 + high * 0.42))
        sparkle = min(1.0, _cos_tension(min(1.0, high_flux * 5.2 + max(0.0, x_high - 0.36) * 0.80)))

        b_sum = sum(bands8_raw)
        centroid = (sum(i * bands8_raw[i] for i in range(8)) / (7.0 * b_sum)) if b_sum > 1e-4 else 0.35
        centroid = max(0.0, min(1.0, centroid))

        bands8 = tuple(max(0.0, min(1.0, b * 1.12)) for b in bands8_raw)
        return (
            rms,
            sub_env,
            body_env,
            mid,
            high_env,
            warp_tension,
            kick_onset,
            sparkle,
            centroid,
            bands8,
            kick_peak_strength,
        )


def _build_log_band_ranges(fft_n: int, num_bands: int = 8) -> tuple[tuple[int, int, float], ...]:
    ranges: list[tuple[int, int, float]] = []
    max_bin = fft_n // 2
    for b in range(num_bands):
        f0 = 1.0 * ((max_bin - 2) ** (b / num_bands))
        f1 = 1.0 * ((max_bin - 2) ** ((b + 1) / num_bands))
        i0 = max(1, int(math.floor(f0)))
        i1 = max(i0 + 1, min(max_bin, int(math.ceil(f1))))
        tilt = 0.75 + 0.90 * (b / max(1, num_bands - 1))
        ranges.append((i0, i1, tilt))
    return tuple(ranges)


class AudioBeatAnalyzer:
    """零依赖 PipeWire (.monitor) 实时多频段分析器（4阶 Biquad 低频分频 + 808 免疫底鼓峰值检测）。"""

    SAMPLE_RATE = 22050
    CHUNK_SAMPLES = 330  # ~15ms
    FFT_N = _DSP_FFT_N
    _BAND8_RANGES = _build_log_band_ranges(256, 8)
    _CLICK_BIN_LO = max(1, int(round(1600.0 * 256 / 22050.0)))  # bin 19 (~1636 Hz)
    _CLICK_BIN_HI = min(128, int(round(4500.0 * 256 / 22050.0)) + 1)  # bin 53 (~4565 Hz)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._capture_enabled = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None

        self.energy: float = 0.0
        self.sub: float = 0.0
        self.sub_sustain: float = 0.0
        self.sub_phase: float = 0.0
        self.bass: float = 0.0
        self.mid: float = 0.0
        self.high: float = 0.0
        self.presence: float = 0.0
        self.beat: float = 0.0
        self.heavy_beat: float = 0.0
        self.beat_kick: float = 0.0
        self.beat_rand_amp: float = 1.0
        self.beat_dir_x: float = 1.0
        self.beat_dir_y: float = 0.0
        self.beat_focus_x: float = 0.5
        self.beat_focus_y: float = 0.5
        self.edge_seed: float = 0.0
        self.sparkle: float = 0.0
        self.centroid: float = 0.38
        self.bands: list[float] = [0.0] * 8

        self.flow_time: float = 0.0
        self.flow_vel_x: float = 0.06
        self.flow_vel_y: float = -0.04
        self._target_vx: float = 0.06
        self._target_vy: float = -0.04
        self._target_beat_dx: float = 1.0
        self._target_beat_dy: float = 0.0
        self._target_focus_x: float = 0.5
        self._target_focus_y: float = 0.5
        self._target_edge_seed: float = 0.0
        self._last_vel_shuffle: float = 0.0
        self._last_beat_onset: float = 0.0

        self._low_filter = LowEndFilterBank(self.SAMPLE_RATE, decimate=4)
        self._pcm_ring: list[float] = [0.0] * self.FFT_N
        self._ring_head: int = 0

        self._raw_energy: float = 0.0
        self._raw_sub: float = 0.0
        self._raw_bass: float = 0.0
        self._raw_mid: float = 0.0
        self._raw_high: float = 0.0
        self._raw_warp: float = 0.0
        self._raw_onset: float = 0.0
        self._raw_sparkle: float = 0.0
        self._raw_centroid: float = 0.38
        self._raw_bands: list[float] = [0.0] * 8
        self._pending_kick_peak: float = 0.0
        self._last_audio_at: float = 0.0

        # 实时通道慢参考与快慢包络状态（单位均与 CHUNK_SAMPLES=330 ~15ms 匹配）
        self._sub_slow_ref: float = 0.12
        self._body_slow_ref: float = 0.12
        self._fast_body_log: float = 0.0
        self._slow_body_log: float = 0.0
        self._fast_sub_log: float = 0.0
        self._slow_sub_log: float = 0.0
        self._fast_click_log: float = 0.0
        self._slow_click_log: float = 0.0
        self._flux_hist: deque[float] = deque(maxlen=58)  # ~0.87s
        self._high_hist: deque[float] = deque(maxlen=65)
        self._prev_high_e: float = 0.0
        self._prev_flux: float = 0.0
        self._prev2_flux: float = 0.0
        self._rt_last_peak_at: float = 0.0
        self._rt_sim_time: float = 0.0

    def start(self, capture_enabled: bool = False) -> None:
        if capture_enabled:
            self._capture_enabled.set()
        else:
            self._capture_enabled.clear()
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def set_capture_enabled(self, enabled: bool) -> None:
        """按需启停底层 parec 实时采集与 FFT（隐藏态或已有 TUI lookahead 时零后台开销）。"""
        if enabled:
            self._capture_enabled.set()
            return
        if not self._capture_enabled.is_set() and self._proc is None:
            return
        self._capture_enabled.clear()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()
        self._capture_enabled.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None

    @staticmethod
    def _find_monitor_source() -> str | None:
        if not shutil.which("parec"):
            return None
        try:
            res = subprocess.run(
                ["pactl", "get-default-sink"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            sink = (res.stdout or "").strip()
            if sink:
                return f"{sink}.monitor"
        except Exception:  # noqa: BLE001
            pass
        return None

    def _capture_loop(self) -> None:
        chunk_bytes = self.CHUNK_SAMPLES * 2
        while not self._stop.is_set():
            if not self._capture_enabled.wait(timeout=0.35):
                continue
            if self._stop.is_set():
                break

            monitor = self._find_monitor_source()
            cmd = [
                "parec",
                "--format=s16le",
                f"--rate={self.SAMPLE_RATE}",
                "--channels=1",
                "--latency-msec=15",
            ]
            if monitor:
                cmd.extend(["--device", monitor])

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                self._proc = proc
            except Exception:  # noqa: BLE001
                time.sleep(1.5)
                continue

            assert proc.stdout is not None
            try:
                while not self._stop.is_set() and self._capture_enabled.is_set():
                    raw = proc.stdout.read(chunk_bytes)
                    if not raw:
                        break
                    if len(raw) < 4 or not self._capture_enabled.is_set():
                        continue
                    self._process_pcm(raw)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    if proc.stdout:
                        proc.stdout.close()
                    proc.terminate()
                    proc.wait(timeout=0.5)
                except Exception:  # noqa: BLE001
                    pass
                self._proc = None

            if not self._stop.is_set() and self._capture_enabled.is_set():
                time.sleep(1.0)

    def _process_pcm(self, raw: bytes) -> None:
        samples = array.array("h")
        usable = len(raw) - (len(raw) % 2)
        samples.frombytes(raw[:usable])
        n = len(samples)
        if n == 0:
            return

        dt_chunk = max(0.004, min(0.06, n / float(self.SAMPLE_RATE)))
        self._rt_sim_time += dt_chunk

        sum_tot = 0.0
        inv_scale = 1.0 / 32768.0
        chunk_floats = [0.0] * n
        ring = self._pcm_ring
        head = self._ring_head
        mask = self.FFT_N - 1
        for i, s in enumerate(samples):
            x = s * inv_scale
            chunk_floats[i] = x
            sum_tot += x * x
            ring[head] = x
            head = (head + 1) & mask
        self._ring_head = head

        rms_tot = math.sqrt(sum_tot / n)
        sub_rms, body_rms, body_peak, _sub_pk = self._low_filter.process_block(chunk_floats)

        mags = rfft256_mags_ring(ring, head)

        bands8: list[float] = []
        for i0, i1, tilt in self._BAND8_RANGES:
            pk = max(mags[i0:i1]) * tilt
            bands8.append(min(1.0, math.log1p(pk * 5.2) / 2.25))

        click_slice = mags[self._CLICK_BIN_LO : self._CLICK_BIN_HI]
        click_lin = (
            math.sqrt(sum(m * m for m in click_slice) / len(click_slice))
            if click_slice
            else 0.0
        )

        mid_e = sum(bands8[2:5]) / 3.0
        high_e = sum(bands8[5:8]) / 3.0
        e_tot = min(1.0, math.sqrt(max(0.0, rms_tot - 0.0018) * 7.5))

        # 1) 持续超低频长时慢 AGC（上升 1.8s，回落 11.0s，绝不吞掉持续 808）
        if sub_rms > self._sub_slow_ref:
            alpha_s = 1.0 - math.exp(-dt_chunk / 1.8)
        else:
            alpha_s = 1.0 - math.exp(-dt_chunk / 11.0)
        self._sub_slow_ref = max(0.08, self._sub_slow_ref + (sub_rms - self._sub_slow_ref) * alpha_s)
        sub_env = _sub_sustain_env(sub_rms, self._sub_slow_ref)

        # 2) 底鼓主体参考（74~175Hz，与 808 隔离）
        body_e = 0.55 * body_rms + 0.45 * body_peak
        if body_rms > self._body_slow_ref:
            alpha_b = 1.0 - math.exp(-dt_chunk / 0.8)
        else:
            alpha_b = 1.0 - math.exp(-dt_chunk / 4.0)
        self._body_slow_ref = max(0.08, self._body_slow_ref + (body_rms - self._body_slow_ref) * alpha_b)
        body_log_cur = _log_lin(body_rms, 16.0)
        body_floor = _log_lin(0.012, 16.0)
        x_body = max(
            0.0,
            min(1.0, (body_log_cur - body_floor) / max(0.30, _log_lin(self._body_slow_ref, 16.0) - body_floor)),
        )
        body_env = _cos_tension(x_body)

        # 3) 削顶前 dB 域快慢包络差（持续 808 被 slow 包络吸收，不阻挡叠加底鼓）
        cur_body_log = _log_lin(body_e, 18.0)
        cur_sub_log = _log_lin(sub_rms, 16.0)
        cur_click_log = _log_lin(click_lin, 22.0)

        a_fast = 1.0 - math.exp(-dt_chunk / 0.014)
        a_slow = 1.0 - math.exp(-dt_chunk / 0.210)

        self._fast_body_log += (cur_body_log - self._fast_body_log) * a_fast
        self._fast_sub_log += (cur_sub_log - self._fast_sub_log) * a_fast
        self._fast_click_log += (cur_click_log - self._fast_click_log) * a_fast

        kick_flux = _kick_flux_score(
            body_e=body_e,
            sub_e=sub_rms,
            click_e=click_lin,
            base_body_log=self._slow_body_log,
            base_sub_log=self._slow_sub_log,
            base_click_log=self._slow_click_log,
            mid_e=mid_e,
            high_e=high_e,
        )

        # 计算完当帧通量后再更新慢基底，防止把当前尖峰立刻掺进基底
        self._slow_body_log += (cur_body_log - self._slow_body_log) * a_slow
        self._slow_sub_log += (cur_sub_log - self._slow_sub_log) * a_slow
        self._slow_click_log += (cur_click_log - self._slow_click_log) * a_slow

        self._flux_hist.append(kick_flux)
        flux_thresh, flux_p95 = _adaptive_threshold(list(self._flux_hist), min_floor=0.085, mad_k=1.55)

        x_onset = max(
            0.0,
            min(1.0, (kick_flux - flux_thresh * 0.65) / max(0.14, flux_p95 - flux_thresh * 0.65)),
        )
        kick_onset = _cos_tension(x_onset)
        warp_tension = _cos_tension(min(1.0, x_body * 0.68 + x_onset * 0.55))

        # 实时自适应局部峰值拾取（锁存脉冲事件，避免 15ms 短底鼓在渲染帧之间漏拍）
        peak_strength = 0.0
        prev_f = self._prev_flux
        prev2_f = self._prev2_flux
        if (self._rt_sim_time - self._rt_last_peak_at) >= 0.115:
            is_local_peak = (
                prev_f >= flux_thresh
                and prev_f >= prev2_f
                and prev_f > kick_flux
            )
            is_sharp_attack = (
                kick_flux >= flux_thresh * 1.38
                and kick_flux > prev_f * 1.25
                and prev_f <= flux_thresh
            )
            if is_local_peak or is_sharp_attack:
                ref_val = prev_f if is_local_peak else kick_flux
                peak_norm = max(
                    0.0,
                    min(1.0, (ref_val - flux_thresh * 0.60) / max(0.14, flux_p95 - flux_thresh * 0.60)),
                )
                peak_strength = max(0.35, min(1.0, _cos_tension(peak_norm) * 0.72 + peak_norm * 0.35))
                self._rt_last_peak_at = self._rt_sim_time
        self._prev2_flux = prev_f
        self._prev_flux = kick_flux

        # 4) 高频局部动态归一化与闪烁
        self._high_hist.append(high_e)
        ref_high = sorted(self._high_hist)[int(0.88 * (len(self._high_hist) - 1))]
        x_high = max(0.0, min(1.0, (high_e - 0.025) / max(0.13, ref_high - 0.025)))
        high_env = _cos_tension(min(1.0, x_high * 0.78 + high_e * 0.42))

        high_flux = max(0.0, high_e - self._prev_high_e)
        self._prev_high_e = self._prev_high_e * 0.62 + high_e * 0.38
        sparkle = min(1.0, _cos_tension(min(1.0, high_flux * 5.2 + max(0.0, x_high - 0.36) * 0.80)))

        b_sum = sum(bands8)
        centroid = (sum(i * bands8[i] for i in range(8)) / (7.0 * b_sum)) if b_sum > 1e-4 else 0.38
        centroid = max(0.0, min(1.0, centroid))

        now = time.monotonic()
        with self._lock:
            self._raw_energy = e_tot
            self._raw_sub = sub_env
            self._raw_bass = body_env
            self._raw_mid = mid_e
            self._raw_high = high_env
            self._raw_warp = warp_tension
            self._raw_onset = kick_onset
            self._raw_sparkle = sparkle
            self._raw_centroid = centroid
            self._raw_bands = bands8
            if peak_strength > self._pending_kick_peak:
                self._pending_kick_peak = peak_strength
            if e_tot > 0.025 or sub_env > 0.08:
                self._last_audio_at = now

    def step(
        self,
        dt: float,
        now: float,
        lookahead: tuple[Any, ...] | None = None,
    ) -> None:
        """推进多频段包络、持续超低频声压与 808 免疫的随机方向底鼓冲击。"""
        dt = max(0.001, min(0.05, dt))

        def follow(cur: float, target: float, attack: float, release: float) -> float:
            rate = attack if target > cur else release
            return cur + (target - cur) * min(1.0, dt * rate)

        kick_peak_strength = 0.0
        if lookahead is not None:
            r_e, r_sub, r_b, r_m, r_h, target_warp, target_onset, r_spark, r_cent, r_bands = lookahead[:10]
            if len(lookahead) >= 11:
                kick_peak_strength = float(lookahead[10])
            has_sound = r_e > 0.015 or target_warp > 0.02 or r_sub > 0.04
            self.energy = follow(self.energy, r_e, 18.0, 5.5)
            self.sub = follow(self.sub, r_sub, 8.0, 2.4)
            self.bass = follow(self.bass, r_b, 20.0, 6.0)
            self.mid = follow(self.mid, r_m, 18.0, 5.8)
            self.high = follow(self.high, r_h, 24.0, 7.5)
            self.beat = follow(self.beat, target_warp, 26.0, 5.5)
            self.heavy_beat = follow(self.heavy_beat, target_onset, 34.0, 6.8)
            self.sparkle = follow(self.sparkle, r_spark, 34.0, 8.8)
            self.centroid = follow(self.centroid, r_cent, 4.5, 4.5)
            for i in range(8):
                atk = 16.0 + i * 2.2
                rel = 4.8 + i * 0.65
                self.bands[i] = follow(self.bands[i], r_bands[i], atk, rel)
        else:
            with self._lock:
                r_e = self._raw_energy
                r_sub = self._raw_sub
                r_b = self._raw_bass
                r_m = self._raw_mid
                r_h = self._raw_high
                target_warp = self._raw_warp
                target_onset = self._raw_onset
                r_spark = self._raw_sparkle
                r_cent = self._raw_centroid
                r_bands = list(self._raw_bands)
                kick_peak_strength = self._pending_kick_peak
                self._pending_kick_peak = 0.0
                last_audio = self._last_audio_at
            has_sound = (now - last_audio) < 0.85 and (
                self.energy > 0.02 or target_warp > 0.02 or r_sub > 0.04
            )
            self.energy = follow(self.energy, r_e, 10.0, 3.5)
            self.sub = follow(self.sub, r_sub, 6.5, 2.1)
            self.bass = follow(self.bass, r_b, 14.0, 4.5)
            self.mid = follow(self.mid, r_m, 12.0, 4.2)
            self.high = follow(self.high, r_h, 18.0, 5.8)
            self.beat = follow(self.beat, target_warp, 22.0, 4.8)
            self.heavy_beat = follow(self.heavy_beat, target_onset, 30.0, 6.2)
            self.sparkle = follow(self.sparkle, r_spark, 28.0, 7.8)
            self.centroid = follow(self.centroid, r_cent, 3.8, 3.8)
            for i in range(8):
                atk = 11.0 + i * 1.8
                rel = 3.6 + i * 0.55
                self.bands[i] = follow(self.bands[i], r_bands[i], atk, rel)

        # 持续 808 在场权重与慢速低频驻波相位（哪怕持续 30 秒也能保持呼吸涌动）
        self.sub_sustain = follow(self.sub_sustain, self.sub, 3.0, 2.0)
        self.sub_phase = (self.sub_phase + dt * (2.6 + 3.8 * self.sub)) % (math.tau * 100.0)

        target_presence = min(1.0, 0.30 + max(self.energy, self.sub * 0.85) * 0.90) if has_sound else 0.0
        self.presence = follow(self.presence, target_presence, 6.0, 1.8)

        # 自适应瞬态峰值触发底鼓冲击（无固定 0.45 回落锁死，持续 808 期间也能逐拍精准触发）
        if kick_peak_strength > 0.0 and (now - self._last_beat_onset) >= 0.115:
            self._last_beat_onset = now
            self.beat_rand_amp = random.uniform(0.48, 1.62)
            self.beat_kick = max(self.beat_kick, min(1.0, 0.56 + 0.44 * kick_peak_strength))
            ang = random.uniform(0.0, math.tau)
            self._target_beat_dx = math.cos(ang)
            self._target_beat_dy = math.sin(ang)

            # 随机边缘焦点（让不同拍子在屏幕不同边缘/角落产生不均匀的伸缩与冲击）
            focus_ang = random.uniform(0.0, math.tau)
            focus_r = random.uniform(0.32, 0.56)
            self._target_focus_x = max(0.0, min(1.0, 0.5 + math.cos(focus_ang) * focus_r))
            self._target_focus_y = max(0.0, min(1.0, 0.5 + math.sin(focus_ang) * focus_r))
            self._target_edge_seed = random.uniform(0.0, 40.0)

            spd = random.uniform(0.18, 0.42) * (0.5 + 0.5 * kick_peak_strength)
            self._target_vx = self._target_beat_dx * spd
            self._target_vy = self._target_beat_dy * spd

        # 底鼓弹性冲击：极快起跳、脆利回落（~145ms）
        self.beat_kick *= math.exp(-dt * 6.8)

        # 平滑转向当前重拍方向与聚光重心
        self.beat_dir_x += (self._target_beat_dx - self.beat_dir_x) * min(1.0, dt * 9.5)
        self.beat_dir_y += (self._target_beat_dy - self.beat_dir_y) * min(1.0, dt * 9.5)
        dlen = math.hypot(self.beat_dir_x, self.beat_dir_y)
        if dlen > 1e-4:
            self.beat_dir_x /= dlen
            self.beat_dir_y /= dlen

        self.beat_focus_x += (self._target_focus_x - self.beat_focus_x) * min(1.0, dt * 6.5)
        self.beat_focus_y += (self._target_focus_y - self.beat_focus_y) * min(1.0, dt * 6.5)
        self.edge_seed += (self._target_edge_seed - self.edge_seed) * min(1.0, dt * 4.5)

        if (now - self._last_vel_shuffle) > random.uniform(3.2, 5.8):
            self._last_vel_shuffle = now
            ang = random.uniform(0.0, math.tau)
            spd = random.uniform(0.06, 0.22) * (0.4 + 0.7 * self.energy)
            self._target_vx = math.cos(ang) * spd
            self._target_vy = math.sin(ang) * spd

        self.flow_vel_x += (self._target_vx - self.flow_vel_x) * min(1.0, dt * 0.95)
        self.flow_vel_y += (self._target_vy - self.flow_vel_y) * min(1.0, dt * 0.95)

        flow_speed = (
            0.045
            + 0.11 * self.energy
            + 0.12 * self.beat
            + 0.24 * self.beat_kick * self.beat_rand_amp
            + 0.06 * self.sub_sustain
        )
        self.flow_time += dt * flow_speed
