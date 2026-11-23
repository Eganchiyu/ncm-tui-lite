"""OpenGL 3.3 Core 常量、GLSL 着色器源码与轻量级 ctypes 绑定。"""
from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any


# ── OpenGL 常量 ───────────────────────────────────────────────────────────────
GL_COLOR_BUFFER_BIT = 0x00004000
GL_TRIANGLES = 0x0004
GL_FRAGMENT_SHADER = 0x8B30
GL_VERTEX_SHADER = 0x8B31
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_BLEND = 0x0BE2
GL_ONE = 1
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_TEXTURE1 = 0x84C1
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_LINEAR = 0x2601
GL_LINEAR_MIPMAP_LINEAR = 0x2703
GL_CLAMP_TO_EDGE = 0x812F
GL_RGBA8 = 0x8058
GL_BGRA = 0x80E1
GL_UNSIGNED_BYTE = 0x1401
GL_FLOAT = 0x1406
GL_FALSE = 0
GL_ARRAY_BUFFER = 0x8892
GL_STREAM_DRAW = 0x88E0

# ── GLSL 3.30 Core Shaders ────────────────────────────────────────────────────
VERT_SHADER = r"""
#version 330 core
out vec2 v_uv;
void main() {
    float x = float((gl_VertexID & 1) << 2) - 1.0;
    float y = float((gl_VertexID & 2) << 1) - 1.0;
    v_uv = vec2(x, y) * 0.5 + 0.5;
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"""

FRAG_SHADER = r"""
#version 330 core
in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_screen;
uniform float u_has_screen;

uniform vec2  u_res;
uniform float u_time;
uniform float u_flow_time;
uniform vec2  u_flow_vel;
uniform float u_presence;       // 0..1: 无音频趋近于0，有音频平滑升起
uniform float u_energy;         // 0..1: 整体音频能量
uniform float u_sub;            // 0..1: 超低频稳态声压包络（20~62Hz，持续 808 不消失）
uniform float u_sub_sustain;    // 0..1: 超低频持续驻留权重（驱动慢速低频驻波涌动）
uniform float u_sub_phase;      // 随超低频推进的慢速驻波相位
uniform float u_bass;           // 0..1: 底鼓主体能量（74~175Hz）
uniform float u_mid;            // 0..1: 中频能量（人声/主旋律）
uniform float u_high;           // 0..1: 高频能量（镲片/泛音/空气感）
uniform float u_beat;           // 0..1: 拍子粘滞张力包络（已与 808 隔离）
uniform float u_heavy_beat;     // 0..1: 底鼓瞬态通量包络
uniform float u_beat_kick;      // 0..1: 自适应峰值拾取触发的底鼓瞬态弹性冲击
uniform float u_beat_rand;      // 0.48..1.62: 当前底鼓的随机强度倍率
uniform vec2  u_beat_dir;       // 当前重拍的随机单位推动方向
uniform vec2  u_beat_focus;     // 当前重拍的随机边缘聚光重心
uniform float u_edge_seed;      // 随重拍跳跃的边缘随机调制相位
uniform float u_sparkle;        // 0..1: 高频微瞬态闪烁包络
uniform float u_centroid;       // 0..1: 频谱质心（音色冷暖）
uniform float u_bands[8];       // 8 频段平滑频谱（低 -> 高）
uniform float u_intro;          // 0..1: 左下 -> 右上液态玻璃入场扫描进度
uniform float u_intro_active;   // 0..1: 入场/退场波前可见度
uniform float u_seed;           // 随机种子
uniform sampler2D u_lyric_tex;  // 可选全屏大歌词离屏纹理 (Premultiplied Alpha)
uniform float u_lyric_alpha;    // 0..1: 大歌词显示平滑权重
uniform vec4  u_lyric_rect;     // (x0, y0, x1, y1) 大歌词在 UV 中的矩形区域

// ── 流动全光谱光影调色板（青绿 -> 冰蓝 -> 靛紫 -> 珊瑚金 -> 柔粉流动循环，不固定粉色）
vec3 flowingAuroraColor(float t, float warmth) {
    // 基于 IQ 余弦光谱 + 音色质心冷暖偏移，色彩丰富、通透不刺眼
    vec3 a = vec3(0.56, 0.58, 0.64);
    vec3 b = vec3(0.32, 0.28, 0.30);
    vec3 c = vec3(1.00, 1.00, 1.00);
    vec3 d = vec3(0.18, 0.42, 0.72) + vec3(0.12, -0.05, -0.15) * warmth;
    return clamp(a + b * cos(6.28318 * (c * t + d)), 0.0, 1.0);
}

// 平滑采样 8 频段连续频谱 (x in [0, 1])
float sampleBand(float x) {
    float fx = clamp(x, 0.0, 1.0) * 7.0;
    int i0 = int(floor(fx));
    int i1 = min(7, i0 + 1);
    float frac = fx - float(i0);
    frac = frac * frac * (3.0 - 2.0 * frac);
    return mix(u_bands[i0], u_bands[i1], frac);
}

// ── 干净的大尺度粘稠颜料流场 (Single Clean Viscous Paint Field) ──────────────
vec2 hash22(vec2 p) {
    p = vec2(dot(p, vec2(127.1, 311.7)),
             dot(p, vec2(269.5, 183.3)));
    return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}

vec3 hash32(vec2 p) {
    vec3 q = vec3(
        dot(p, vec2(127.1, 311.7)),
        dot(p, vec2(269.5, 183.3)),
        dot(p, vec2(419.2, 371.9))
    );
    return fract(sin(q) * 43758.5453123);
}

float gnoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);

    float a = dot(hash22(i + vec2(0.0, 0.0)), f - vec2(0.0, 0.0));
    float b = dot(hash22(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0));
    float c = dot(hash22(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0));
    float d = dot(hash22(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0));

    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// 3 层低频粘稠 FBM（干净顺滑、不糊文字）
float viscousFbm(vec2 p) {
    float v = 0.0;
    float a = 0.54;
    mat2 rot = mat2(0.84, -0.54, 0.54, 0.84);
    for (int i = 0; i < 3; i++) {
        v += a * gnoise(p);
        p = rot * p * 1.78 + vec2(1.9, 7.4);
        a *= 0.45;
    }
    return v;
}

// 粘稠颜料流场：返回标量势能 val 与平滑折射法线 grad
void pigmentField(vec2 p, float t, vec2 vel, float tension, out float val, out vec2 grad) {
    vec2 q = vec2(
        viscousFbm(p + vel * 0.55 + t * 0.08),
        viscousFbm(p + vel * 0.55 + vec2(4.1, 1.7) - t * 0.07)
    );
    vec2 r = vec2(
        viscousFbm(p + (1.65 + tension * 0.70) * q + vec2(2.3, 6.8) + t * 0.11),
        viscousFbm(p + (1.65 + tension * 0.70) * q + vec2(7.1, 3.4) - t * 0.09)
    );
    vec2 sp = p + (1.80 + tension * 0.85) * r;
    val = viscousFbm(sp);

    float eps = 0.032;
    float vx = viscousFbm(sp + vec2(eps, 0.0));
    float vy = viscousFbm(sp + vec2(0.0, eps));
    grad = vec2(vx - val, vy - val) / eps;
}

// 干净的高频随机星尘粒子（纯光效叠加，零像素扭曲）
vec3 highFreqParticles(vec2 p, float t, float hfEnergy, float sparkle, float edgeMask, float centroidVal) {
    if (hfEnergy < 0.03 || edgeMask < 0.02) {
        return vec3(0.0);
    }
    vec3 acc = vec3(0.0);
    // 2 层不同尺度的随机星尘网格
    for (int layer = 0; layer < 2; layer++) {
        float scale = (layer == 0) ? 14.0 : 24.0;
        vec2 drift = (layer == 0)
            ? vec2(t * 0.22, t * 0.34)
            : vec2(-t * 0.31, t * 0.46);
        vec2 gp = p * scale + drift;
        vec2 cell = floor(gp);
        vec2 f = fract(gp) - 0.5;

        vec3 rnd = hash32(cell + float(layer) * 37.0);
        // 随机稀疏度：随高频和瞬态动态开启更多粒子，平时保持克制干净
        float prob = clamp(0.10 + 0.36 * hfEnergy + 0.35 * sparkle, 0.0, 0.75);
        if (rnd.z > prob) {
            continue;
        }
        // 粒子在网格内的随机游走中心
        vec2 offset = (rnd.xy - 0.5) * 0.62
                    + 0.14 * vec2(sin(t * 2.4 + rnd.z * 19.0), cos(t * 2.1 + rnd.x * 17.0));
        float d = length(f - offset);
        float radius = mix(0.045, 0.105, rnd.y) * (0.75 + 0.55 * sparkle);
        float core = exp(-pow(d / max(0.015, radius), 2.0));
        // 随机呼吸闪烁
        float blink = 0.35 + 0.65 * pow(0.5 + 0.5 * sin(t * (5.0 + rnd.x * 7.0) + rnd.y * 25.0), 2.0);
        vec3 col = flowingAuroraColor(rnd.x + t * 0.04, centroidVal);
        float gain = (layer == 0 ? 0.55 : 0.42) * core * blink * (0.45 * hfEnergy + 0.75 * sparkle);
        acc += col * gain;
    }
    return acc * edgeMask;
}

vec2 toScreenTexUV(vec2 uv) {
    // X11 抓屏原点在左上角，OpenGL v_uv 原点在左下角
    return clamp(vec2(uv.x, 1.0 - uv.y), vec2(0.001), vec2(0.999));
}

void main() {
    vec2 uv = v_uv;
    float aspect = u_res.x / max(1.0, u_res.y);
    vec2 aspectVec = vec2(aspect, 1.0);
    vec2 p = (uv - 0.5) * aspectVec * 1.16;

    // ── 1. 超低频（808）与底鼓（Kick）完全解耦的音乐包络 ──────────────────────
    float beatCurve = u_beat * u_beat * (3.0 - 2.0 * u_beat);
    float heavyCurve = u_heavy_beat * u_heavy_beat * (3.0 - 2.0 * u_heavy_beat);
    float kickCurve = u_beat_kick * u_beat_kick * (3.0 - 2.0 * u_beat_kick);
    float subCurve = u_sub * u_sub * (3.0 - 2.0 * u_sub);
    float subSustainCurve = u_sub_sustain * u_sub_sustain * (3.0 - 2.0 * u_sub_sustain);
    float highCurve = smoothstep(0.04, 0.78, u_high);
    float audioPresence = smoothstep(0.02, 0.20, u_presence);

    // 每一拍底鼓瞬态都有独立随机幅度，彻底剥离持续 808 避免流场张力被拉死
    float dynamicBeat = (beatCurve * 0.40 + heavyCurve * 0.60 + kickCurve * 0.88) * u_beat_rand;
    float tension = clamp(dynamicBeat * 0.72, 0.0, 1.45);

    // ── 2. 屏幕边缘距离场 & 环绕频谱映射 ──────────────────────────────────────
    vec2 pxDist = min(uv, 1.0 - uv) * u_res;
    float edgePx = min(pxDist.x, pxDist.y);

    float horizSym = abs(uv.x - 0.5) * 2.0;
    float perimSpecCoord = clamp(uv.y * 0.78 + (1.0 - horizSym) * 0.22, 0.0, 1.0);
    float localBand = sampleBand(perimSpecCoord);
    float localBandCurve = localBand * localBand * (3.0 - 2.0 * localBand);

    // ── 3. 单层干净粘稠流场（无多余叠层） ─────────────────────────────────────
    float fVal = 0.0;
    vec2 fGrad = vec2(0.0);
    pigmentField(p + vec2(u_seed * 2.3, -u_seed * 1.9), u_flow_time, u_flow_vel, tension, fVal, fGrad);

    // ── 4. 左下 -> 右上 液态玻璃入场/退场波前 ─────────────────────────────────
    float diag = (uv.x + uv.y) * 0.5;
    float waveWobble = 0.026 * sin(uv.x * 6.5 - uv.y * 5.5 + u_time * 2.2) + 0.036 * fVal;
    float waveFront = mix(-0.18, 1.18, u_intro) + waveWobble;
    float dWave = diag - waveFront;

    float waveMain = exp(-pow(dWave / 0.055, 2.0));
    float waveTail = exp(-pow((dWave + 0.085) / 0.035, 2.0)) * 0.45;
    float waveLens = (waveMain + waveTail) * u_intro_active;
    float waveSlope = -dWave * exp(-pow(dWave / 0.048, 2.0)) * 22.0 * u_intro_active;
    float sweepReveal = smoothstep(waveFront + 0.08, waveFront - 0.14, diag);

    // ── 5. 高度随机不均匀的边缘渗透权重 + 底鼓局部聚光冲击 ────────────────────
    vec2 centerDelta = uv - 0.5;
    vec2 centerDir = normalize(centerDelta + vec2(1e-5));
    float perimAngle = atan(centerDelta.y, centerDelta.x);

    // 随时间和重拍相位变化的边缘不均匀起伏（让四周有的地方伸展、有的地方收敛）
    float edgeVarNoise = 0.5 + 0.5 * gnoise(vec2(perimAngle * 2.1 + u_edge_seed * 0.15, u_flow_time * 0.35));
    float focusDist = length((uv - u_beat_focus) * aspectVec);
    float focusBoost = exp(-pow(focusDist / 0.50, 2.0)) * (0.42 + 0.58 * kickCurve);

    // 持续 808 低频驻波调制（全周缓慢厚重涌动，画面不晃动、持续 808 始终有生命力）
    float subWave = 0.76
                  + 0.15 * sin(perimAngle * 2.0 - u_sub_phase + fVal * 1.35)
                  + 0.09 * cos(perimAngle * 3.0 + u_sub_phase * 0.72 - fVal * 1.05);
    float subBreath = subCurve * mix(0.92, subWave, 0.45 + 0.55 * subSustainCurve);

    float edgeReach = mix(
        18.0,
        (72.0 + 132.0 * edgeVarNoise)
            + 68.0 * localBandCurve
            + 82.0 * tension * (0.55 + 0.90 * focusBoost),
        audioPresence
    );
    float edgeWeight = exp(-pow(max(0.0, edgePx + fVal * 28.0) / max(18.0, edgeReach), 1.68));

    // 底鼓（Kick）：局部、定向、脆利爆发（集中在随机方向 u_beat_dir 与聚光点 u_beat_focus）
    float dirBias = 0.50 + 0.85 * max(0.0, dot(centerDir, u_beat_dir));
    float kickImpact = kickCurve * u_beat_rand * (0.76 + 0.78 * focusBoost);
    float fluidWarpAmp = audioPresence * sweepReveal
                       * (0.0016 * u_energy + 0.0042 * beatCurve + 0.0128 * kickImpact)
                       * mix(0.04, 1.18, edgeWeight) * dirBias;

    // 超低频（Sub-bass / 808）：全周对称、深沉厚重的向心空气透镜压迫 + 慢速驻波呼吸
    float subEdgeMask = exp(-pow(max(0.0, edgePx + fVal * 18.0) / (68.0 + 82.0 * subBreath), 1.62));
    vec2 subDisp = -centerDir * (subBreath * 0.0052 * subEdgeMask * audioPresence * sweepReveal);

    // 入场波前折射
    vec2 waveDir = normalize(vec2(1.0, 1.0));
    vec2 waveWarp = (waveDir * waveSlope * 0.026 + fGrad * waveLens * 0.012);

    vec2 totalDisp = fGrad * fluidWarpAmp + subDisp + waveWarp;
    float dispMag = length(totalDisp);

    // ── 6. 底鼓驱动的 RGB 棱镜色散（纯粹响应底鼓瞬态，绝不被持续 808 污染）
    float caStrength = audioPresence * sweepReveal * (
        (heavyCurve * 0.42 + kickImpact * 0.86) * (0.0024 + 0.0165 * edgeWeight)
    ) + waveMain * u_intro_active * 0.018;
    vec2 caVec = normalize(totalDisp + u_beat_dir * 0.45 + centerDir * 0.18 + vec2(1e-5)) * caStrength;

    // ── 7. 采样被折射与色散的真实下方屏幕 ─────────────────────────────────────
    vec2 uvR = toScreenTexUV(uv + totalDisp + caVec);
    vec2 uvG = toScreenTexUV(uv + totalDisp);
    vec2 uvB = toScreenTexUV(uv + totalDisp - caVec);

    vec3 refractedScreen = vec3(
        texture(u_screen, uvR).r,
        texture(u_screen, uvG).g,
        texture(u_screen, uvB).b
    );

    // ── 8. 不均匀宽光线流动光影 + 高频干净星尘粒子 ────────────────────────────
    // 沿屏幕四周游走的宽光束（Broad Flowing Light Beams），明暗错落而非均匀包边
    float beam1 = pow(0.5 + 0.5 * sin(perimAngle * 2.0 + u_time * 0.42 + fVal * 2.6), 2.2);
    float beam2 = pow(0.5 + 0.5 * cos(perimAngle * 3.0 - u_time * 0.31 + u_edge_seed * 0.25 - fVal * 1.9), 2.8);
    float broadBeam = clamp(beam1 * 0.65 + beam2 * 0.55 + focusBoost * 0.35, 0.0, 1.25);

    float rimReach = 22.0 + 48.0 * broadBeam * (0.45 + 0.65 * highCurve + 0.45 * localBandCurve + 0.35 * heavyCurve);
    float rimMask = exp(-pow(max(0.0, edgePx + fVal * 14.0 - broadBeam * 12.0) / max(16.0, rimReach), 1.78));

    // 流动全光谱光影：随空间角度、流场与时间持续流转，不再固定粉色
    float colorPhase = perimAngle * 0.22
                     + fVal * 0.38
                     + dot(fGrad, vec2(0.09, 0.07))
                     + u_time * 0.06
                     + u_flow_time * 0.08;
    vec3 lightColor = flowingAuroraColor(colorPhase, u_centroid);

    // 不均匀宽光线漏光强度
    float leakStrength = clamp(
        (rimMask * (0.04 + 0.22 * broadBeam * (0.35 + 0.65 * highCurve + 0.45 * localBandCurve + 0.35 * u_sparkle))
         * audioPresence * sweepReveal)
        + (waveMain * smoothstep(95.0, 0.0, edgePx) * u_intro_active * 0.22),
        0.0, 0.42
    );

    // 超低频（808）专属深沉边缘厚玻璃暗角 + 冷靛/深紫声压底韵（全周厚重，与明透流光区隔）
    float subPressureMask = exp(-pow(edgePx / (74.0 + 62.0 * subBreath), 1.65));
    float subVignette = clamp(
        length(fGrad) * 0.028 * (fluidWarpAmp * 40.0 + waveLens * 0.45)
        + subBreath * subPressureMask * (0.125 + 0.045 * subSustainCurve) * audioPresence * sweepReveal,
        0.0, 0.19
    );
    vec3 subDeepTint = mix(
        vec3(0.09, 0.13, 0.27),
        vec3(0.18, 0.10, 0.28),
        0.5 + 0.5 * sin(u_sub_phase * 0.35 + perimAngle)
    );
    float subTintAmt = clamp(
        subBreath * (0.45 + 0.55 * subSustainCurve) * subPressureMask * 0.15 * audioPresence * sweepReveal,
        0.0, 0.17
    );

    // 高频干净粒子（仅在边缘区域轻盈闪烁，完全不扭曲画面）
    float particleMask = exp(-pow(max(0.0, edgePx - 6.0) / (95.0 + 55.0 * highCurve), 1.65));
    vec3 particles = highFreqParticles(p, u_time, highCurve, u_sparkle, particleMask, u_centroid)
                   * audioPresence * sweepReveal;
    float particleAlpha = clamp(max(particles.r, max(particles.g, particles.b)) * 1.35, 0.0, 0.72);

    vec3 outCol = refractedScreen * (1.0 - subVignette);
    outCol = mix(outCol, subDeepTint, subTintAmt);
    outCol = mix(outCol, lightColor, leakStrength);
    // 宽光线核心处的柔和流光叠加
    outCol += lightColor * (leakStrength * broadBeam * 0.26) * (1.0 - outCol * 0.55);
    outCol += particles * (1.0 - outCol * 0.35);

    // ── 9. 透明混合因子 ───────────────────────────────────────────────────────
    float distortActivity = smoothstep(0.00035, 0.0028, dispMag + caStrength * 0.85);
    float subActivity = smoothstep(0.015, 0.085, subVignette + subTintAmt);
    float finalAlpha = clamp(
        max(max(distortActivity, subActivity), max(leakStrength * 1.35, particleAlpha)) * u_has_screen,
        0.0, 1.0
    );

    if (u_has_screen < 0.5) {
        outCol = mix(lightColor, subDeepTint, subTintAmt * 2.0) + particles;
        finalAlpha = max(max(leakStrength * 0.45, subActivity * 0.55), particleAlpha * 0.75);
    }

    vec4 premulFluid = vec4(clamp(outCol, 0.0, 1.0) * finalAlpha, finalAlpha);

    // ── 10. 可开关全屏大歌词层 ────────────────────────────────────────────────
    if (u_lyric_alpha > 0.003) {
        vec2 rectMin = u_lyric_rect.xy;
        vec2 rectMax = u_lyric_rect.zw;
        vec2 rectSize = max(vec2(1e-4), rectMax - rectMin);
        vec2 luv = (uv - rectMin) / rectSize;

        // 超低频驱动沉稳呼吸缩放，底鼓驱动干脆微弹跳
        float breathScale = 1.0 + (0.013 * kickCurve * u_beat_rand + 0.008 * subBreath) * audioPresence;
        vec2 suv = (luv - 0.5) / breathScale + 0.5;

        if (suv.x > 0.0 && suv.x < 1.0 && suv.y > 0.0 && suv.y < 1.0) {
            vec2 texUv = vec2(suv.x, 1.0 - suv.y);
            float lCa = (kickCurve * 0.0018 + u_sparkle * 0.0009) * audioPresence;
            vec4 lSample;
            if (lCa > 0.0003) {
                vec4 lR = texture(u_lyric_tex, clamp(texUv + vec2(lCa, 0.0), vec2(0.001), vec2(0.999)));
                vec4 lG = texture(u_lyric_tex, texUv);
                vec4 lB = texture(u_lyric_tex, clamp(texUv - vec2(lCa, 0.0), vec2(0.001), vec2(0.999)));
                float la = max(lG.a, max(lR.a, lB.a) * 0.85);
                lSample = vec4(lR.r, lG.g, lB.b, la);
            } else {
                lSample = texture(u_lyric_tex, texUv);
            }
            lSample *= u_lyric_alpha * sweepReveal;
            premulFluid = lSample + premulFluid * (1.0 - lSample.a);
        }
    }

    fragColor = clamp(premulFluid, 0.0, 1.0);
}
"""


# ── 全屏流体动态排印歌词 Pass (Kinetic Liquid Crystal Typography) ──────────────
LYRIC_VERT_SHADER = r"""
#version 330 core
layout(location = 0) in vec2 a_pos;      // NDC (-1..1)
layout(location = 1) in vec2 a_uv;       // Atlas UV (0..1)
layout(location = 2) in vec4 a_cell_uv;  // (u0, v0, u1, v1) 单元格安全边界
layout(location = 3) in vec4 a_state;    // (lit_prog, ignite_pulse, blur_amt, alpha)
layout(location = 4) in vec4 a_meta;     // (local_u, local_v, dissolve, seed_or_neg_trans)

out vec2 v_uv;
out vec4 v_cell_uv;
out vec4 v_state;
out vec4 v_meta;

void main() {
    v_uv = a_uv;
    v_cell_uv = a_cell_uv;
    v_state = a_state;
    v_meta = a_meta;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

LYRIC_FRAG_SHADER = r"""
#version 330 core
in vec2 v_uv;
in vec4 v_cell_uv;
in vec4 v_state;
in vec4 v_meta;

out vec4 fragColor;

uniform sampler2D u_atlas;
uniform sampler2D u_screen;
uniform float u_has_screen;
uniform vec2  u_res;
uniform vec2  u_atlas_size;
uniform float u_time;
uniform float u_flow_time;
uniform float u_presence;
uniform float u_sub;
uniform float u_sub_sustain;
uniform float u_beat_kick;
uniform float u_beat_rand;
uniform vec2  u_beat_dir;
uniform float u_high;
uniform float u_sparkle;
uniform float u_centroid;
uniform float u_intro;
uniform float u_intro_active;
uniform float u_lyric_alpha;

vec3 flowingAuroraColor(float t, float warmth) {
    vec3 a = vec3(0.58, 0.60, 0.67);
    vec3 b = vec3(0.32, 0.28, 0.30);
    vec3 c = vec3(1.00, 1.00, 1.00);
    vec3 d = vec3(0.18, 0.42, 0.72) + vec3(0.12, -0.05, -0.15) * warmth;
    return clamp(a + b * cos(6.28318 * (c * t + d)), 0.0, 1.0);
}

vec2 hash22(vec2 p) {
    p = vec2(dot(p, vec2(127.1, 311.7)),
             dot(p, vec2(269.5, 183.3)));
    return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}

float gnoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
    float a = dot(hash22(i + vec2(0.0, 0.0)), f - vec2(0.0, 0.0));
    float b = dot(hash22(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0));
    float c = dot(hash22(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0));
    float d = dot(hash22(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// 严格钳位在字形单元格内部的 Mipmap 采样，杜绝跨格串色
float sampleGlyph(vec2 uv, float lod) {
    vec2 pad = (0.65 + lod * 0.55) / max(vec2(64.0), u_atlas_size);
    vec2 lo = v_cell_uv.xy + pad;
    vec2 hi = max(lo, v_cell_uv.zw - pad);
    if (uv.x < v_cell_uv.x || uv.x > v_cell_uv.z || uv.y < v_cell_uv.y || uv.y > v_cell_uv.w) {
        return 0.0;
    }
    return textureLod(u_atlas, clamp(uv, lo, hi), lod).a;
}

vec2 toScreenTexUV(vec2 uv) {
    return clamp(vec2(uv.x, 1.0 - uv.y), vec2(0.001), vec2(0.999));
}

void main() {
    float litProg    = clamp(v_state.x, 0.0, 1.0);
    float ignite     = clamp(v_state.y, 0.0, 1.0);
    float blurAmt    = clamp(v_state.z, 0.0, 1.0);
    float glyphAlpha = clamp(v_state.w, 0.0, 1.0) * u_lyric_alpha;
    if (glyphAlpha < 0.002) {
        discard;
    }

    vec2 screenUv = gl_FragCoord.xy / max(vec2(1.0), u_res);
    // 与主 Shader 完全同步的左下 -> 右上液态玻璃入场/退场遮罩
    float diag = (screenUv.x + screenUv.y) * 0.5;
    float waveFront = mix(-0.18, 1.18, u_intro) + 0.020 * sin(screenUv.x * 6.0 - screenUv.y * 5.0 + u_time * 2.1);
    float sweepReveal = smoothstep(waveFront + 0.08, waveFront - 0.14, diag);
    glyphAlpha *= sweepReveal;
    if (glyphAlpha < 0.002) {
        discard;
    }

    vec2 localUv = v_meta.xy;
    float dissolve = clamp(v_meta.z, 0.0, 1.0);
    bool isTrans = (v_meta.w < 0.0);
    float seed = abs(v_meta.w);

    vec2 cellSize = max(vec2(1e-4), v_cell_uv.zw - v_cell_uv.xy);
    vec2 texel = 1.0 / max(vec2(64.0), u_atlas_size);

    // 退场/入场时的有机流体烟雾扰动
    float n1 = gnoise(localUv * 3.8 + vec2(seed * 5.3 + u_flow_time * 0.35, -u_flow_time * 0.28));
    float n2 = gnoise(localUv * 7.6 + vec2(-seed * 3.1 - u_time * 0.45, seed * 2.7 + u_time * 0.32));
    vec2 fluidWarp = vec2(n1, n2) * cellSize * (0.048 * dissolve * dissolve + 0.0045 * ignite);
    vec2 suv = v_uv + fluidWarp;

    // 硬件 Mipmap 电影拉焦：核心锐利 + 近场光晕 + 宽幅环境光晕
    float coreLod = blurAmt * 2.75 + dissolve * 2.40;
    float coreA   = sampleGlyph(suv, coreLod);
    float glowLod = clamp(max(1.85, coreLod + 1.35), 0.0, 3.6);
    float nearGlow = sampleGlyph(suv, glowLod);
    float wideHalo = sampleGlyph(suv, 3.35);

    // 轻柔下偏移光学投影（托底防亮色桌面吞字）
    vec2 shadowOff = vec2(texel.x * 2.2, -texel.y * 4.5);
    float shadowA = sampleGlyph(suv - shadowOff, clamp(coreLod + 2.1, 1.8, 3.6));

    // 流体溶解侵蚀
    if (dissolve > 0.001) {
        float edgeMask = smoothstep(dissolve * 1.12 - 0.18, dissolve * 1.12 + 0.32, 0.54 + 0.46 * n1 + 0.22 * n2);
        coreA *= edgeMask;
        nearGlow *= edgeMask;
        wideHalo *= edgeMask;
        shadowA *= edgeMask;
    }

    if (max(max(coreA, nearGlow), max(wideHalo, shadowA)) < 0.003) {
        discard;
    }

    // 采样真实下方屏幕：既做桌面明暗自适应，又做液态水晶玻璃折射
    vec3 bgCol = vec3(0.08, 0.09, 0.14);
    if (u_has_screen > 0.5) {
        bgCol = texture(u_screen, toScreenTexUV(screenUv)).rgb;
    }
    float bgLuma = dot(bgCol, vec3(0.299, 0.587, 0.114));
    // 亮桌面（网页/白文档）自动加重光学柔影与饱和度
    float brightBgBoost = smoothstep(0.32, 0.82, bgLuma);

    // ── 译文浮标（精致轻盈，不抢巨型主字风头） ──────────────────────────────
    if (isTrans) {
        vec3 shadowCol = vec3(0.025, 0.032, 0.068);
        float shA = shadowA * mix(0.46, 0.72, brightBgBoost);
        vec3 tTint = mix(
            vec3(0.92, 0.95, 1.00),
            flowingAuroraColor(screenUv.x * 0.45 + u_flow_time * 0.07 + seed, u_centroid),
            0.26
        );
        float tA = coreA * 0.88;
        float haloA = nearGlow * 0.22 * (1.0 - 0.35 * brightBgBoost);
        vec3 outRgb = shadowCol * shA * (1.0 - tA)
                    + tTint * tA
                    + tTint * haloA * (1.0 - tA);
        float outA = clamp(max(tA, max(shA, haloA)), 0.0, 1.0) * glyphAlpha;
        fragColor = vec4(clamp(outRgb * glyphAlpha, 0.0, 1.0), outA);
        return;
    }

    // ── 主歌词巨型字形：液态水晶玻璃 + 斜向流动极光 + 菲涅尔刀锋亮边 ───────────
    float audioPresence = smoothstep(0.02, 0.20, u_presence);
    float kickCurve = u_beat_kick * u_beat_kick * (3.0 - 2.0 * u_beat_kick);
    float subCurve = u_sub * u_sub * (3.0 - 2.0 * u_sub);

    // 估算字形平滑法线（供玻璃折射与菲涅尔刀锋边缘高光）
    float nStep = max(2.2, 3.0 + coreLod * 1.4);
    vec2 gDx = vec2(texel.x * nStep, 0.0);
    vec2 gDy = vec2(0.0, texel.y * nStep);
    float nLod = clamp(coreLod + 1.05, 0.9, 2.8);
    float gx = sampleGlyph(suv + gDx, nLod) - sampleGlyph(suv - gDx, nLod);
    float gy = sampleGlyph(suv + gDy, nLod) - sampleGlyph(suv - gDy, nLod);
    vec2 glyphGrad = vec2(gx, gy);
    float gradMag = length(glyphGrad);
    vec2 glyphNormal = (gradMag > 1e-4) ? (glyphGrad / gradMag) : vec2(0.0, 1.0);

    // 斜向液态流光波前：唱到该字时从左下/左向右上优雅漫过
    float sweepCoord = localUv.x * 0.72 + (1.0 - localUv.y) * 0.28 + n1 * 0.085;
    float sweepEdge = mix(-0.08, 1.12, litProg);
    float litMask = smoothstep(sweepEdge + 0.14, sweepEdge - 0.11, sweepCoord);
    // 波前高光脊线（正在扫过瞬间的璀璨流光锋面）
    float waveCrest = exp(-pow((sweepCoord - sweepEdge) / 0.125, 2.0))
                    * smoothstep(0.01, 0.08, litProg)
                    * (1.0 - smoothstep(0.90, 1.0, litProg));

    // 全光谱流动极光色调（与主 Shader 同源流动）
    float auroraPhase = screenUv.x * 0.42
                      - screenUv.y * 0.26
                      + seed * 0.37
                      + n1 * 0.22
                      + u_flow_time * 0.09
                      + u_time * 0.035;
    vec3 auroraPrimary = flowingAuroraColor(auroraPhase, u_centroid);
    vec3 auroraAccent  = flowingAuroraColor(auroraPhase + 0.28, u_centroid);
    // 提亮为高级珠光极光色，避免暗沉脏色
    vec3 pearlAurora = mix(vec3(0.97, 0.985, 1.00), auroraPrimary * 1.16 + vec3(0.06), mix(0.46, 0.64, brightBgBoost));

    // 液态玻璃折射采样真实桌面
    vec3 refractedBg = bgCol;
    if (u_has_screen > 0.5) {
        float refractAmp = (0.010 + 0.008 * litMask + 0.007 * ignite + 0.004 * subCurve * audioPresence);
        vec2 refUv = toScreenTexUV(screenUv + glyphNormal * refractAmp * coreA);
        refractedBg = texture(u_screen, refUv).rgb;
    }

    // 菲涅尔刀锋边缘高光（受光面流光随时间缓缓转动）
    float lightAng = u_time * 0.38 + seed * 4.1;
    vec2 lightDir = vec2(cos(lightAng), sin(lightAng));
    float edgeBand = clamp(gradMag * 2.55, 0.0, 1.0) * smoothstep(0.04, 0.65, coreA);
    float fresnelSpec = pow(edgeBand, 1.25) * (0.42 + 0.58 * max(0.0, dot(glyphNormal, lightDir)));

    // ── 待唱态（液态冰晶磨砂玻璃：轻盈、空灵、透底） ─────────────────────────
    vec3 frostTint = mix(vec3(0.84, 0.90, 0.99), auroraPrimary, 0.22);
    vec3 unlitGlass = mix(refractedBg * 0.82 + vec3(0.05, 0.06, 0.11), frostTint, mix(0.56, 0.72, brightBgBoost));
    unlitGlass += mix(vec3(0.95, 0.98, 1.0), auroraAccent, 0.38) * fresnelSpec * 0.55;
    float unlitOpacity = mix(0.40, 0.54, brightBgBoost);

    // ── 已唱/激发态（液态极光琉璃：璀璨通透 + 流动丝绸光 + 珍珠高光） ──────────
    float silkBand = 0.5 + 0.5 * sin(localUv.x * 5.2 - localUv.y * 3.8 + u_flow_time * 1.45 + seed * 6.0 + n1 * 2.2);
    vec3 litCore = mix(pearlAurora, mix(vec3(1.0), auroraAccent * 1.20, 0.35), silkBand * 0.38);
    // 保留 10%~14% 真实玻璃透底质感，让巨字像会发光的液态琉璃而不是僵硬纯色块
    litCore = mix(litCore, refractedBg * 0.55 + litCore * 0.65, 0.14 * (1.0 - 0.5 * brightBgBoost));
    // 波前锋面与唱到瞬间爆发高光
    litCore += vec3(1.00, 0.99, 1.00) * (waveCrest * 0.42 + ignite * 0.24);
    // 菲涅尔刀锋边缘高亮 + 高频星芒微闪
    float sparkleEdge = (0.30 + 0.70 * pow(0.5 + 0.5 * sin(u_time * 8.5 + localUv.x * 16.0 + seed * 19.0), 3.0))
                      * (0.35 * u_high + 0.85 * u_sparkle) * audioPresence;
    litCore += mix(vec3(1.0), auroraAccent, 0.45) * fresnelSpec * (0.58 + 0.55 * ignite + 0.75 * sparkleEdge);
    float litOpacity = mix(0.91, 0.96, brightBgBoost);

    // 融合待唱与已唱材质
    vec3 bodyCol = mix(unlitGlass, litCore, litMask);
    float bodyOpacity = mix(unlitOpacity, litOpacity, litMask);
    float bodyA = coreA * bodyOpacity;

    // ── 底鼓脆利 RGB 棱镜色散（仅在点亮字与底鼓瞬态时优雅绽放） ──────────────
    vec3 caRgb = vec3(0.0);
    float caA = 0.0;
    float caPx = (kickCurve * u_beat_rand * 4.8 + ignite * 2.6 + waveCrest * 2.2) * (0.25 + 0.75 * litMask) * audioPresence;
    if (caPx > 0.35) {
        vec2 caDir = normalize(u_beat_dir + vec2(0.7, 0.35));
        vec2 caOff = caDir * texel * caPx;
        float aR = sampleGlyph(suv + caOff, coreLod + 0.35);
        float aB = sampleGlyph(suv - caOff, coreLod + 0.35);
        float fringeR = max(0.0, aR - coreA * 0.85);
        float fringeB = max(0.0, aB - coreA * 0.85);
        caRgb = vec3(1.00, 0.46, 0.62) * fringeR * 0.75 + vec3(0.42, 0.84, 1.00) * fringeB * 0.75;
        caA = clamp((fringeR + fringeB) * 0.55, 0.0, 0.45);
    }

    // ── 电影级光学背影（保证浅色桌面绝佳可读性）+ 外围极光呼吸光晕 ────────────
    float outerMask = clamp(1.0 - coreA * 0.92, 0.0, 1.0);
    float subBreath = 1.0 + 0.25 * subCurve * audioPresence;
    float shadowStrength = shadowA * mix(0.32, 0.62, brightBgBoost) * mix(0.65, 1.0, litMask) * subBreath;
    vec3 shadowCol = mix(vec3(0.02, 0.025, 0.065), vec3(0.05, 0.04, 0.11), 0.35);

    float glowIntensity = (0.14 + 0.34 * litMask + 0.32 * ignite + 0.26 * waveCrest)
                        * (1.0 + 0.22 * u_high * audioPresence)
                        * mix(1.0, 0.68, brightBgBoost);
    float nearAuraA = nearGlow * outerMask * glowIntensity * 0.78;
    float wideAuraA = wideHalo * outerMask * glowIntensity * (0.26 + 0.14 * u_sub_sustain * audioPresence);
    vec3 auraCol = mix(auroraPrimary, auroraAccent, 0.42) * 1.12;

    // 严格 Premultiplied Alpha 层次合成：光学暗影 -> 宽极光晕 -> 色散边 -> 水晶极光本体
    vec4 acc = vec4(0.0);
    // 1) 柔和暗色光学投影
    vec4 layerShadow = vec4(shadowCol * shadowStrength, shadowStrength);
    acc = layerShadow;
    // 2) 外围极光流光晕（柔和加法感，不脏暗）
    float auraTotalA = clamp(nearAuraA + wideAuraA, 0.0, 0.72);
    vec3 auraPremul = auraCol * (nearAuraA * 0.95 + wideAuraA * 0.82);
    acc = vec4(auraPremul + acc.rgb * (1.0 - auraTotalA * 0.55), clamp(auraTotalA + acc.a * (1.0 - auraTotalA), 0.0, 1.0));
    // 3) 底鼓微色散边
    if (caA > 0.001) {
        acc = vec4(caRgb + acc.rgb * (1.0 - caA), clamp(caA + acc.a * (1.0 - caA), 0.0, 1.0));
    }
    // 4) 巨型水晶极光字形本体
    vec4 layerBody = vec4(bodyCol * bodyA, bodyA);
    acc = layerBody + acc * (1.0 - layerBody.a);

    fragColor = clamp(acc * glyphAlpha, 0.0, 1.0);
}
"""




class _GLBindings:
    """基于 ctypes + libGL.so.1 的轻量级 OpenGL 3.3 Core 函数绑定。"""

    def __init__(self) -> None:
        lib_path = ctypes.util.find_library("GL") or "libGL.so.1"
        self._lib = ctypes.CDLL(lib_path)
        self._get_proc = self._lib.glXGetProcAddressARB
        self._get_proc.argtypes = [ctypes.c_char_p]
        self._get_proc.restype = ctypes.c_void_p

        self.glClearColor = self._bind("glClearColor", None, [ctypes.c_float] * 4)
        self.glClear = self._bind("glClear", None, [ctypes.c_uint])
        self.glEnable = self._bind("glEnable", None, [ctypes.c_uint])
        self.glDisable = self._bind("glDisable", None, [ctypes.c_uint])
        self.glBlendFunc = self._bind("glBlendFunc", None, [ctypes.c_uint, ctypes.c_uint])
        self.glViewport = self._bind("glViewport", None, [ctypes.c_int] * 4)

        self.glCreateShader = self._bind("glCreateShader", ctypes.c_uint, [ctypes.c_uint])
        self.glShaderSource = self._bind(
            "glShaderSource",
            None,
            [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int)],
        )
        self.glCompileShader = self._bind("glCompileShader", None, [ctypes.c_uint])
        self.glGetShaderiv = self._bind(
            "glGetShaderiv", None, [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
        )
        self.glGetShaderInfoLog = self._bind(
            "glGetShaderInfoLog",
            None,
            [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p],
        )
        self.glDeleteShader = self._bind("glDeleteShader", None, [ctypes.c_uint])

        self.glCreateProgram = self._bind("glCreateProgram", ctypes.c_uint, [])
        self.glAttachShader = self._bind("glAttachShader", None, [ctypes.c_uint, ctypes.c_uint])
        self.glLinkProgram = self._bind("glLinkProgram", None, [ctypes.c_uint])
        self.glGetProgramiv = self._bind(
            "glGetProgramiv", None, [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
        )
        self.glGetProgramInfoLog = self._bind(
            "glGetProgramInfoLog",
            None,
            [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p],
        )
        self.glUseProgram = self._bind("glUseProgram", None, [ctypes.c_uint])
        self.glDeleteProgram = self._bind("glDeleteProgram", None, [ctypes.c_uint])

        self.glGetUniformLocation = self._bind(
            "glGetUniformLocation", ctypes.c_int, [ctypes.c_uint, ctypes.c_char_p]
        )
        self.glUniform1i = self._bind("glUniform1i", None, [ctypes.c_int, ctypes.c_int])
        self.glUniform1f = self._bind("glUniform1f", None, [ctypes.c_int, ctypes.c_float])
        self.glUniform2f = self._bind("glUniform2f", None, [ctypes.c_int, ctypes.c_float, ctypes.c_float])
        self.glUniform4f = self._bind(
            "glUniform4f",
            None,
            [ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float],
        )
        self.glUniform1fv = self._bind(
            "glUniform1fv", None, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]
        )
        self.glUniform4fv = self._bind(
            "glUniform4fv", None, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]
        )

        self.glGenVertexArrays = self._bind(
            "glGenVertexArrays", None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        )
        self.glBindVertexArray = self._bind("glBindVertexArray", None, [ctypes.c_uint])
        self.glDeleteVertexArrays = self._bind(
            "glDeleteVertexArrays", None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        )
        self.glGenBuffers = self._bind(
            "glGenBuffers", None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        )
        self.glDeleteBuffers = self._bind(
            "glDeleteBuffers", None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        )
        self.glBindBuffer = self._bind("glBindBuffer", None, [ctypes.c_uint, ctypes.c_uint])
        self.glBufferData = self._bind(
            "glBufferData",
            None,
            [ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint],
        )
        self.glEnableVertexAttribArray = self._bind(
            "glEnableVertexAttribArray", None, [ctypes.c_uint]
        )
        self.glVertexAttribPointer = self._bind(
            "glVertexAttribPointer",
            None,
            [
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_ubyte,
                ctypes.c_int,
                ctypes.c_void_p,
            ],
        )
        self.glDrawArrays = self._bind("glDrawArrays", None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int])

        self.glGenTextures = self._bind("glGenTextures", None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)])
        self.glDeleteTextures = self._bind(
            "glDeleteTextures", None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        )
        self.glActiveTexture = self._bind("glActiveTexture", None, [ctypes.c_uint])
        self.glBindTexture = self._bind("glBindTexture", None, [ctypes.c_uint, ctypes.c_uint])
        self.glTexParameteri = self._bind("glTexParameteri", None, [ctypes.c_uint, ctypes.c_uint, ctypes.c_int])
        self.glGenerateMipmap = self._bind("glGenerateMipmap", None, [ctypes.c_uint])
        self.glTexImage2D = self._bind(
            "glTexImage2D",
            None,
            [
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_void_p,
            ],
        )
        self.glTexSubImage2D = self._bind(
            "glTexSubImage2D",
            None,
            [
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_void_p,
            ],
        )

    def _bind(self, name: str, restype: Any, argtypes: list[Any]) -> Any:
        addr = self._get_proc(name.encode("ascii"))
        if not addr:
            fn = getattr(self._lib, name)
            fn.restype = restype
            fn.argtypes = argtypes
            return fn
        proto = ctypes.CFUNCTYPE(restype, *argtypes)
        return proto(addr)

    def compile_program(self, vert_src: str, frag_src: str) -> int:
        vs = self._compile_shader(GL_VERTEX_SHADER, vert_src)
        fs = self._compile_shader(GL_FRAGMENT_SHADER, frag_src)
        prog = self.glCreateProgram()
        self.glAttachShader(prog, vs)
        self.glAttachShader(prog, fs)
        self.glLinkProgram(prog)
        status = ctypes.c_int(0)
        self.glGetProgramiv(prog, GL_LINK_STATUS, ctypes.byref(status))
        self.glDeleteShader(vs)
        self.glDeleteShader(fs)
        if not status.value:
            buf = ctypes.create_string_buffer(2048)
            length = ctypes.c_int(0)
            self.glGetProgramInfoLog(prog, 2048, ctypes.byref(length), buf)
            self.glDeleteProgram(prog)
            raise RuntimeError(f"GLSL 链接失败: {buf.value.decode('utf-8', 'replace')}")
        return prog

    def _compile_shader(self, stype: int, src: str) -> int:
        shader = self.glCreateShader(stype)
        bsrc = src.encode("utf-8")
        c_str = ctypes.c_char_p(bsrc)
        c_len = ctypes.c_int(len(bsrc))
        self.glShaderSource(shader, 1, ctypes.byref(c_str), ctypes.byref(c_len))
        self.glCompileShader(shader)
        status = ctypes.c_int(0)
        self.glGetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(status))
        if not status.value:
            buf = ctypes.create_string_buffer(2048)
            length = ctypes.c_int(0)
            self.glGetShaderInfoLog(shader, 2048, ctypes.byref(length), buf)
            self.glDeleteShader(shader)
            kind = "Vertex" if stype == GL_VERTEX_SHADER else "Fragment"
            raise RuntimeError(f"{kind} Shader 编译失败:\n{buf.value.decode('utf-8', 'replace')}")
        return shader

