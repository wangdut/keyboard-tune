# -*- coding: utf-8 -*-
"""
声学度量库（纯 Python，不依赖 numpy）。

check_acoustics.py 与调参探针共用同一份实现，避免"验收脚本一套口径、标定脚本另一套
口径"导致结论互相打架 —— 这次升级里至少有三个"音色不合格"其实都是度量写错。

各函数的口径选择理由见函数 docstring。
"""
import math

SAMPLE_RATE = 44100
SR = SAMPLE_RATE


def decode(pcm, n):
    """int16 小端 PCM -> [-1,1) 浮点序列。"""
    out = [0.0] * n
    for i in range(n):
        v = pcm[2 * i] | (pcm[2 * i + 1] << 8)
        out[i] = (v - 65536 if v >= 32768 else v) / 32768.0
    return out


def envelope(s, width=0.005):
    """
    滑动窗 RMS 包络。淘汰样本必须从累加器里减掉 —— 写成只加不减的"包络"其实是
    单调累积能量，attack/tail 会一起量成假数字（这次就踩过：修好后 attack 从
    78~124ms 变成真实的 11~17ms）。
    """
    w = max(1, int(width * SR))
    out = []
    buf = []
    acc = 0.0
    for v in s:
        x = v * v
        buf.append(x)
        acc += x
        if len(buf) > w:
            acc -= buf.pop(0)
        if len(buf) == w:
            out.append(math.sqrt(acc / w))
    return out


def plate_ref(e):
    """平台参考电平 = 前 50ms 包络最大值（取全曲最大值会让混响爬坡冒充起音）。"""
    return max(e[:min(len(e), int(0.050 * SR))])


def attack_ms(s):
    """10% -> 90% 平台电平的时间（需求 §一.3 的 Attack 定义）。"""
    e = envelope(s)
    ref = plate_ref(e)
    lim = min(len(e), int(0.050 * SR))
    i0 = next((i for i in range(lim) if e[i] >= 0.10 * ref), None)
    if i0 is None:
        raise AssertionError("包络在前 50ms 内没爬过 10%%，峰值=%.4f" % max(s))
    i1 = next((i for i in range(i0, len(e)) if e[i] >= 0.90 * ref), len(e) - 1)
    return (i1 - i0) * 1000.0 / SR, e, ref


def tail_ms(s):
    """返回 (-60dB 落点 ms, 是否被缓存截断)。截断的尾巴不算"自然尾音"。"""
    e = envelope(s)
    ref = plate_ref(e)
    thr = 0.001 * ref
    last = max((i for i, v in enumerate(e) if v >= thr), default=0)
    return last * 1000.0 / SR, e[-1] >= thr


def tone_mag(s, i0, i1, freq):
    """Hann 加窗 Goertzel 归一化幅度，用于同一窗内不同频点的相对比较。"""
    m = i1 - i0
    if m < 8 or freq >= SR * 0.5:
        return 0.0
    coeff = 2.0 * math.cos(2.0 * math.pi * freq / SR)
    y1 = y2 = 0.0
    for k in range(m):
        win = 0.5 - 0.5 * math.cos(2.0 * math.pi * k / m)
        y0 = s[i0 + k] * win + coeff * y1 - y2
        y2 = y1
        y1 = y0
    p = y1 * y1 + y2 * y2 - coeff * y1 * y2
    return math.sqrt(p if p > 0.0 else 0.0) / (m * 0.5)


def biquad_filt(s, kind, fc, q=0.7071067811865476):
    """RBJ 二阶节，返回过滤后的序列。kind: lp / hp / bp。"""
    fc = min(fc, SR * 0.45)
    w0 = 2.0 * math.pi * fc / SR
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    a0 = 1.0 + alpha
    if kind == "lp":
        b0, b1, b2 = (1.0 - cw) * 0.5, 1.0 - cw, (1.0 - cw) * 0.5
    elif kind == "hp":
        b0, b1, b2 = (1.0 + cw) * 0.5, -(1.0 + cw), (1.0 + cw) * 0.5
    else:                                    # 峰值增益约 1 的带通
        b0, b1, b2 = sw * 0.5, 0.0, -sw * 0.5
    a1, a2 = -2.0 * cw / a0, (1.0 - alpha) / a0
    b0, b1, b2 = b0 / a0, b1 / a0, b2 / a0
    x1 = x2 = y1 = y2 = 0.0
    out = [0.0] * len(s)
    for i, x in enumerate(s):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[i] = y
        x2, x1 = x1, x
        y2, y1 = y1, y
    return out


def biquad_rms(s, kind, fc, q=0.7071067811865476):
    y = biquad_filt(s, kind, fc, q)
    return math.sqrt(sum(v * v for v in y) / len(y))


def air_db(s, f0, i1=None):
    """
    onset 高频带(4~8k) 与基频带的能量差（dB）。
    不能用"4~8k 占全曲总能量的比例"量空气感：那个分母被基频整条尾音主导，一个
    -18dB 的 8ms 琴槌 tick 只会算出 0.02% 占比，怎么加参数都是"没有空气感"。
    """
    seg = s if i1 is None else s[:i1]
    mid = biquad_rms(seg, "bp", f0 * 1.3, 1.2)
    hf = biquad_rms(seg, "bp", 5600.0, 1.2)
    return 20.0 * math.log10(hf / mid + 1e-12)


def crossing_freq(s, i0, i1):
    """
    上行过零点（线性插值）的**相邻间隔中位数** -> 频率。指数衰减只改幅度不改过零
    时刻，因此能到 0.2Hz 量级，足够分辨 ±5 音分（262Hz 处 1 音分 = 0.15Hz）。

    为什么是中位数而不是对过零点序号做最小二乘：噪声（混响里返回的琴槌"房间噪声"）
    只要在窗内多推/漏掉一个过零点，后面全部序号集体偏一位，斜率会错整整一个
    "1/窗长" —— 160ms 窗就是 6.25Hz，在 130Hz 处等于 83 音分。中位数对单个异常间隔
    完全免疫（一个假过零点只造成一长一短两个离群值），而 20 个间隔的中位数标准误
    只有 ~0.3 个样本，折合 0.7 音分，精度够把关 ±5 音分。

    使用前应当先低通到 ~1.3 倍基频，否则非谐模态会拉高过零率（偏 1~4 音分）。
    """
    ts = []
    for i in range(i0 + 1, i1):
        a, b = s[i - 1], s[i]
        if a < 0.0 <= b:
            ts.append((i - 1) + (-a) / (b - a))
    m = len(ts)
    if m < 6:
        return None
    gaps = sorted(ts[k + 1] - ts[k] for k in range(m - 1))
    n = len(gaps)
    med = gaps[n // 2] if n % 2 else 0.5 * (gaps[n // 2 - 1] + gaps[n // 2])
    if med <= 1e-6:
        return None
    return SR / med
