# -*- coding: utf-8 -*-
"""
音色引擎：用模态合成（modal synthesis）渲染"原声打击乐"质感的短音。

为什么不是"基础波形合成"
    方波/锯齿/脉冲的频谱是无限密集的整数倍谐波，听感就是芯片音；而真实琴条（马林巴/
    木琴/钢片琴）发声是一组**非整数倍**的弯曲模态在振动，且每个模态衰减速度差别极大——
    高次模态几十毫秒就没了，基频还在延续。本模块按这个物理结构生成，不含任何基础波形。

一个键的发声链
    1. 模态列    非整数倍频率比 + 每模态独立指数衰减；基频拆成相差 split 的两个振荡器
                 产生缓慢拍颤，消除"稳态纯音"的电子感
    2. 起音瞬态  琴槌撞击的带通噪声：低频闷击 thump（木质琴体感）+ 高频 air（空气感），
                 两者中心频率都随音高落在琴条共振区，不是嘶嘶白噪
    3. 包络      raised-cosine 起音（10~30ms，无突变）+ 结尾 6ms 淡出，两处都是去 click
    4. 音头滑音  击打瞬间基频有约 4ms、数音分的上跳，真实琴条受冲击的实测特征
    5. 频谱整形  二阶 Butterworth 低通压掉刺耳顶部谐振
    6. 房间混响  Schroeder 结构（4 并联梳状 + 2 串联全通），湿声默认 5%、RT60 110ms
                 干声衰减时间按“总尾音预算”反推：dry = tail_cap - 0.7*RT60，
                 因此 −60dB 落点始终守在 tail_cap（需求 ≤300ms）以内
    7. 输出      DC 阻断 -> 峰值归一（不可能削波）-> TPDF 抖动 -> 16bit 量化

两个正交的"变体"维度
    timbre 尾字符 a/b/c = 敲击方式（家族名不变，因此"音区=家族""相邻不同音色"两条既有
    规则不被破坏）：a 常规、b 稍硬（高次更多）、c 节拍重音（大按键，闷击更足、高次更少）。
    humanize 维另算：同一槽位再渲染 K 个细微差异版本（音分与响量在限额内偏移、噪声种子
    不同），按键时随机取一个，用来打破机械重复。
"""
import array
import math
import random
import zlib

SAMPLE_RATE = 44100
NYQUIST_SAFE = SAMPLE_RATE * 0.45          # 高于此的模态无法表达，舍弃而不留别名
LN1000 = 6.907755278982137                 # -60dB 的自然对数：衰减常数一律以此标定
# 波形格式版本：合成链（模态/瞬态/包络/混响/输出）的任何实现细节改动都要 +1，
# 它进参数指纹，因此旧采样库不会在新代码下被静默复用。
RENDER_VERSION = 1

# ------------------------------------------------------------------ 琴条模态模型
# ratios / amps / decayfac 三个元组一一对应。amps 相对基频，decayfac 相对总衰减时间。
# 数值取自真实琴条的频谱特征：基频绝对主导，二次模态约 -16dB，更高次迅速消失。
# thump / air 的第一个数是**这一击的峰值幅度**（基频记作 1.0），见 _add_burst；
# 两者的中心频率区间分别对应"琴体闷击"（150~880Hz）与"空气感"（3.6~8kHz）。
# decayfac 有硬下限：每个模态的寿命 decay*decayfac 必须比 attack 长。之前写成
# 0.045（≈10ms）时，28ms 的 raised-cosine 起音窗直接把最高两个模态整个吞掉，
# 实测泛音列塌成单频（验收项“泛音数量 ≥3”直接挂）——这就是需求里“避免单薄的
# 单频感”和“Attack 10~30ms”两个约束的真实冲突，靠拉长高次模态寿命 + 分元起音解决。
BARS = {
    # 玫瑰木马林巴：温暖饱满 —— 主打字区
    "marimba": {
        "ratios": (1.0, 3.92, 9.36, 17.2, 28.5),
        "amps": (1.0, 0.158, 0.062, 0.028, 0.010),
        "decayfac": (1.00, 0.55, 0.38, 0.26, 0.16),
        "attack": 0.028, "decay": 0.30,
        "thump": (0.30, 2.2, 150.0, 880.0, 0.013, 0.9),
        "air": (0.13, 16.0, 4200.0, 7800.0, 0.008, 1.6),
        "split": 0.0018, "bend": 0.0040,
    },
    # 硬木木块弱奏：更暗更短 —— 功能键/状态键（不抢主音）
    "wood": {
        "ratios": (1.0, 3.05, 6.9, 12.4),
        "amps": (1.0, 0.19, 0.062, 0.034),
        "decayfac": (1.00, 0.45, 0.38, 0.32),
        "attack": 0.022, "decay": 0.17,
        "thump": (0.34, 1.8, 140.0, 760.0, 0.014, 0.8),
        "air": (0.055, 14.0, 3600.0, 6600.0, 0.006, 1.2),
        "split": 0.0014, "bend": 0.0030,
    },
    # 钢片琴极弱奏：金属余韵略长但仍收敛 —— 编辑键/方向键
    "glock": {
        "ratios": (1.0, 2.76, 5.40, 8.93, 13.3),
        "amps": (1.0, 0.120, 0.062, 0.032, 0.014),
        "decayfac": (1.00, 0.60, 0.46, 0.34, 0.22),
        "attack": 0.030, "decay": 0.22,
        "thump": (0.18, 2.0, 160.0, 820.0, 0.010, 0.7),
        "air": (0.17, 12.0, 5000.0, 7800.0, 0.010, 1.8),
        "split": 0.0022, "bend": 0.0026,
    },
    # 低音马林巴/共鸣木：基频更厚、闷击更明显 —— 大按键/低音区
    "lowbar": {
        "ratios": (1.0, 3.86, 9.1, 16.4, 26.0),
        "amps": (1.0, 0.132, 0.048, 0.020, 0.008),
        "decayfac": (1.00, 0.55, 0.40, 0.30, 0.18),
        "attack": 0.026, "decay": 0.30,
        "thump": (0.40, 1.6, 110.0, 620.0, 0.018, 0.6),
        "air": (0.07, 12.0, 3800.0, 6800.0, 0.006, 1.1),
        "split": 0.0016, "bend": 0.0050,
    },
}

# 分区家族 -> (琴条模型, 家族音量比例)。家族名沿用 notes.ZONE_FAMILY 的既有约定。
# 这里的增益只在"直接 new VoicePack() 不调 calibrate()"的手工路径下生效：发行路径
# 一律走 make_pack()，分区响量由 calibrate() 的"家族 x 音高"补偿曲线接管（原因见它的
# 注释：单标量增益管不了响量比随音高漂移）。下面三个数字是当年在 midi60 单点上
# 手标的结果，留着当量级参考。
# 需求：非主打字区比主键区低 10~15%，实测落在 0.86~0.89。
FAMILY_VOICE = {
    "key": ("marimba", 1.000),     # 主打字区：基准
    "tick": ("wood", 0.921),       # 功能/状态键：裸响量 .1517 -> 补到 0.875
    "soft": ("glock", 0.842),      # 编辑/方向键：裸响量 .1658（钢片琴最响）
    "warm": ("lowbar", 0.865),     # 小键盘：裸响量 .1614
}
# 分区响量目标（相对主打字区）：需求 §三 要求非主键区低 10~15%，取中值 12.5%。
# 这是"设计意图"而不是"标定结果"：标定把实测比拉到这些数字上。
ZONE_TARGET = {"key": 1.000, "tick": 0.875, "soft": 0.875, "warm": 0.875}
# 分区响量的比值基准家族（需求 §三 把"主打字区音量"定为基准值，其余都是相对它）
_BASE_FAM = "key"
# 标定的默认参考音高。没拿到实际映射时的兼容选择，只保证分区响量的**平均值**到位；
# 逐半音实测发现裸响量比是逐半音振荡的，不是平滑曲线（midi52 到 53 就能从 0.964
# 跳到 0.946）：响量是 0.66 乘以 RMS/peak，而峰与 RMS 的比值随模态频率对采样栅格
# 的位置非单调变化。所以少量参考点无法把每个键都钉进 0.85~0.90：运行时用
# refs_for(slots, 3) 拿各自家族的真实范围，离线建库用 refs_for(slots) 把每个音高
# 都标上，并把得到的曲线存进 manifest 供运行时恢复（见 voicebank.restore_calibration）。
CALIB_REFS = (45, 63, 81)
# 响量统计窗（秒）：只算可听部分，不被缓存长度稀释。与验收脚本的 WIN_MS 同口径。
LOUD_WIN_SEC = 0.250
# 敲击方式四档：(高次模态增益, 闷击增益, 衰减时间倍率, 音分微调, 响量倍率)
# a/b 是同一音区内的棋盘格细微变体（相邻键可区分）；c/d 只给大按键（需求 §三 的
# "节拍重音"层），两档是因为主键区里存在相邻的大按键（Enter 与 RShift）——同一家族
# 只用一档就会让相邻两键的音色标识完全相同，破掉"相邻不重复"。
STRIKE = {
    "a": (1.00, 1.00, 1.00, 0.0, 1.00),
    "b": (1.55, 0.86, 0.95, 1.0, 1.00),    # 稍硬：高次更多、闷击更少
    "c": (0.58, 1.65, 1.20, -1.2, 1.15),   # 重音：闷击更足、高次更少、+1.2dB
    "d": (0.72, 1.40, 1.12, -0.6, 1.08),   # 次重音：同方向但收着，给相邻的大按键错开
}
# 重音档（大按键专用）：除响量/闷击之外，还把琴条换成共鸣更厚的低音模型
ACCENT = ("c", "d")
ACCENT_BAR = "lowbar"
CENTS = (-4.1, 0.0, 2.7, -1.6, 4.3, 1.1)          # humanize 音分偏移表
LOUDNESS_JITTER = (-0.55, 0.0, 0.35, -0.25, 0.5, 0.15)   # dB，需求上限 ±1dB
CENT_LIMIT = 5.0        # 需求：音高随机偏移 ≤±5 音分（叠加敲击方式后仍要守住）
DB_LIMIT = 1.0         # 需求：音量随机偏移 ≤±1dB
# 需求：湿声占比 ≤10%。这里的上限是 reverb_wet 这个【发送量】的允许值，不等于 10%：
# 实测混响网络把发送量放大 1.11 倍（Σ湿²/Σ干² = 1.11 x wet，与音高/音色无关），而验
# 收量的是湿声占混合后总能量的比例，还要再叠一个干声与自己早期反射的干涉项——同一设置
# 在 8 个槽位上量到 1.7%~2.3%（发送量固定 2.04%），最坏槽位是发送量的 1.39 倍。于是
# wet 填到 0.10 会量出 13.9%（midi48 keyc），即“配置能把产品调成违反需求”。按最坏槽位
# 反推 ≤10% 对应的发送量上限就是 0.07（实测占比 6.1%~9.4%，留一丝余量）。
WET_LIMIT = 0.07


def midi_to_freq(midi):
    """MIDI 音高 -> 基频（A4=440）。"""
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


# ------------------------------------------------------------------ 工具


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class VoicePack(object):
    """
    一套可整体替换的音色参数。config 的 voice_pack / reverb / humanize / zone gain
    都落在这里；audio 只认 pack.render()，换音色包不需要动播放层。
    """

    def __init__(self, family_voice=None, strike=None, cents=None, loudness_db=None,
                 reverb_wet=0.05, reverb_rt=0.11, pre_delay=0.008,
                 tilt_hz=8000.0, variants=6, tail_cap=0.29,
                 seed=20261006, name="acoustic", zone_target=None,
                 accent_bar=ACCENT_BAR):
        self.family_voice = dict(family_voice or FAMILY_VOICE)
        self.strike = dict(strike or STRIKE)
        self.cents = tuple(cents if cents is not None else CENTS)
        self.loudness_db = tuple(loudness_db if loudness_db is not None
                                 else LOUDNESS_JITTER)
        self.reverb_wet = _clamp(reverb_wet, 0.0, WET_LIMIT)  # 需求：湿声占比 ≤10%
        self.reverb_rt = _clamp(reverb_rt, 0.02, 0.20)      # 需求：RT60 ≤200ms
        self.pre_delay = _clamp(pre_delay, 0.0, 0.05)
        self.tilt_hz = _clamp(tilt_hz, 2000.0, 18000.0)
        self.variants = max(1, int(variants))
        # 需求：自然尾音 100~300ms。这里守的是“含混响的总尾音”（−60dB 落点），
        # 不是干声长度 —— 只卡干声会被混响环再拖长 60~100ms。
        self.tail_cap = _clamp(tail_cap, 0.10, 0.30)
        self.seed = int(seed)
        self.name = name
        self.zone_target = dict(zone_target or ZONE_TARGET)
        # 重音档用的琴条：名字不在 BARS 里就退回家族自己的琴条（_plan 里查表）
        self.accent_bar = accent_bar or ACCENT_BAR
        self.family_trim = {}          # 家族 -> ((参考音高, 响量补偿), ...)，calibrate() 填
        self._calibrated = False

    # ------------------------------------------------------------ 参数指纹
    def fingerprint(self):
        """
        渲染参数的唯一标识：音色/映射/混响任一参数变化都必须重建采样库。

        RENDER_VERSION 在内：合成代码本身改了（不是参数改了）也会产出不同的波形，
        而那一类变化没法从参数表看出来，不升版本号就会误用旧采样库。

        不含 family_trim（分区响量补偿曲线）：它是这些参数的**导出量**，但导出时用的
        参考音高集合可以不同（启动廉价 3 点 / 离线逐半音稠密）。采样库目录按本函数命名，
        voicebank.open() 会先把库里存的曲线恢复到本包上再用它渲染回退，两条路径因此
        不会互相认不出来。BARS 也不在 payload 里：它只按 family_voice/accent_bar 里的
        琴条名字被查表，改动它同时需要 +RENDER_VERSION。

        zone_target 必须在内：它是 calibrate() 的目标值，直接决定每条采样的响量。用户
        在配置里把分区比从 0.875 改成 0.90 时，若不进指纹就会拿着旧分区比的库继续出声，
        而且无论怎么排查参数都看不出来（需求 §六.3 要求这一项可调，所以真会发生）。
        """
        payload = (RENDER_VERSION,
                   sorted(self.family_voice.items()),
                   sorted(self.strike.items()),
                   tuple(sorted((f, round(v, 6))
                                for f, v in self.zone_target.items())),
                   self.cents, self.loudness_db, round(self.reverb_wet, 6),
                   round(self.reverb_rt, 6), round(self.pre_delay, 6),
                   round(self.tilt_hz, 3), self.variants, round(self.tail_cap, 6),
                   self.accent_bar, self.seed, self.name)
        return "%08x" % (zlib.crc32(repr(payload).encode("utf-8")) & 0xFFFFFFFF)

    def key_for(self, midi, timbre, variant):
        """采样库里一条采样的稳定标识（不用内置 hash：它每进程随机化）。"""
        fam = timbre[:-1]
        return "%d:%s:%s:%d" % (midi, fam, timbre[-1:], variant % self.variants)

    # ------------------------------------------------------------ 尺寸/参数推导
    def _plan(self, timbre):
        """
        (琴条, 干声衰减时间, 缓存帧数) —— render 与 cache_frames 共用同一条路径。

        为什么不靠"渲染一个然后乘个数"估体积：帧数由琴条的 decay 与档位决定，各家族
        差一倍以上，抽几个槽位外推整库会差出几十%（实测拿 8 个主键区槽位外推会得到
        63MB，真值是 11MB）。尺寸与波形分开算才能不渲染就拿到准确数字。

        重音档（大按键）在这里就把琴条换成低音模型：需求 §三要的不只是"响一点"而是
        "偏低沉的同风格打击音色"，只改闷击/高次是滤波级的差别，换发声体才是。
        """
        voice, _g = self.family_voice.get(timbre[:-1], self.family_voice["key"])
        letter = timbre[-1:]
        if letter in ACCENT and self.accent_bar in BARS:
            voice = self.accent_bar
        bar = BARS.get(voice, BARS["marimba"])
        decay_k = self.strike.get(letter, self.strike["a"])[2]
        # 总尾音预算里先扣掉混响的延伸量，剩下的才是给干声的衰减时间
        decay = _clamp(bar["decay"] * decay_k, 0.05,
                       max(0.05, self.tail_cap - 0.7 * self.reverb_rt))
        # 缓存长度：实测 −60dB 落点大约在干声 tau 再过去几毫秒（混响是从干声
        # 派生的，跟着干声一起衰减，不会自己再拖出一个 RT60），所以
        # 0.55*RT60 + 余量足够把可听部分完整包进去，不留上百毫秒的纯静音。
        return bar, decay, int((decay + 0.55 * self.reverb_rt + self.pre_delay
                               + 0.012) * SAMPLE_RATE) + 32

    def cache_frames(self, timbre):
        """这条采样的帧数（不渲染就能问）：建库前算总体积、验收算内存预算都靠它。"""
        return self._plan(timbre)[2]

    # ------------------------------------------------------------ 对外渲染
    def render(self, midi, timbre, variant=0, quality="full"):
        """
        渲染 (音高, 音色, humanize变体) -> (int16 小端 PCM bytes, 帧数)。
        quality="full" 为完整精度（离线建库）；"fast" 舍弃 -30dB 以下的弱模态、
        混响减为 2 个梳状并去掉全通级（运行时补染兜底用，听感差异极小）。
        """
        fam = timbre[:-1]
        letter = timbre[-1:]
        _bar_name, fam_gain = self.family_voice.get(fam, self.family_voice["key"])
        fam_gain = fam_gain * self._trim(fam, midi)
        bar, decay, n = self._plan(timbre)      # 琴条/长度/衰减的算法见 _plan
        uppers, thump_k, _decay_k, strike_cents, strike_gain = self.strike.get(
            letter, self.strike["a"])

        v = variant % self.variants
        # 音分随机化叠加敲击方式偏移后，仍要守住需求的 ±5 音分上限
        cents = _clamp(self.cents[v % len(self.cents)] + strike_cents,
                       -CENT_LIMIT, CENT_LIMIT)
        db = _clamp(self.loudness_db[v % len(self.loudness_db)], -DB_LIMIT, DB_LIMIT)
        base = midi_to_freq(midi) * (2.0 ** (cents / 1200.0))

        # 确定性 RNG：同一 (键,变体) 每次构建产出同一波形，采样库指纹校验才有意义
        stem = (self.seed * 1000003) ^ (int(midi) * 8191) ^ \
            (zlib.crc32(timbre.encode("utf-8")) & 0xFFFFFFFF)
        rng = random.Random((stem ^ (v * 2654435761)) & 0x7FFFFFFF)
        # 模态失谐属于"琴条"而不是"这一击"：同一根琴条每次被敲，弯曲模态频率都是
        # 同一个数（物理如此），所以它的种子不含变体号。之前混在一起会让 250ms 窗
        # 响量随种子漂 ±3%（实测）：需求给分区比的窗口只有 ±2.9%，标定量和验收量都
        # 不稳。分开后只有琴槌噪声的 realization 随变体变（实测 <1.5%）。
        det = random.Random(stem & 0x7FFFFFFF)

        out = [0.0] * n
        fast = (quality != "full")
        self._add_modes(out, n, base, bar, uppers, decay, fast, det)
        self._add_burst(out, n, bar["thump"], thump_k, base, rng)
        self._add_burst(out, n, bar["air"], 1.0, base, rng)
        self._release(out, n)
        self._lowpass(out, n)
        self._dc_block(out, n)
        # 峰值定标取在混响之前：混响是从干声派生的尾巴，它不该反过来把干声压小。
        # 先算 peak 再叠加混响，(a) 击键响量由琴条本体决定（分区响量比才是设计量），
        # (b) 关掉混响的渲染与开启时干声部分逐样本相同，湿声占比可以直接量出来。
        peak = self._peak(out, n)
        # 先把干声峰值归一到 0.66（这本身不可能削波），再乘分区/重音/抖动增益。
        # 上限 1.25 是留给“响量抖动 +0.55dB × 重音 +1.2dB”的余量，因此干声峰值最高
        # 0.80，给混响留下到 0.88 的空间（混响不能反过来把总峰值顶出安全区）。
        # 不能把 scale 笼统 clamp 到 1.0：那会把归一化本身卡掉，实测峰值会随琴条
        # 不同而在 0.57~0.65 之间乱跳，分区响量比就变成了意外量而不是设计量。
        target = 0.66 * fam_gain * strike_gain * 10.0 ** (db / 20.0)
        scale = _clamp(target / peak if peak > 1e-9 else 0.0, 0.0, 1.25)
        if self.reverb_wet > 0.0:
            # 0.88 是叠加混响后允许的原始峰值上限（需求：不削波），换算回未定标域。
            self._reverb(out, n, fast, peak, 0.88 / scale if scale > 1e-9 else 0.0)
        return self._quantize(out, n, scale), n

    # ------------------------------------------------------------ 分区响量标定
    def calibrate(self, refs=CALIB_REFS, passes=2):
        """
        按实测响量反推"分区 x 音高"的响量补偿，让"非主键区低 10~15%"在整张键盘上成立。

        为什么不能只有一个标量增益：峰值归一之后响量取决于波形形状（波峰因数），
        而四根琴条的形状各不相同，并且随音高以不同的速率变化 —— 实测 tick/wood 在
        midi48 比主键区低 11%，到 midi76 就低 23%（因为高音下高次模态被 Nyquist/tilt
        舍弃掉的比例不一样）。而 tick 家族实际覆盖 midi50~81（数字键与方向键共用一族），
        拿 midi60 一个点标定出来的数字恰好把这个问题藏起来：真正出声时功能区比
        主键区轻了两成多，需求 §三 的"10~15%"直接落空。所以补偿必须是一条随音高插值的曲线。

        为什么必须实测而不能只靠手写表：改 BARS / 琴槌电平 / 分元起音 / tilt 任何一项
        都会让旧数字失配，而失配恰好落在需求禁止的那条线上（功能区抢主音区）。
        换音色包（§六.2）本来就需要重新手标一次，现在它自动做。

        两轮：第一轮用 fast 干声探针量"总倍率"曲线；第二轮用同样的干声但 full 精度在
        每个参考音高上量残差再乘回去，修正峰值定标 clamp（scale ≤ 1.25）带来的非线性。
        两轮都量干声：混响是按能量比发送的，每个音同一份湿/干比，它不可能也不应该
        改变设计上的分区比（把它算进去会掺进一个与基频-延迟相位有关的交叉项，实测
        让分区比多摆动几个百分点）。代价：每家族 3 点约 0.45s，逐半音稠密标定约 1.3s
        （只在建库时跑，结果存进 manifest 供运行时恢复；两个数都是单条计时的冷启动口径，
        持续满载后机器降频会整体翻倍）。

        refs 可以是音高序列（全家族共用），也可以是 {家族: 音高序列}。后者是正解：
        四个家族实际用的音高段各不相同（key 45~81、tick 50~81、soft 64~76、warm 45~62，
        由 notes.all_slots() 统计），拿一个全局的 (45,63,81) 去标定 soft 等于在问它
        根本不会发的那些音高上响多少。用 refs_for(slots, 3) 就能在同样的代价下
        拿到各自家族的真实范围（实测：出带键数从 5 个降到 2 个，家族均值全部回到 0.875）。
        """
        refs_map = self._refs_map(refs)
        probe = self._neutral(reverb_wet=0.0, neutral_voice=True)
        # 只量需要的 (家族, 音高) 对。主键区在每个参考音高上都要量：它是比值分母。
        need = set()
        for fam, pts in refs_map.items():
            for midi in pts:
                need.add((fam, midi))
                need.add((_BASE_FAM, midi))
        rms = {}
        for fam, midi in sorted(need):
            if fam not in probe.family_voice:
                continue
            pcm, n = probe.render(midi, fam + "a", 0, "fast")
            rms[(fam, midi)] = _rms(pcm, n)
        curve = {}
        for fam, pts in refs_map.items():
            want = self.zone_target.get(fam, 1.0)
            row = []
            for midi in pts:
                r = rms.get((fam, midi), 0.0)
                base = rms.get((_BASE_FAM, midi), 0.0)
                # 主键区自己：r == base，算出来恒为 1.0（它本就没有"比主键区低"这件事）
                row.append((midi, want * base / r
                            if (r > 1e-12 and base > 1e-12) else 1.0))
            curve[fam] = tuple(row)
        self.family_voice = dict((fam, (bar, 1.0))
                                 for fam, (bar, _g) in self.family_voice.items())
        self.family_trim = curve

        for _ in range(max(0, int(passes) - 1)):
            meas = self._zone_ratios(refs_map, wet=False)
            trim = {}
            for fam, pts in self.family_trim.items():
                want = self.zone_target.get(fam, 1.0)
                fixed = []
                for (midi, v) in pts:
                    got = meas.get(fam, {}).get(midi, want)
                    fixed.append((midi, v * (want / got) if got > 1e-9 else v))
                trim[fam] = tuple(fixed)
            self.family_trim = trim
        self._calibrated = True
        return self.family_trim

    def _refs_map(self, refs):
        """refs -> {家族: (参考音高, ...)}。序列 = 全家族共用；某家族缺失则回共用默认点。"""
        fams = list(self.family_voice)
        if isinstance(refs, dict):
            out = {}
            for fam in fams:
                pts = refs.get(fam)
                out[fam] = tuple(sorted(set(int(m) for m in pts))) if pts else tuple(CALIB_REFS)
            return out
        seq = tuple(refs or CALIB_REFS)
        return dict((fam, seq) for fam in fams)

    def _neutral(self, reverb_wet=None, neutral_voice=False):
        """关掉 humanize 抖动的探针副本（量响量时 ±0.55dB 的抖动就是噪声）。"""
        fam = dict(((f, (b, 1.0)) for f, (b, _g) in self.family_voice.items())
                   if neutral_voice else self.family_voice)
        p = VoicePack(
            family_voice=fam, strike=self.strike, cents=(0.0,), loudness_db=(0.0,),
            reverb_wet=self.reverb_wet if reverb_wet is None else reverb_wet,
            reverb_rt=self.reverb_rt, pre_delay=self.pre_delay, tilt_hz=self.tilt_hz,
            variants=1, tail_cap=self.tail_cap, seed=self.seed, name=self.name,
            zone_target=self.zone_target)
        # 探针要么从零补偿开始（第一轮量的是"总倍率"），要么带着当前曲线（第二轮量残差）。
        # 这里写错一次就会把已有曲线当成裸响量再乘一遍，重复调用 calibrate() 时跑偏。
        p.family_trim = {} if neutral_voice else self.family_trim
        p._calibrated = True
        return p

    def _zone_ratios(self, refs, quality="full", wet=None):
        """
        -> {家族: {音高: 实测响量/主键区}}。refs 收序列也收 {家族: 序列}（见 _refs_map）。

        wet=False 时量干声：混响是按能量比发送的（每个音同一个湿/干比），它本来
        就不该改变设计上的分区比；把它算进度量只会掺进一个 2·Σ干·湿 的交叉项，
        其符号与大小取决于基频与梳状延迟的相位关系，实测能让分区比在标定点上
        摆动 ±5%（比需求给的 10~15% 窗口还宽）。量测音高时也是同样的道理（见
        tests/metrics.py 的 crossing_freq）。
        """
        refs_map = self._refs_map(refs)
        probe = self._neutral(reverb_wet=0.0) if wet is False else self._neutral()
        need = set()
        for fam, pts in refs_map.items():
            for midi in pts:
                need.add((fam, midi))
                need.add((_BASE_FAM, midi))      # 比值分母：同音高的主键区
        rms = {}
        for fam, midi in sorted(need):
            if fam not in probe.family_voice:
                continue
            pcm, n = probe.render(midi, fam + "a", 0, quality)
            rms[(fam, midi)] = _rms(pcm, n)
        out = {}
        for fam, pts in refs_map.items():
            row = {}
            for midi in pts:
                base = rms.get((_BASE_FAM, midi), 0.0)
                r = rms.get((fam, midi), 0.0)
                row[midi] = (r / base) if base > 1e-12 else 1.0
            out[fam] = row
        return out

    def _trim(self, fam, midi):
        """音高 -> 响量补偿（本包当前曲线，见 trim_at）。"""
        return trim_at(self.family_trim, fam, midi)

    # ------------------------------------------------------------ 1/2：模态与瞬态
    def _add_modes(self, out, n, base, bar, uppers, decay, fast, det):
        """非谐模态列，每模态独立指数衰减，各自带自己的起音斜坡。"""
        atk_f = bar["attack"]
        # 高次模态只走 0.3 倍时长的起音。理由不是偷懒：真实琴条被击中的瞬间，高次
        # 模态是被冲击直接激励的（接触时间 1~3ms 量级），只有基频因为要通过空气辐射
        # 才显得"圆"。把 28ms 的柔和斜坡平摊到所有模态，会把 -30dB 附近的泛音整段
        # 抹掉，实测泛音列塌成单频 —— 正是需求禁止的"单薄的单频感"。
        atk_u = max(0.0025, atk_f * 0.30)
        for i, (ratio, amp, dfac) in enumerate(
                zip(bar["ratios"], bar["amps"], bar["decayfac"])):
            f = base * ratio
            if f >= NYQUIST_SAFE:
                break
            a = amp * (uppers if i else 1.0)
            if a <= 1e-6 or (fast and a < 0.03):
                continue                    # 低于 -30dB 的弱模态：fast 舍弃
            tau = max(0.006, decay * dfac)
            if i == 0:
                sp = bar["split"]
                # 基频分裂成两个微失谐振荡器 -> 缓慢拍颤，去掉"稳态纯音"感
                self._osc(out, n, f * (1.0 - sp / 2.0), tau, a * 0.71,
                          bar["bend"], atk_f)
                self._osc(out, n, f * (1.0 + sp / 2.0), tau * 0.92, a * 0.71,
                          bar["bend"], atk_f)
            else:
                self._osc(out, n, f * (1.0 + det.uniform(-0.004, 0.004)),
                          tau, a, 0.0, atk_u)

    def _osc(self, out, n, freq, tau, amp, bend, atk):
        """
        一个模态。衰减常数按 -60dB 落在 tau 秒处标定：k = exp(ln(0.001)/(tau*SR))。
        bend>0 时前 ~4ms 频率上跳数音分（击打瞬间的真实音头行为）。
        atk 是本模态自己的 raised-cosine 起音长度（斜率连续，所以不会 click）。
        这里用精确 math.sin 而不是带限查表：纯 Python 里查表每次要多一次取整/
        取模/两次索引，实测反而比 C 实现的 sin 慢 20%。
        """
        k = math.exp(-LN1000 / max(1.0, tau * SAMPLE_RATE))
        v = amp
        w = 2.0 * math.pi * freq / SAMPLE_RATE
        TWO_PI = 2.0 * math.pi
        ph = 0.0
        al = min(n, max(2, int(atk * SAMPLE_RATE)))
        bd = min(n, max(1, int(0.004 * SAMPLE_RATE))) if bend > 0.0 else 0
        bstep = (2.0 * math.pi * freq * bend / SAMPLE_RATE) if bd else 0.0
        bdec = (bd * 0.35) if bd else 1.0
        head = al if al >= bd else bd
        for i in range(head):                # 头部：起音斜坡（+ 音头滑音）
            ph += w + (bstep * math.exp(-i / bdec) if i < bd else 0.0)
            if ph >= TWO_PI:
                ph -= TWO_PI
            g = 0.5 - 0.5 * math.cos(math.pi * i / al) if i < al else 1.0
            out[i] += v * g * math.sin(ph)
            v *= k
        for i in range(head, n):             # 主体：恒定频率，纯指数尾
            ph += w
            if ph >= TWO_PI:
                ph -= TWO_PI
            out[i] += v * math.sin(ph)
            v *= k

    def _add_burst(self, out, n, spec, gain, base, rng):
        """
        撞击瞬态：白噪 -> Chamberlin 状态变量带通 -> 指数衰减。
        中心频率随基频浮动并被夹在给定区间，因此是"木质/金属的一击"而不是嘶嘶白噪。

        spec[0] 的含义是**这一击的峰值幅度**（基频记作 1.0），而不是白噪源幅度：
        带通滤白噪的实际输出峰值只有源幅度的 9%~22%（随中心频率与 Q 变），把源幅度
        直接当响量写参数会连续低估 14~20dB —— 改之前实测 thump/air 都在 −43dB，等于
        没有琴槌，需求里的"空气感/低音共鸣/不生硬"全靠这一层在撑。所以先滤、量峰值、
        再回乘到目标幅度，参数与听感就成了一一对应关系。
        """
        amp, mult, lo, hi, dur, q = spec
        amp *= gain
        if amp <= 1e-6:
            return
        fc = _clamp(base * mult, lo, hi)
        if fc >= SAMPLE_RATE * 0.25:
            fc = SAMPLE_RATE * 0.25
        g = math.tan(math.pi * fc / SAMPLE_RATE)
        damp = 1.0 / max(0.3, 2.0 * q)
        m = max(2, min(n, int(dur * SAMPLE_RATE)))
        k = math.exp(-LN1000 / max(1.0, dur * SAMPLE_RATE))
        seg = [0.0] * m
        bp = lb = 0.0
        v = 1.0
        pk = 0.0
        for i in range(m):
            white = rng.uniform(-1.0, 1.0)
            hp = white - lb - damp * bp
            bp += g * hp
            lb += g * bp
            y = bp * v
            seg[i] = y
            av = -y if y < 0.0 else y
            if av > pk:
                pk = av
            v *= k
        if pk <= 1e-9:
            return
        s = amp / pk
        al = max(2, min(m, int(0.0012 * SAMPLE_RATE)))   # 1.2ms：只去硬边，不磨掉冲击
        for i in range(m):
            out[i] += seg[i] * s * (0.5 - 0.5 * math.cos(math.pi * i / al)
                                    if i < al else 1.0)

    # ------------------------------------------------------------ 3：包络
    def _release(self, out, n):
        """末端 6ms 淡出（缓存尾部本来已在 -60dB 下，这里只是保证样本边界不突变）。
        起音斜坡不在这里 —— 它按模态各自的物理时间在 _osc/_add_burst 里做，原因见
        _add_modes 的注释。"""
        rel = min(n, max(1, int(0.006 * SAMPLE_RATE)))
        for i in range(rel):
            out[n - rel + i] *= 1.0 - i / float(rel)

    # ------------------------------------------------------------ 5：频谱整形
    def _lowpass(self, out, n):
        """二阶 Butterworth 低通（RBJ，Q=0.707）：压掉刺耳的顶部谐振，不改变尾音长度。"""
        f = min(self.tilt_hz, SAMPLE_RATE * 0.45)
        w0 = 2.0 * math.pi * f / SAMPLE_RATE
        cw = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * 0.7071067811865476)
        a0 = 1.0 + alpha
        b0 = (1.0 - cw) * 0.5 / a0
        b1 = (1.0 - cw) / a0
        b2 = b0
        a1 = -2.0 * cw / a0
        a2 = (1.0 - alpha) / a0
        x1 = x2 = y1 = y2 = 0.0
        for i in range(n):
            x = out[i]
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2 = x1
            x1 = x
            y2 = y1
            y1 = y
            out[i] = y

    # ------------------------------------------------------------ 6：房间混响
    def _reverb(self, out, n, fast=False, peak=0.0, cap=0.0):
        """
        Schroeder 混响：4 个并联梳状滤波器（各带一阶阻尼，高频先被空气吸收）+
        2 个串联全通做扩散。输入是 out 本身，输出按 wet 叠加回 out，因此
        干声完全不被稀释，击键清晰度不受影响。
        fast 只跑 2 个梳状（去掉全通级），湿声电平靠下面的能量定标自动对齐。

        reverb_wet 的语义是需求里那个"湿声占比"的发送量：混响分量占总能量 ≤10%。这
        个比例靠**量混响网络自身的输出能量再反推增益**得到，而不是靠一个调好的乘数：
        梳状环的增益是 1/(1-g)≈2，乘上 4 个并联再乘 wet 之后，实际占比跟 wet 完全不
        是一个数（曾把 norm=2.35 当成"7% 标定"，实测只有 0.1%~2%，混响约等于不存在）。
        直接按能量比归一还有一个好处：同一房间内"湿/干比"本来就不该随音高变化。
        注意设定值是发送量（Σ湿²/Σ干²），验收量的是湿声占混合后总能量的比例：后者还含
        一个 2·Σ干·湿 的交叉项（可正可负，取决于基频与延迟的相位关系），再加上缓存
        末尾会截掉一段混响尾巴，所以实测占比会在设定值上下浮动而不是严格等于它
        （0.05 -> 约 4%~6%）。上限因此不是 0.10 而是 WET_LIMIT=0.07：按最坏槽位的干涉项
        反推，发送量 0.07 才压得住"占比 ≤10%"这条硬指标（详见 WET_LIMIT 的实测记录）。

        peak/cap 是峰值保护：混响是从干声派生的，它的峰值不允许把总峰值顶出 cap。
        判据用的是**叠加后的真实峰值**而不是 peak+gain*wpk 这种保守上界：实测两者差
        一个数量级（混响最早也要 pre-delay+最短梳状 = 20ms 才出声，跟干声峰值不重叠），
        用上界会把湿声砍到只剩一半，需求里的"≤10%"是上限而不是目标。
        """
        pre = max(1, int(self.pre_delay * SAMPLE_RATE))
        size = n + pre + 1
        # 梳状延迟按毫秒设（不沿用 Schroeder 的 25kHz 样本数：直接搬到 44.1k 会把
        # 62ms 的延迟压缩成 35ms，四个几乎相等的延迟会叠加成“哒-哒”回声而不是房间感）。
        taps = (11.7, 13.7, 15.9, 18.3) if not fast else (11.7, 15.9)
        combs = []
        for ms in taps:
            dd = max(2, int(ms * 0.001 * SAMPLE_RATE))
            g = math.exp(-LN1000 * (dd / SAMPLE_RATE) / self.reverb_rt)
            combs.append((dd, g, 0.62, [0.0] * dd))   # damp: 环内一阶吸收
        wetbuf = [0.0] * size
        for dd, g, damp, buf in combs:
            idx = 0
            lp = 0.0
            for i in range(n):
                y = buf[idx]
                # 环内一阶低通，DC 增益必须为 1：写成 lp = y + damp*lp 会把环路增益
                # 乘上 1/(1-damp)=2.6，g*2.6 > 1 就是一个发散混响（实测湿声占比会到
                # 20% 以上且不随 wet 变）。正确写法：(1-damp)*y + damp*lp。
                lp = y * (1.0 - damp) + lp * damp
                buf[idx] = out[i] + g * lp
                wetbuf[i + pre] += y
                idx += 1
                if idx >= dd:
                    idx = 0
        # 串联全通（Chamberlin）：只推移能量、不改频谱，让混响更均匀不"嗡"
        if not fast:
            for ms, g in ((5.3, 0.5), (1.9, 0.5)):
                dd = max(2, int(ms * 0.001 * SAMPLE_RATE))
                buf = [0.0] * dd
                idx = 0
                for i in range(size):
                    x = wetbuf[i]
                    y = -g * x + buf[idx]
                    buf[idx] = x + g * y
                    wetbuf[i] = y
                    idx += 1
                    if idx >= dd:
                        idx = 0
        edry = 0.0
        ewet = 0.0
        wpk = 0.0
        for i in range(n):
            x = out[i]
            edry += x * x
            v = wetbuf[i + pre]
            ewet += v * v
            av = -v if v < 0.0 else v
            if av > wpk:
                wpk = av
        if edry <= 1e-18 or ewet <= 1e-18:
            return
        rho = _clamp(self.reverb_wet, 0.0, 0.99)
        # 发送量：g^2*ewet = rho/(1-rho) * edry  =>  湿声能量 / 干声能量 = rho/(1-rho)
        gain = math.sqrt(rho / (1.0 - rho) * edry / ewet)
        if cap > peak > 0.0:
            pk = 0.0
            for i in range(n):
                v = out[i] + gain * wetbuf[i + pre]
                av = -v if v < 0.0 else v
                if av > pk:
                    pk = av
            if pk > cap:                      # 只有极个别的重音档会走到这里
                gain *= (cap - peak) / (gain * wpk)
        for i in range(n):
            out[i] += gain * wetbuf[i + pre]

    # ------------------------------------------------------------ 7：输出级
    def _dc_block(self, out, n):
        """约 18Hz 一阶高通：混响与噪声都可能带进直流，长期听会发闷。"""
        r = 1.0 - 2.0 * math.pi * 18.0 / SAMPLE_RATE
        px = py = 0.0
        for i in range(n):
            x = out[i]
            y = r * (py + x - px)
            px = x
            py = y
            out[i] = y

    def _peak(self, out, n):
        pk = 0.0
        for v in out:
            av = -v if v < 0.0 else v
            if av > pk:
                pk = av
        return pk

    def _quantize(self, out, n, scale):
        """
        16bit 量化：TPDF 抖动（±1 LSB）+ 四舍五入。抖动把低位量化噪声打散成与信号
        不相关的细嘶声，否则极轻的尾音段会出现随音量周期出现的"颗粒感"（这是数字
        音频特有的廉价感）。scale 已把峰值归一到 0.80 以下，因此不存在削波。
        注意 out 是 [-1,1] 的浮点，必须先换算到 LSB 幅度再加抖动。
        """
        buf = bytearray(2 * n)
        r = random.Random((self.seed ^ (n * 7919)) & 0x7FFFFFFF)
        full = 32767.0 * scale
        for i in range(n):
            d = r.random() + r.random() - 1.0        # 三角概率密度，单位 = 1 LSB
            v = int(round(out[i] * full + d))
            if v < -32767:
                v = -32767
            elif v > 32767:
                v = 32767
            if v < 0:
                v += 65536
            buf[2 * i] = v & 0xFF
            buf[2 * i + 1] = (v >> 8) & 0xFF
        return bytes(buf)


def refs_for(slots, max_pts=0):
    """
    (midi, timbre) 槽位 -> {家族: 该家族实际用到的音高}，供 calibrate(refs=...)。

    为什么参考点必须由映射表给：四个家族实际用的音高段不重合（soft 只在 64~76，
    warm 只在 45~62），拿一个全局的 (45,63,81) 去标定 soft 等于用两端外推中间，而
    裸响量比恰恰是逐半音振荡的。max_pts>0 时在家族自己的跨度上等距抽样，用于启动
    路径（只付 3 点的代价就能落在真实范围内）；=0 就是逐音高稠密标定，给离线建库。
    """
    got = {}
    for midi, timbre in slots:
        got.setdefault(timbre[:-1], set()).add(int(midi))
    out = {}
    for fam, pitches in got.items():
        pts = sorted(pitches)
        if max_pts and len(pts) > max_pts:
            step = (len(pts) - 1) / float(max_pts - 1) if max_pts > 1 else 0.0
            pts = list(dict.fromkeys(
                pts[int(round(i * step))] for i in range(max_pts)))
        out[fam] = tuple(pts)
    return out


def trim_at(curve, fam, midi):
    """
    在给定曲线上查一个音高的响量补偿：参考点之间线性插值，区间外取端点值，无表 1.0。

    写成接收 curve 的模块函数而不是只用 self.family_trim 的方法，是为了让采样库写侧
    能拿"库里那条"与"本次这条"两个曲线对比同一个键的补偿值（voicebank.build）。
    """
    pts = (curve or {}).get(fam)
    if not pts:
        return 1.0
    if midi <= pts[0][0]:
        return pts[0][1]
    for i in range(1, len(pts)):
        m0, v0 = pts[i - 1]
        m1, v1 = pts[i]
        if midi <= m1:
            return v0 + (v1 - v0) * (midi - m0) / float(m1 - m0)
    return pts[-1][1]


def _rms(pcm, n):
    """
    int16 PCM -> 归一响量（0~1）。两个细节都是必须的：

    * 量的是前 LOUD_WIN_SEC 秒，不是整段缓存。峰值已经被归一到同一个数值，量峰值
      量不出区别；而 RMS 如果含缓存尾部，不同家族的缓存长度不一样（木块衰减快
      -> 缓存短）就会把“后半段静音多少”当成响量差，标定出来的曲线是错的。
      这个窗与 tests/check_acoustics.py 的 WIN_MS 同口径，否则验收量的是另一回事。
    * 用 RMS 而不用峰值：峰值与 RMS 的差（波峰因数）才是各琴条的真实区别。
    """
    m = min(max(0, int(n)), int(LOUD_WIN_SEC * SAMPLE_RATE))
    if m <= 0:
        return 0.0
    a = array.array("h")
    a.frombytes(pcm[:2 * m])
    if not a:
        return 0.0
    total = 0
    for v in a:
        total += v * v
    return math.sqrt(total / float(len(a))) / 32768.0


# ------------------------------------------------------------------ 音色包切换接口
# 需求 §六.2：靠配置文件的 voice_pack 换一整套风格，播放层（audio.py）只认
# make_pack()，不知道换了什么。包名会进采样库目录名，因此各包互不覆盖。
#
# 预设只应该改两件事：分区用哪根琴条（family_voice 的琴条名）、全局声学参数
# （混响量/RT60/低通转折/尾音预算/变体数）。分区音量比写了也不算：calibrate() 会
# 按实测重算（原因见 calibrate 的注释），预设里的增益只是迭代起点。
VOICE_PACKS = {
    # 默认包：玫瑰木马林巴主打 + 木块/钢片琴/低音条分区（需求 §一.1 指定的三种质感）
    "acoustic": {"name": "acoustic"},
    # 温润包：编辑/方向键也换木质（去掉钢片琴的金属余韵），低通压到 6kHz、尾音更短，
    #   适合安静环境长时间打字
    "mellow": {"name": "mellow",
               "family_voice": {"key": ("marimba", 1.00), "tick": ("wood", 0.90),
                                "soft": ("wood", 0.90), "warm": ("lowbar", 0.90)},
               "tilt_hz": 6000.0, "reverb_wet": 0.04, "reverb_rt": 0.09,
               "tail_cap": 0.24},
    # 明亮包：钢片琴上位（主键区也用它），高频留更多空气感，混响稍短防止发糊
    "bright": {"name": "bright",
               "family_voice": {"key": ("glock", 1.00), "tick": ("wood", 0.90),
                                "soft": ("glock", 0.90), "warm": ("marimba", 0.90)},
               "tilt_hz": 11000.0, "reverb_wet": 0.05, "reverb_rt": 0.09,
               "tail_cap": 0.27},
}
DEFAULT_PACK = "acoustic"


def pack_names():
    """可供配置选择的音色包名单（config._sanitize 靠它判定非法包名）。"""
    return sorted(VOICE_PACKS)


# ------------------------------------------------------------------ 配置项的单一真相
# 需求 §六.3：混响量、随机化范围、各区域音量比都要能从配置文件调。这些键的【合法区间】
# 与【落到哪个渲染参数】只在下面这张表里写一次：config.py 拿它生成 DEFAULTS 并在
# _sanitize 里夹值，pack_from_config 拿它把值送进 VoicePack。两处各写一份数字一定会漂，
# 而漂了不会报错，只会把用户写的 0.5 音分当成 5 音分用。
#
# 默认值一律是“未设置”（config 里存 None）而不是这里的中值：VOICE_PACKS 的每个预设
# 自带一套混响/tilt/尾音参数，配置项一旦有个写死的默认值就会把预设的调校顶掉（实现在
# make_pack：预设先展开、配置项后覆盖）。区间与 VoicePack.__init__ 的夹取保持一致——
# 配置层先夹一次是为了“写进 json 的脏值不会让参数越出需求上限”，而不是多一道保险。
#
# 解释方式 kind：
#   direct 直接当参数值；ms 毫秒转秒（tail_ms -> tail_cap）；int 取整（变体条数）；
#   cents / db 是【随机化范围上限】，按上限归一去缩放 CENTS/LOUDNESS_JITTER 的分布形状
#   （保留预设的错落，而不是重排成均匀）；zone:xxx 写进 zone_target 而不是覆盖项。
# 区间上限就是需求硬指标：reverb_wet ≤WET_LIMIT(0.07，占比 ≤10% 的发送量反推值，
# 不是 10% 本身——见常量注释)、reverb_rt ≤0.20、tail_ms ≤300、
# humanize_cents ≤CENT_LIMIT(5)、humanize_db ≤DB_LIMIT(1)、分区比 0.85~0.90（低 10~15%）。
CONFIG_TUNES = (
    ("reverb_wet",        0.0,  WET_LIMIT, "reverb_wet",   "direct"),
    ("reverb_rt",        0.02,  0.20,  "reverb_rt",    "direct"),
    ("tilt_hz",        2000.0, 18000.0, "tilt_hz",     "direct"),
    ("tail_ms",         100.0,  300.0,  "tail_cap",    "ms"),
    ("humanize_variants",   1,   12,  "variants",      "int"),
    ("humanize_cents",    0.0,  CENT_LIMIT,  "cents",        "cents"),
    ("humanize_db",       0.0,  DB_LIMIT,    "loudness_db",  "db"),
    ("zone_tick_gain",   0.85,  0.90,  "zone:tick",    "direct"),
    ("zone_soft_gain",   0.85,  0.90,  "zone:soft",    "direct"),
    ("zone_warm_gain",   0.85,  0.90,  "zone:warm",    "direct"),
)


def config_keys():
    """配置里能写的音色项（含包名本身），供 config 建 DEFAULTS 与文档使用。"""
    return tuple([row[0] for row in CONFIG_TUNES] + ["voice_pack"])


def make_pack(name=None, slots=None, dense=False, **overrides):
    """
    按预设名 + 覆盖项造一个已标定的音色包；包名不认识就静默回落默认包。

    slots 给的是 (midi, timbre) 映射表（notes.all_slots()）：标定点就能落在每个家族
    自己的音高范围内。dense=True 则逐音高稠密标定（~1.3s，与采样库建库同一口径，
    验收测的就是发行配置）；不给 slots 就用全局 CALIB_REFS（平均值对、单键可以偏）。
    """
    key = name if name in VOICE_PACKS else DEFAULT_PACK
    params = dict(VOICE_PACKS[key])
    for k, v in overrides.items():
        if v is not None:
            params[k] = v
    pack = VoicePack(**params)
    pack.calibrate(refs=(refs_for(slots) if dense else refs_for(slots, 3))
                   if slots else CALIB_REFS)
    return pack


def _num(value, lo, hi, default=None):
    """配置里的数字：读不出合法值就用默认，越界就夹。渲染参数越界比配置读错更难查。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:                       # NaN
        return default
    return _clamp(v, lo, hi)


def pack_from_config(cfg, slots=None, dense=False):
    """
    从 Config（或任何带 .get 的映射）读音色参数，返回已标定的 VoicePack。
    需求 §六.3 的可调项都在这里进：混响量/RT60/随机化范围/各区域音量比。
    键名、区间、落地参数三件事全在 CONFIG_TUNES 一张表里（见它的注释）。
    每一项都是可选：缺省（None 或读不出数）就沿用自己音色包的值；任何一项非法
    都只会被夹回区间，不会报错——配置是用户手改的，报错等于让程序起不来。

    slots/dense：标定参考点怎么取。运行时走默认（每家族 3 点，~0.45s）；离线建库传
    dense=True 就逐音高稠密标定（~1.3s），跟采样库里的曲线同一口径。
    """
    get = cfg.get if hasattr(cfg, "get") else lambda k, d=None: d
    name = get("voice_pack") or DEFAULT_PACK
    zone = dict(ZONE_TARGET)
    overrides = {}
    for key, lo, hi, attr, kind in CONFIG_TUNES:
        v = _num(get(key), lo, hi, None)
        if v is None:                     # 未设置：跟随音色包自己的值
            continue
        if kind == "ms":
            overrides[attr] = v / 1000.0
        elif kind == "int":
            overrides[attr] = int(round(v))
        elif kind == "cents":             # 按上限归一，保留预设分布形状
            overrides[attr] = tuple(x * (v / CENT_LIMIT) for x in CENTS)
        elif kind == "db":
            overrides[attr] = tuple(x * (v / DB_LIMIT) for x in LOUDNESS_JITTER)
        elif attr.startswith("zone:"):     # 分区比是标定目标，不是 VoicePack 直参
            zone[attr.split(":", 1)[1]] = v
        else:
            overrides[attr] = v
    overrides["zone_target"] = zone
    # 预设自带的琴条映射/参数先展开，再用配置项覆盖；重新标定是因为换包/改 tilt 都会
    # 改变裸响量，不重标分区比就回到意外量上（标定口径全在 make_pack 一处）
    return make_pack(name, slots=slots, dense=dense, **overrides)

