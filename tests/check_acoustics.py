# -*- coding: utf-8 -*-
"""
音色声学验收：把需求里那些可量化的听觉指标变成可回归的数字，而不是一句"听着不像电子音"。

  python tests/check_acoustics.py

度量口径（全部在 tests/metrics.py 里，调参探针共用同一份实现）：
  攻击  5ms 窗 RMS 包络从 10% 升到 90% 平台电平所用时间（平台电平取前 50ms 的最大值）
  尾音  包络最后一次不低于 -60dB（平台电平的 1/1000）的时刻，即 RT60 定义；
        同时要求缓存末尾确实在 -60dB 之下，否则算"被截断"而不是真尾音
  湿声  关掉混响渲染同一音做参照，两版逐样本相减就是湿声分量，量它自己的能量占比。
        峰值定标在混响之前所以两版干声相同；不能用总能量差（交叉项会把它读成负数），
        也不能做峰值对齐（那等于把湿声乘回去抵掉）
  音高  先低通到 1.3 倍基频再取上行过零间隔的中位数。中位数而不是最小二乘：一个
        被噪声多推出来的过零点会把后面所有序号集体带偏一位，160ms 窗里就是 83 音分。
        量测在关掉混响的那一版上做：混响会把琴槌噪声重新散布到整条尾音，低基频下
        读数会从 ±0.1 音分散到 ±5 音分（混响是线性时不变网络，不可能改基频）
  RT60  往混响网络灌一个单位冲击，直接量冲激响应的 -60dB 落点。不能用"含混响总尾音
        减干声尾音"：总尾音由干声衰减支配，那只能量出几毫秒，而需求限的是房间时间
  泛音  量测窗 = 击弦后 3~22ms。长窗平均会把只有几十毫秒寿命的高次模态抹成 -80dB，
        那是度量把物理搞反了：人耳听到的泛音列就是击弦那一下的频谱亮度
  空气感  onset 的 4~8k 带与基频带的能量差（dB）。用"占全曲总能量比"会被基频尾音
        淹掉，一个 -18dB 的 8ms 琴槌 tick 只会算出 0.02% 占比
  非谐  同一时刻分别在"设计模态频率"和"最近的整数倍谐波"上做 Hann 加窗 Goertzel：
        方波/锯齿的能量只可能落在整数倍上，若非谐位置明显更强即为非谐模态列
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from keyboard_tune import voice as V                       # noqa: E402
from keyboard_tune import notes as N                        # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics import (decode, envelope, plate_ref, attack_ms, tail_ms,   # noqa: E402
                     tone_mag, biquad_filt, biquad_rms, air_db,
                     crossing_freq)
from metrics import SR as MS                                  # noqa: E402

SR = V.SAMPLE_RATE
if SR != MS:
    raise SystemExit("voice.SAMPLE_RATE=%d 与 metrics.SR=%d 不一致，度量会全错" % (SR, MS))
WIN_MS = 250.0          # 响量统计窗：只算可听部分，不被缓存长度稀释
# 标定（voice.LOUD_WIN_SEC）与验收（WIN_MS）必须量同一个窗，否则"标到 0.875 的量"
# 和"验收量的量"不是同一件事，分区响量验收会凭空空挂或凭空空过。
if abs(V.LOUD_WIN_SEC * 1000.0 - WIN_MS) > 1e-6:
    raise SystemExit("响量窗不一致：标定 %.1fms 验收 %.1fms"
                     % (V.LOUD_WIN_SEC * 1000.0, WIN_MS))

# Windows 控制台常被重定向为 GBK，数学符号（− ≤ ≥）编不出去会直接抛 UnicodeEncodeError，
# 把结论淹没在 traceback 里。errors=replace 让验收脚本永远能跑完并打印结果。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

print("音色声学验收")
print("-" * 46)

FAILS = []


def case(name):
    def deco(fn):
        try:
            msg = fn()
            print("ok   %-28s %s" % (name, msg or ""))
        except Exception as exc:
            import traceback
            FAILS.append(name)
            # 失败原因必须跟在 FAIL 行上：traceback 走 stderr，与 stdout 汇流时会被
            # 重排到最后，PowerShell 里看不出哪一段 traceback 属于哪一项
            print("FAIL %s: %s" % (name, exc))
            traceback.print_exc(file=sys.stdout)
    return deco


# --------------------------------------------------------------------- 度量工具
# 全部从 tests/metrics.py 取：验收与调参探针共用一份实现，避免两边口径打架。
def render(pack, midi, timbre, variant=0, quality="full"):
    pcm, n = pack.render(midi, timbre, variant, quality)
    return pcm, decode(pcm, n)


# 验收对象必须是发行路径上的那个对象：make_pack() 会按实测标定分区增益，直接
# new VoicePack() 量到的是"出厂字面值"，跟真实出声时不是同一套参数。
# slots=真实映射表 + dense=逐音高稠密标定：采样库就是照这个口径建的，运行时从
# manifest 恢复同一条曲线，所以"用户听到的"就是这里量的这一份。
KEYMAP = N.all_slots()
PACK = V.make_pack(slots=KEYMAP, dense=True)
# 覆盖四个家族 x 四种敲击 x 低/中/高音区（"keyd" 是空格真实用的次重音档）
SLOTS = [(60, "keya"), (69, "keyb"), (48, "keyc"), (45, "keyc"),
         (76, "tickb"), (81, "ticka"), (72, "softa"), (64, "softc"),
         (45, "warmb"), (57, "warmc"), (46, "keya"), (81, "tickc"),
         (48, "keyd"), (72, "softd")]
VARIANTS = range(PACK.variants)


# --------------------------------------------------------------------- 结构完整
@case("输出无 NaN/越界，且不削顶（峰值 ≤0.90）")
def t_structure():
    worst = 0.0
    for midi, timbre in SLOTS:
        for v in VARIANTS:
            pcm, s = render(PACK, midi, timbre, v)
            for x in s:
                if math.isnan(x) or math.isinf(x):
                    raise AssertionError("出现 NaN/Inf: midi=%d %s v=%d" % (midi, timbre, v))
            pk = max(abs(x) for x in s)
            worst = max(worst, pk)
            if pk > 0.90:
                raise AssertionError("峰值 %.3f 超 0.90（midi=%d %s）" % (pk, midi, timbre))
            # 连续 3 个同向满幅样本 = 真实削顶特征，归一化后不该出现
            rail = 0
            for i in range(1, len(s)):
                if abs(s[i]) >= 0.995 and abs(s[i - 1]) >= 0.995:
                    rail += 1
                    if rail >= 3:
                        raise AssertionError("连续削顶 (midi=%d %s)" % (midi, timbre))
                else:
                    rail = 0
    return "max peak=%.3f" % worst


@case("确定性：同参数两次渲染逐字节一致，变体之间必须不同")
def t_determinism():
    seen = {}
    for midi, timbre, var in ((60, "keya", 0), (72, "softb", 3), (45, "warmc", 5)):
        a = render(PACK, midi, timbre, var)[0]
        b = render(PACK, midi, timbre, var)[0]
        if a != b:
            raise AssertionError("同一 (音高,音色,变体) 两次渲染不一致，采样库无法做指纹校验")
        seen[(midi, timbre)] = seen.get((midi, timbre)) or {}
        if var in seen[(midi, timbre)]:
            pass
        seen[(midi, timbre)][var] = a
    for key, vs in seen.items():
        vals = list(vs.values())
        if len(set(vals)) != len(vals):
            raise AssertionError("变体之间出现完全相同的波形: %s" % (key,))
    return "%d 组变体互不相同" % sum(len(v) for v in seen.values())


# --------------------------------------------------------------------- 时间维度
@case("起音 10~30ms（需求 §一.3）")
def t_attack():
    lo, hi = 1e9, 0.0
    for midi, timbre in SLOTS:
        a, _, _ = attack_ms(render(PACK, midi, timbre, 1)[1])
        if not (10.0 <= a <= 30.0):
            raise AssertionError("midi=%d %s 攻击 %.1fms 不在 10~30ms" % (midi, timbre, a))
        lo, hi = min(lo, a), max(hi, a)
    return "实测 %.1f~%.1fms" % (lo, hi)


@case("自然尾音 100~300ms 且未被缓存截断（需求 §一.3）")
def t_tail():
    lo, hi = 1e9, 0.0
    for midi, timbre in SLOTS:
        s = render(PACK, midi, timbre, 1)[1]
        t, trunc = tail_ms(s)
        if trunc:
            raise AssertionError("midi=%d %s 的 -60dB 落点还在缓存末尾，尾音被截断" % (midi, timbre))
        if not (100.0 <= t <= 300.0):
            raise AssertionError("midi=%d %s 尾音 %.1fms 不在 100~300ms" % (midi, timbre, t))
        lo, hi = min(lo, t), max(hi, t)
    return "实测 %.0f~%.0fms" % (lo, hi)


@case("快取：从按下到出声的包络起振 <5ms（延迟预算里合成不是瓶颈）")
def t_onset():
    worst = 0.0
    for midi, timbre in SLOTS:
        e = envelope(render(PACK, midi, timbre, 1)[1])
        ref = plate_ref(e)
        i0 = next((i for i in range(min(len(e), int(0.05 * SR))) if e[i] >= 0.10 * ref), None)
        if i0 is None:
            raise AssertionError("前 50ms 内没起振: midi=%d %s" % (midi, timbre))
        worst = max(worst, i0 * 1000.0 / SR)
    return "10%% 电平最晚出现在 %.1fms" % worst


# --------------------------------------------------------------------- 混响维度
@case("湿声占比 ≤10% 且确实存在（需求 §一.3）")
def t_reverb():
    """
    湿声占比必须直接量湿声分量自身的能量：scale 定标在混响之前，所以 reverb_wet=0
    与 reverb_wet>0 两版的 scale 完全相同、干声部分逐样本相同（量化抖动同种子），于是
    (s1 - s0) 就是湿声。不能用总能量差 (e1-e0)：湿声是干声延迟 ~20ms 的副本，两者有一
    个交叉项，对某些基频是负的，能把湿声自身的能量淹没，实测会把 6% 的湿声读成 -1.5%
    （"加了混响反而不增能量"），那是度量口径错了。
    也不能做"峰值对齐"：混响版峰值本来就略高（尾巴叠上去的），对齐等于把湿声乘掉。
    """
    dry = V.VoicePack(reverb_wet=0.0)
    ratios = []
    for midi, timbre in ((60, "keya"), (45, "warmb"), (81, "ticka")):
        s1 = render(PACK, midi, timbre, 1)[1]
        s0 = render(dry, midi, timbre, 1)[1]
        m = min(int(WIN_MS / 1000.0 * SR), len(s1), len(s0))
        dif = max(abs(s1[i] - s0[i]) for i in range(m))
        ew = sum((s1[i] - s0[i]) ** 2 for i in range(m))
        e1 = sum(x * x for x in s1[:m])
        if dif <= 1e-4:
            raise AssertionError("midi=%d %s 加混响前后几乎逐样本相同，混响没接进去"
                                 % (midi, timbre))
        ratios.append(ew / e1)
    lo, hi = min(ratios), max(ratios)
    if not (0.01 <= lo <= hi <= 0.10):
        raise AssertionError("湿声占比 %.1f%%~%.1f%% 不在 1%%~10%%" % (lo * 100, hi * 100))
    return "实测 %.1f%%~%.1f%%（配置 reverb_wet=%.2f）" % (
        lo * 100, hi * 100, PACK.reverb_wet)


@case("混响时间 RT60 ≤200ms（需求 §一.3）")
def t_rt60():
    """
    直接量混响网络自身的冲激响应衰减：往 _reverb 里灌一个单位冲击，干声只剩首样本，
    之后整条尾巴都是湿声 —— 这才是房间混响的 RT60。用"含混响总尾音减干声尾音"量不
    到它：总尾音由干声衰减支配，混响只被拖长几毫秒（需求限的是房间时间，不是这个）。
    """
    n = int(0.45 * SR)
    out = []
    for wet, rt in ((PACK.reverb_wet, PACK.reverb_rt), (V.WET_LIMIT, 0.20)):
        pack = V.VoicePack(reverb_wet=wet, reverb_rt=rt)
        buf = [0.0] * n
        buf[0] = 1.0
        pack._reverb(buf, n, False)
        buf[0] = 0.0                       # 去掉直达（干声）那一下
        e = envelope(buf)
        ref = max(e[:int(0.010 * SR)])
        thr = 0.001 * ref
        last = max((i for i, v in enumerate(e) if v >= thr), default=0)
        ms = last * 1000.0 / SR
        if ms < 20.0:
            raise AssertionError("wet=%.2f 的 RT60 只有 %.0fms，算不上房间混响"
                                 % (wet, ms))
        if ms > rt * 1000.0 + 40.0:
            raise AssertionError("wet=%.2f 实测 RT60 %.0fms，与配置 %.0fms 对不上"
                                 % (wet, ms, rt * 1000))
        if ms > 200.0:
            raise AssertionError("RT60 %.0fms 超 200ms" % ms)
        out.append("wet=%.2f -> %.0fms" % (wet, ms))
    return ", ".join(out)


# --------------------------------------------------------------------- 音高维度
@case("音高随机偏移实测 ≤±5 音分，并与设计值一致（需求 §一.3）")
def t_cents():
    """
    量的是关掉混响那一版，这不是偷工：混响会把琴槌噪声重新散布到整条尾音里，而低
    基频（midi 48 = 131Hz，160ms 窗只容得下 21 个周期）的过零点会被它随机多推/漏掉
    一个，实测偏差从 ±0.1 音分炸到 ±5 音分（已实测）—— 那是量测噪声而不是音高噪声：
    混响是线性时不变网络，不会改基频，也不可能跟合成分叉。
    干声版下这个估计器很稳：三个音高 × 三档敲击 × 六个变体的读数全部只偏
    -0.5~-0.7 音分（基频那对微失谐振荡器的幅度加权），所以既能把关绝对上限 ±5 音分，
    也能把关"设计音分表真的接到了合成上"（残差 ≤1.5 音分）。
    """
    dry = V.VoicePack(reverb_wet=0.0)
    i0 = int(0.040 * SR)
    i1 = min(int(0.200 * SR), int(WIN_MS / 1000.0 * SR))
    worst_meas = 0.0
    worst_res = 0.0
    worst_design = 0.0
    for midi in (48, 60, 76):
        base0 = V.midi_to_freq(midi)
        for letter in ("a", "b", "c"):
            strike_cents = PACK.strike[letter][3]
            for var in VARIANTS:
                design = min(V.CENT_LIMIT, max(
                    -V.CENT_LIMIT, PACK.cents[var % len(PACK.cents)] + strike_cents))
                _, s = render(dry, midi, "key" + letter, var)
                # 先低通到 1.3 倍基频再数过零点：非谐模态（3.9/2.76 倍）与琴槌噪声会
                # 拉高过零率，直接数原始波形会偏 3~4 音分（曾误报为超 ±5）。
                f = crossing_freq(biquad_filt(s, "lp", base0 * 1.3),
                                  i0, min(i1, len(s)))
                if f is None:
                    raise AssertionError("过零点太少，无法测音高: midi=%d" % midi)
                meas = 1200.0 * math.log(f / base0, 2.0)
                if abs(meas) > 5.7:              # 0.7 音分留给估计器的系统偏差
                    raise AssertionError("midi=%d key%s v=%d 实测 %+0.2f 音分超 ±5"
                                         % (midi, letter, var, meas))
                worst_meas = max(worst_meas, abs(meas))
                worst_design = max(worst_design, abs(design))
                worst_res = max(worst_res, abs(meas - design))
    if worst_res > 1.5:
        raise AssertionError("实测与设计音分差 %.2f 音分，随机化没正确接到合成上" % worst_res)
    return "设计最大 %.2f 音分（上限 ±5），实测读数差 %.2f 音分估计器偏差" % (
        worst_design, worst_meas - worst_design)


@case("模态列是非谐的，不是方波/锯齿的整数倍谐波列（需求 §一.2）")
def t_inharmonic():
    checks = []
    for voice in ("marimba", "glock"):
        bar = V.BARS[voice]
        midi = 60
        f0 = V.midi_to_freq(midi)
        _, s = render(V.VoicePack(family_voice={"key": (voice, 1.0)}), midi, "keya", 1)
        i0, i1 = int(0.006 * SR), min(len(s), int(0.090 * SR))
        for ratio in bar["ratios"][1:3]:
            near = round(ratio)
            if abs(ratio - near) < 0.02:
                continue
            a = tone_mag(s, i0, i1, f0 * ratio)
            b = tone_mag(s, i0, i1, f0 * near)
            if a <= 0:
                raise AssertionError("%s 的 %.2f 次模态没有能量" % (voice, ratio))
            db = 20 * math.log10(a / b) if b > 0 else 99.0
            if db < 3.0:
                raise AssertionError("%s 模态 %.2f 处并不比整数倍 %.1f 强（%.1fdB）-> 谐波列"
                                    % (voice, ratio, near, db))
            checks.append("%s %.2f>%.1f +%.0fdB" % (voice, ratio, near, db))
    return "; ".join(checks)


@case("泛音数量 ≥3 且不是单薄纯音（需求 §一.3）")
def t_partials():
    lo = 99
    detail = []
    for voice, timbre in (("marimba", "keya"), ("wood", "ticka"),
                          ("glock", "softa"), ("lowbar", "warma")):
        bar = V.BARS[voice]
        fam = timbre[:-1]
        fv = {fam: (voice, 1.0), "key": ("marimba", 1.0)}
        _, s = render(V.VoicePack(family_voice=fv), 60, timbre, 1)
        # 量测窗 = 击弦后的前 3~22ms。不能用几十毫秒的长窗：这里的衰减常数按
        # "-60dB 落在 tau" 标定，高次模态本来就只有几十毫秒寿命，长窗平均下来会把
        # 它们抹成 −80dB，那是度量把物理搞反了而不是音色单薄 —— 人耳听到的正是
        # 击弦那一下的频谱亮度。
        i0, i1 = int(0.003 * SR), min(len(s), int(0.022 * SR))
        base = V.midi_to_freq(60)
        fund = tone_mag(s, i0, i1, base)
        n = 0
        dbs = []
        for ratio in bar["ratios"]:
            f = base * ratio
            if f >= SR * 0.45 or ratio == 1.0:
                continue
            m = tone_mag(s, i0, i1, f)
            d = 20 * math.log10(m / fund + 1e-12)
            dbs.append(d)
            if d > -40.0:
                n += 1
        if n < 3:
            raise AssertionError("%s 只有 %d 个 -40dB 以上的泛音（%s），太单薄"
                                 % (voice, n, " ".join("%+.0f" % d for d in dbs)))
        lo = min(lo, n)
        detail.append("%s:%d" % (voice, n))
    return " ".join(detail)


@case("高频不刺耳：>8kHz 能量 <=3%，但 onset 空气感 >=-34dB（需求 §一.2/3）")
def t_hf():
    hi_worst = 0.0
    air_worst = 0.0
    M12 = int(0.012 * SR)
    for midi, timbre in SLOTS:
        _, s = render(PACK, midi, timbre, 1)
        tot = sum(x * x for x in s) or 1e-12
        above = biquad_rms(s, "hp", 8000.0)
        above2 = above * above * len(s) / tot
        if above2 > 0.03:
            raise AssertionError("midi=%d %s 的 >8kHz 占比 %.1f%% 会刺耳" %
                                 (midi, timbre, above2 * 100))
        hi_worst = max(hi_worst, above2)
        ad = air_db(s, V.midi_to_freq(midi), M12)
        if ad < -34.0:
            raise AssertionError("midi=%d %s 几乎没有空气感（onset 4~8k 比基频带低 "
                                 "%.1fdB），会发闷" % (midi, timbre, ad))
        if ad > -10.0:
            raise AssertionError("midi=%d %s 高频太冲（onset %.1fdB），会刺耳" % (midi, timbre, ad))
        air_worst = max(air_worst, -ad)
    return ">8k 最多 %.2f%%，onset 空气感最弱 %.1fdB" % (hi_worst * 100, -air_worst)


@case("低音区有共鸣不闷：150Hz 以下能量占比 ≥8%")
def t_lowend():
    ratios = []
    for midi in (45, 48, 52):
        _, s = render(PACK, midi, "warmb", 1)
        tot = sum(x * x for x in s) or 1e-12
        low = biquad_rms(s, "lp", 150.0)
        ratios.append(low * low * len(s) / tot)
    lo = min(ratios)
    if lo < 0.08:
        raise AssertionError("低音区 150Hz 以下只占 %.1f%%，共鸣不足" % (lo * 100))
    return "最低 %.1f%%" % (lo * 100)


# --------------------------------------------------------------------- 分区维度
def zone_ratios(pack, slots):
    """
    -> {家族: [(midi, 干声比, 含混响比)]}，对 pack 当前的标定曲线。

    两种口径都要：干声比是设计量（击键起音那一瞬的相对响度，人耳判断"谁抢谁"
    主要看它），含混响比是喇叭真正出来的东西。混响的传输增益随基频与梳状延迟的
    相位关系变，两者会差千分之几到 2%，只量一个都说不出结论。
    探针用 _neutral()：它自带当前 family_trim，并把 cents/loudness_db 归零（量分区比
    时 ±0.55dB 的 humanize 拖动就是噪声）。
    """
    dry, wet = pack._neutral(reverb_wet=0.0), pack._neutral()
    cache = {}

    def loud(probe, midi, timbre):
        key = (id(probe), midi, timbre)
        if key not in cache:
            pcm, n = probe.render(midi, timbre, 0, "full")
            cache[key] = V._rms(pcm, n)
        return cache[key]

    rows = {}
    for midi, timbre in slots:
        fam = timbre[:-1]
        if fam == "key":
            continue
        d0, w0 = loud(dry, midi, "keya"), loud(wet, midi, "keya")
        rows.setdefault(fam, []).append(
            (midi, loud(dry, midi, fam + "a") / d0 if d0 > 1e-12 else 1.0,
                  loud(wet, midi, fam + "a") / w0 if w0 > 1e-12 else 1.0))
    return rows


@case("分区响量：非主打字区比主键区低 10~15%（需求 §三）")
def t_zone_gain():
    """
    量真实映射表上每一个非主键区槽位，而不是抽一个 midi60。

    为什么不能只抽一点：分区补偿是一条随音高插值的曲线（见 calibrate 的注释：裸
    响量比逐半音振荡），单点验收只能证明那个点上对。实测：全局 3 点标定时 warm 家族
    有 4 个槽位落在带外（0.838~0.875），而只看 midi60 时它是 0.875。“验收绿了但产
    品不守需求”正是这种抽点测法造出来的。

    启动时那份每家族 3 点的廉价标定（~0.4s）只出现在"第一次运行、后台还在建库"的
    窗口里，这里作为对照：它的个别槽位可以贴到 0.85/0.90 外几厘，但家族均值必须进带。
    """
    bad = []
    got = {}
    n_slots = 0
    for fam, rows in sorted(zone_ratios(PACK, KEYMAP).items()):
        ds = [d for _m, d, _w in rows]
        ws = [w for _m, _d, w in rows]
        n_slots += len(rows)
        got[fam] = (min(ds), max(ds), min(ws), max(ws))
        for midi, d, w in rows:
            if not (0.85 <= d <= 0.90 and 0.85 <= w <= 0.90):
                bad.append("%s@%d 干%.3f 湿%.3f" % (fam, midi, d, w))
    live = V.make_pack(slots=KEYMAP)           # 启动路径：每家族 3 点
    live_means = dict((f, sum(w for _m, _d, w in r) / len(r))
                      for f, r in zone_ratios(live, KEYMAP).items())
    live_out = ["%s 均值%.3f" % (f, m) for f, m in sorted(live_means.items())
                if not 0.85 <= m <= 0.90]
    if bad or live_out:
        flat = {f: (V.FAMILY_VOICE[f][0], 1.0) for f in V.FAMILY_VOICE}
        naked = {f: (min(d for _m, d, _w in r), max(d for _m, d, _w in r))
                 for f, r in zone_ratios(V.VoicePack(family_voice=flat), KEYMAP).items()}
        raise AssertionError("分区 RMS 比不在 0.85~0.90（即低 10~15%%）: %s；启动标定 %s"
                             "（发行曲线 %s，裸增益比 %s）"
                             % (", ".join(bad) or "无", ", ".join(live_out) or "无",
                                {f: [round(v, 3) for v in r] for f, r in got.items()},
                                {f: [round(v, 3) for v in r] for f, r in naked.items()}))
    return "%d 槽全部进带；干声 %s；含混响 %s；启动3点均值 %s" % (
        n_slots,
        " ".join("%s=%.3f~%.3f" % (f, r[0], r[1]) for f, r in sorted(got.items())),
        " ".join("%s=%.3f~%.3f" % (f, r[2], r[3]) for f, r in sorted(got.items())),
        " ".join("%s=%.3f" % (f, m) for f, m in sorted(live_means.items())))


@case("大按键重音档比常规档更低沉更响、琴条更厚（需求 §三）")
def t_accent():
    """
    重音层不是只把音量拉高：需求写的是"偏低沉的同风格打击音色 + 节拍重音"，所以
    同时验三件事：琴条本体的"高次/基频"必须下来、250ms 响量必须上去、琴条必须换成
    低音模型。验的是映射表真正用上的那些 (音高, 重音档)，而不是拿 midi60 手造一个：
    换琴条、换档位这类接线错了，抽个理想点测照样能绿。

    “低沉”为什么不拿 onset 带通亮度（4~8k 对基频带那类比）当证据：实测 8 个真槽位里
    有 4 个的带通亮度反而更高（+0.9~+6.3dB）。机制是重音档把宽带琴槌噪声（thump 增益
    1.65）抬成了整条波形的峰值来源，而归一化发生在混响与 tilt 之前——峰值定标把相干
    音体整体压小，任何"带通能量比"读数都在跟定标打架，量到的是噪声带而不是琴条。换
    低音琴条的 thump/air 的 Q 与幅度也救不回来：_add_burst 里 s = amp/pk 按滤波后峰值
    反归一，Q 变窄的同时把 skirt 又抬回去，实测最差值只从 +6.3dB 降到 +5.8dB。

    直接证据是琴条本体高次模态相对基频的比例：在击弦窗（3~22ms，与泛音列验收同窗）
    内逐模态做 Goertzel。整体缩放会被比值抵消，所以不受峰值定标影响。实测 8 个真槽位
    一致下降 5.2~7.6dB（设计值 20log10(0.58)=-4.7dB，低音琴条自身 amps 更小故更多）。
    窗不能取 30~120ms：那时常规档的高次泛音已衰减到噪声底，比的是噪声比噪声，实测在
    59keyd 上读出 +21dB 的假象。
    """
    win = int(WIN_MS / 1000.0 * SR)
    I0, I1 = int(0.003 * SR), int(0.022 * SR)

    def metrics(midi, tim):
        """(琴条高次/基频 dB, 250ms 响量)；模态频率取该档自己那条琴条的设计值。"""
        _pcm, s = render(PACK, midi, tim, 1)
        f0 = V.midi_to_freq(midi)
        j1 = min(I1, len(s))
        fund = tone_mag(s, I0, j1, f0)
        ups = 0.0
        for ratio in PACK._plan(tim)[0]["ratios"][1:]:
            f = f0 * ratio
            if f < SR * 0.45:
                ups += tone_mag(s, I0, j1, f)
        upper = 20.0 * math.log10(ups / fund) if fund > 1e-12 and ups > 0.0 else -900.0
        return upper, math.sqrt(sum(x * x for x in s[:win]) / win)

    DARK_DB = 3.0        # 低沉下限：设计值 4.7dB 的一半，留出实测余量
    real = sorted(set((midi, tim) for midi, tim in KEYMAP
                      if tim[-1:] in V.ACCENT))
    bad = []
    rows = []
    for midi, tim in real:
        ua, ra = metrics(midi, tim[:-1] + "a")
        ut, rt = metrics(midi, tim)
        if not (ut <= ua - DARK_DB and rt >= ra * 1.05):
            bad.append("%d%s 高次%.1fdB/响量+%.0f%%" % (
                midi, tim, ut - ua, (rt / ra - 1) * 100))
        rows.append("%d%s:%.1fdB/+%.0f%%" % (midi, tim[-1], ut - ua, (rt / ra - 1) * 100))
    if bad:
        raise AssertionError("重音档不比常规档更低沉更响: %s" % ", ".join(bad))
    # 重音档必须换发声体：只靠闷击/高次增益是滤波级的差别，听不出"另一件乐器"
    wrong = [tim for _midi, tim in real
             if PACK._plan(tim)[0] is not V.BARS[PACK.accent_bar]]
    if wrong:
        raise AssertionError("重音档没用上低音琴条 %r: %s" % (PACK.accent_bar, wrong))
    # c/d 是同一层的两个档位（主键区左侧 Tab-CapsLock-Shift 相连，单档会让相邻键的
    # 音色标识完全相同）：d 必须落在 a 与 c 之间，否则两档就是随手填的数而不是"次重音"
    ua, ra = metrics(60, "keya")
    ud, rd = metrics(60, "keyd")
    uc, rc = metrics(60, "keyc")
    if not (ua > ud > uc and ra < rd < rc):
        raise AssertionError("次重音档 d 不介于 a 与 c 之间：高次 %.1f/%.1f/%.1fdB，"
                             "响 %.4f/%.4f/%.4f" % (ua, ud, uc, ra, rd, rc))
    return "%d 个真槽位 %s；60 号 key 高次 %.1f -> d %.1f -> c %.1fdB，响量 +0/+%.0f/+%.0f%%" % (
        len(real), " ".join(rows), ua, ud, uc, (rd / ra - 1) * 100, (rc / ra - 1) * 100)


@case("fast 质量与 full 听感一致（运行时兜底路径）")
def t_fast():
    """
    不能逐样本比：fast 只跑 2 个梳状、去掉全通，混响的微观样本位置本来就变了，而
    “第三个反射落在哪个样本”不是听感量（实测这样比会把 5% 的湿声报成 26% 的偏差）；
    而且采样库路径与兜底路径不会同时出声，没有任何东西需要跟它对齐。
    比的是听得上去的量：响量、起音、尾音、onset 亮度。
    """
    rows = []
    M12 = int(0.012 * SR)
    for midi, timbre in ((60, "keya"), (45, "warmc"), (81, "ticka")):
        _, fs = render(PACK, midi, timbre, 1, "full")
        _, st = render(PACK, midi, timbre, 1, "fast")
        m = min(int(WIN_MS / 1000.0 * SR), len(fs), len(st))

        def lvl(s):
            return math.sqrt(sum(x * x for x in s[:m]) / m) or 1e-9

        db = 20.0 * math.log10(lvl(st) / lvl(fs))
        atk = abs(attack_ms(st)[0] - attack_ms(fs)[0])
        tail = abs(tail_ms(st)[0] - tail_ms(fs)[0])
        f0 = V.midi_to_freq(midi)
        br = abs(air_db(st, f0, M12) - air_db(fs, f0, M12))
        if abs(db) > 1.5:
            raise AssertionError("midi=%d %s fast 响量差 %.2fdB，会与采样库明显不搭"
                                 % (midi, timbre, db))
        if atk > 4.0:
            raise AssertionError("midi=%d %s fast 起音差 %.1fms" % (midi, timbre, atk))
        if tail > 25.0:
            raise AssertionError("midi=%d %s fast 尾音差 %.1fms" % (midi, timbre, tail))
        if br > 3.0:
            raise AssertionError("midi=%d %s fast 亮度差 %.1fdB，弱模态不该影响这么多"
                                 % (midi, timbre, br))
        rows.append("%d%s：%+.2fdB/%.0fms起/%.0fms尾/%.1fdB亮" % (
            midi, timbre, db, atk, tail, br))
    return " ".join(rows)


@case("采样规格：44100Hz / 16bit 单声道 PCM")
def t_format():
    pcm, n = PACK.render(60, "keya", 0, "full")
    if SR != 44100:
        raise AssertionError("采样率不是 44.1k")
    if len(pcm) != 2 * n:
        raise AssertionError("字节数与帧数不符：%d vs %d" % (len(pcm), 2 * n))
    if n / SR < 0.10:
        raise AssertionError("采样太短（%.0fms），尾音会被砍掉" % (n / SR * 1000))
    return "%d 帧 / %.0fms" % (n, n / SR * 1000)


# 预设与配置项两项用的槽位子集：覆盖四家族 x 常规/重音档 x 低中高。这两项各自要建
# 3~6 个包并整批渲染，拿全库口径跑会把验收拖到分钟级。
PSLOTS = [(60, "keya"), (48, "keyc"), (76, "tickb"), (72, "softa"),
          (45, "warmc"), (81, "ticka"), (64, "softd"), (52, "keyb")]
DECIMATED = KEYMAP[::4]          # 分区均值用：间隔取点，仍盖住每条曲线的插值段


def wet_ratio(pack, midi, timbre):
    """
    湿声占混合后总能量的比：与 t_reverb 同一个口径（需求写的是"占比"，不是发送量）。
    分母用含混响那一版而不是干声：干声做分母量到的是发送量，它比占比系统性高 1.11 倍
    （混响网络自身的能量增益），拿它卡"≤10%"会把一个合规的设定值报成越界。
    """
    dry, wetp = pack._neutral(reverb_wet=0.0), pack._neutral()
    s0 = render(dry, midi, timbre, 0)[1]
    s1 = render(wetp, midi, timbre, 0)[1]
    m = min(int(WIN_MS / 1000.0 * SR), len(s0), len(s1))
    e_w = sum((s1[i] - s0[i]) ** 2 for i in range(m))
    e_d = sum(x * x for x in s1[:m]) or 1e-12
    return e_w / e_d


def tick_mean(pack):
    """tick 家族干声响量均比（分区比的【设计量】，见 zone_ratios 与 t_all_packs）。"""
    rows = zone_ratios(pack, DECIMATED).get("tick") or []
    return sum(d for _m, d, _w in rows) / len(rows) if rows else 0.0


@case("每个音色包预设都守住硬指标（需求 §六.2 的接口不是摆设）")
def t_all_packs():
    """
    其余验收只测默认包（发行配置）。这一项把 VOICE_PACKS 的每个预设都过一遍同样的
    时间/混响/分区指标：mellow 把低通压到 6kHz 并把钢片琴换成木、bright 把主键区换成
    钢片琴 —— 换琴条同时换了 decay 与峰值定标，正是最容易把攻击/尾音/分区比带偏的改动。
    不探这一项的话，“换个包发现更难听”只能靠耳朵回归。

    分区判定故意分开两个量：逐槽卡**干声比**（= zone_target 那个设计量，标定后精确
    落在 0.875），含混响那一列只卡“家族均值离干声均值不远”。原因不是怕数字难看，而是
    那一列量的不是分区增益：同一个 0.875 的干声比，bright 的 warm 在含混响列读到
    0.890~0.922（均值 0.906），acoustic 读到 0.871~0.873 —— 差的全是干声与自己早期反射
    的干涉项（探针实测：发送量固定 2.04% 时，占比随槽位在 1.72%~2.30% 之间摆）。
    拿它当逐槽硬指标，等于让标定去追相位运气；发行包的含混响逐槽卡带由 t_zone_gain 守住。
    """
    rows = []
    for name in V.pack_names():
        pack = V.make_pack(name, slots=KEYMAP, dense=True)
        atk = [attack_ms(render(pack, m, t, 1)[1])[0] for m, t in PSLOTS]
        tails = [tail_ms(render(pack, m, t, 1)[1]) for m, t in PSLOTS]
        wets = [wet_ratio(pack, m, t) for m, t in PSLOTS[:3]]
        zones = zone_ratios(pack, DECIMATED)
        bad = []
        for i, (midi, timbre) in enumerate(PSLOTS):
            if not 10.0 <= atk[i] <= 30.0:
                bad.append("%s %d%s 攻击 %.1fms" % (name, midi, timbre, atk[i]))
            tl, trunc = tails[i]
            if trunc or not 100.0 <= tl <= 300.0:
                bad.append("%s %d%s 尾音 %.0fms%s" % (
                    name, midi, timbre, tl, "（被缓存截断）" if trunc else ""))
        for w in wets:
            if not 0.005 <= w <= 0.10:
                bad.append("%s 湿声 %.1f%%" % (name, w * 100))
        drift = 0.0
        dmins, dmaxs = [], []
        for fam, fam_rows in sorted(zones.items()):
            ds = [d for _m, d, _w in fam_rows]
            ws = [w for _m, _d, w in fam_rows]
            for midi, d in zip([m for m, _t in DECIMATED if _t[:-1] == fam], ds):
                if not 0.85 <= d <= 0.90:
                    bad.append("%s 分区 %s@%d 干声比 %.3f 不在 10~15%%"
                               % (name, fam, midi, d))
            gap = abs(sum(ws) / len(ws) - sum(ds) / len(ds))
            drift = max(drift, gap)
            if gap > 0.05:
                bad.append("%s 分区 %s 含混响均值偏离设计量 %.3f（干涉项不该这么大）"
                           % (name, fam, gap))
            dmins += ds
            dmaxs += ds
        if bad:
            raise AssertionError("; ".join(bad))
        rows.append("%s: 起 %.1f~%.1fms 尾 %.0f~%.0fms 湿 %.1f%% 分区干 %.3f~%.3f"
                    "（湿列偏移 %.3f）" % (
                        name, min(atk), max(atk), min(x[0] for x in tails),
                        max(x[0] for x in tails), max(wets) * 100,
                        min(dmins), max(dmaxs), drift))
    return " | ".join(rows)


@case("配置项改得动波形（需求 §六.3 的混响/尾音/随机化/分区比不是摆设）")
def t_config_knobs():
    """
    配置能读能夹不等于它能作数：pack_from_config 把值送进了 VoicePack，但“送对了”
    要看波形。每一项都拿两个端点值对量，方向与幅度一起卡：只卡“变了”会漏掉接错参数
    名（例如把 tail_ms 当秒用）这种“往反方向大改”的错法。
    """
    notes = []
    # 1) 混响量：下限与配置能填的上面值都要在需求的 ≤10% 内，且湿声明显变大。
    #    上限拿 WET_LIMIT 而不是 0.10：这一项同时在看“旋钮能填的最大值仍守需求”（实测
    #    占比随槽位的干涉项变，所以三个槽都量，最差那个才是需求管住的那一个）。
    hi_pack = V.pack_from_config({"reverb_wet": V.WET_LIMIT})
    shares = [wet_ratio(hi_pack, m, t)
              for m, t in ((60, "keya"), (48, "keyc"), (45, "warmb"))]
    r_lo = wet_ratio(V.pack_from_config({"reverb_wet": 0.02}), 60, "keya")
    if not (0.004 <= r_lo and max(shares) <= 0.10 and min(shares) >= r_lo * 1.8):
        raise AssertionError("reverb_wet 不驱动湿声：0.02 -> %.2f%%，%.2f -> %.1f%%~%.1f%%"
                             % (r_lo * 100, V.WET_LIMIT, min(shares) * 100,
                                max(shares) * 100))
    notes.append("湿声 %.1f%%->%.1f%%（发送量上限 %.2f 仍守住 ≤10%%）" % (
        r_lo * 100, max(shares) * 100, V.WET_LIMIT))
    # 2) 尾音预算：120ms 与 290ms 都必须落在需求的 100~300ms 带内，且真的拉开差距
    t_short = V.pack_from_config({"tail_ms": 120.0})
    t_long = V.pack_from_config({"tail_ms": 290.0})
    m_short = tail_ms(render(t_short, 60, "keya", 1)[1])
    m_long = tail_ms(render(t_long, 60, "keya", 1)[1])
    if m_short[1] or m_long[1]:
        raise AssertionError("tail_ms 改到 %d 后尾音被缓存截断" % (120,))
    if not (100.0 <= m_short[0] <= 300.0 and 100.0 <= m_long[0] <= 300.0
            and m_long[0] - m_short[0] >= 40.0):
        raise AssertionError("tail_ms 不驱动尾音：120 -> %.0fms，290 -> %.0fms"
                             % (m_short[0], m_long[0]))
    notes.append("尾音 %.0f->%.0fms" % (m_short[0], m_long[0]))
    # 3) 随机化范围：把上限收到 1 音分后，实测音高偏移也必须跟着收（而不只改表里的数）
    tight = V.pack_from_config({"humanize_cents": 1.0})
    if max(abs(x) for x in tight.cents) > 1.0 + 1e-9:
        raise AssertionError("humanize_cents=1.0 没缩住音分表")
    dry = tight._neutral(reverb_wet=0.0)
    base0 = V.midi_to_freq(60)
    i0, i1 = int(0.040 * SR), int(0.200 * SR)
    worst = 0.0
    for var in range(tight.variants):
        s = render(dry, 60, "keya", var)[1]
        f = crossing_freq(biquad_filt(s, "lp", base0 * 1.3), i0, min(i1, len(s)))
        if f is None:
            raise AssertionError("过零点太少，无法测音高")
        worst = max(worst, abs(1200.0 * math.log(f / base0, 2.0)))
    if worst > 1.7:                       # 1.0 音分上限 + 估计器那 0.6 音分系统偏差
        raise AssertionError("humanize_cents=1.0 下实测偏移 %.2f 音分，配置没接到合成" % worst)
    notes.append("音分上限 1.0 实测 %.2f" % worst)
    # 4) 分区音量比：把 tick 目标从 0.85 拉到 0.90，实测的设计量（干声比）要跟着上来。
    #    用 dense：启动那份 3 点标定有插值残差（实测 tick 均值比目标低 0.003~0.008，
    #    见 t_zone_gain），那是标定密度的事不是接线的事；这里只问“配置值能不能驱动
    #    到它声称的那个数字”，所以拿逐音高稠密曲线量，两个端点各自贴目标 ±0.005。
    z_lo = V.pack_from_config({"zone_tick_gain": 0.85}, slots=KEYMAP, dense=True)
    z_hi = V.pack_from_config({"zone_tick_gain": 0.90}, slots=KEYMAP, dense=True)
    g_lo, g_hi = tick_mean(z_lo), tick_mean(z_hi)
    if not (abs(g_lo - 0.85) <= 0.005 and abs(g_hi - 0.90) <= 0.005
            and g_hi - g_lo >= 0.03):
        raise AssertionError("zone_tick_gain 不驱动分区响量：0.85 -> %.3f，0.90 -> %.3f"
                             % (g_lo, g_hi))
    notes.append("分区 tick 均值 %.3f->%.3f" % (g_lo, g_hi))
    return "；".join(notes)


def main():
    print("-" * 46)
    if FAILS:
        print("失败项: %s" % ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
