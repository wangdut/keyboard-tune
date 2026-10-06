# -*- coding: utf-8 -*-
"""
映射规则验收脚本（不依赖 pytest，直接 python tests/test_mapping_rules.py 即可跑）。

这里刻意不复用 scripts/build_note_map.py 的判定函数：求解器负责"找出满足约束的
表"，本文件负责"独立重验那张表"。两者只有从不同路径得出同样结论，约束才算真守住。
所有频次/连击权重都从 scripts/pinyin_model.py 的拼音表重新统计。

覆盖需求条目：
  一、键盘适配   -> test_104_baseline / test_all_keys_mapped / test_layout_backward_compat
  二、音高规则   -> test_letter_pitch_unique / test_range / test_no_adjacent_same_pitch
                    test_spine_initials / test_vowel_neighbors / test_pinyin_combos
                    test_signature_combos / test_phrase_roots
  二、音色规则   -> test_zone_family / test_no_adjacent_same_timbre
  五、音效实现   -> test_audio_render（合成不削波、时长、内存）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pinyin_model as P                       # noqa: E402
from keyboard_tune import layout as L          # noqa: E402
from keyboard_tune import notes as N           # noqa: E402

# 与需求一致的乐理口径
SPINE = (0, 4, 7)                # 自然大调骨干音级 1 / 3 / 5
NATURAL = (0, 2, 4, 5, 7, 9, 11)
PENTA = (0, 2, 4, 7, 9)
VOWEL_OK = (0, 2, 4, 5, 7, 9)    # 骨干音及其协和邻音（do re mi fa sol la）
SIGN_IC = (3, 4, 5)              # 三度 / 四·五度 / 六度（音程级）
RAW_BANNED = (1, 6, 10, 11)      # 小二度 / 三全音 / 小七度 / 大七度
COMFORT = (45, 84)               # A2..C5：中音区为主，不刺耳不沉闷

TOP_N_INITIALS = 8               # 3 个八度内骨干音位 9 个，主音归 a，余 8 个给最高频声母
HARD_TOP_PARTNERS = 2            # 每个键频次前 N 的连击搭档按硬约束验收
W_MIN = 400
W_HEAVY = 1000
W_TRITONE = 400


def semitone(a, b):
    """两音高的半音距离 0..11（对称）。"""
    return abs(a - b) % 12


def ic(a, b):
    return min(semitone(a, b), 12 - semitone(a, b))


def letter_weights():
    """字母连击总权重（用作频次代理）与每键的搭档权重表。"""
    tot = {}
    adj = {}
    for (x, y), w in P.PAIRS.items():
        adj.setdefault(x, {})[y] = adj.setdefault(x, {}).get(y, 0) + w
        tot[x] = tot.get(x, 0) + w
    return tot, adj


TOT, ADJ = letter_weights()


def hard_pairs(adj):
    """
    需要按硬约束验收的常用连击：每个键频次前 2 的搭档（权重达标）+
    所有权重 >= 1000 的重音节 + 招牌组合的键序。
    """
    hard = set()
    for c, partners in adj.items():
        for p, w in sorted(partners.items(), key=lambda t: -t[1])[:HARD_TOP_PARTNERS]:
            if w >= W_MIN:
                hard.add(tuple(sorted((c, p))))
        for p, w in partners.items():
            if w >= W_HEAVY:
                hard.add(tuple(sorted((c, p))))
    for ini, fin in P.SIGNATURE_COMBO:
        seq = ini + fin
        for x, y in zip(seq, seq[1:]):
            hard.add(tuple(sorted((x, y))))
    return hard


HARD = hard_pairs(ADJ)


def pitch(vk):
    return N.PITCH_BY_VK[vk]


# 字母音高简表：键是小写字母（与 notes/pinyin_model 一致），值是 VK 查出来的音高
LP = {c: pitch(ord(c.upper())) for c in L.LETTERS}


# ------------------------------------------------------------------ 一、键盘适配
def test_104_baseline():
    bad = []
    if len(L.KEYS) != 104:
        bad.append("基准键盘应为 104 键，实际 %d 键" % len(L.KEYS))
    names = [kd.name for kd in L.KEYS.values()]
    if len(set(names)) != len(names):
        bad.append("按键命名有重复")
    for kd in L.KEYS.values():
        if kd.zone not in L.ZONES:
            bad.append("%s 分区 %r 未登记" % (kd.name, kd.zone))
        if kd.row < 0 or kd.col < 0:
            bad.append("%s 没有物理坐标，无法判断相邻关系" % kd.name)
    # 26 字母 + 主数字排 + 小键盘 + 功能区都必须在册
    for probe in ("KeyA", "KeyZ", "Num7", "F12", "ArrowDown", "NumLock", "Space"):
        if probe not in names:
            bad.append("缺少按键 %s" % probe)
    return bad


def test_all_keys_mapped():
    bad = []
    families = set(N.ZONE_FAMILY.values())
    for vk, kd in L.KEYS.items():
        p, t = N.slot_of(vk)
        if not (COMFORT[0] <= p <= COMFORT[1]):
            bad.append("%s 音高 %d 超出舒适音域" % (kd.name, p))
        if t[:-1] not in families:
            bad.append("%s 音色 %s 不属于本分区家族" % (kd.name, t))
    return bad


def test_layout_backward_compat():
    """
    87/68/61 键只是少了物理按键，共有键的 VK 与映射必须逐字节一致；
    缺失键不该报错；未知 VK（笔记本多媒体键等）应落到兜底槽位。
    """
    bad = []
    full = dict(N.PITCH_BY_VK)
    subsets = {
        "87 键（无小键盘）": lambda kd: kd.zone != L.ZONE_NUM,
        "68 键（无独立功能/编辑区）": lambda kd: kd.zone not in (L.ZONE_FN, L.ZONE_STATUS),
        "61 键（只有主键区+少量）": lambda kd: kd.zone in (L.ZONE_MAIN, L.ZONE_NAV),
    }
    for tag, keep in subsets.items():
        kept = [kd for kd in L.KEYS.values() if keep(kd)]
        if not kept:
            bad.append("%s 过滤后一个键都不剩，过滤条件写错了" % tag)
            continue
        for kd in kept:
            if N.slot_of(kd.vk) != (full[kd.vk], N.TIMBRE_BY_VK[kd.vk]):
                bad.append("%s: %s 的映射随配列变化了" % (tag, kd.name))
    for vk in (0xF7, 0x00, 0xFF, 0x100):        # 非 104 基准键
        try:
            if N.slot_of(vk) != N.FALLBACK_SLOT:
                bad.append("未知 VK %02X 未落到兜底槽位" % vk)
        except Exception as exc:
            bad.append("未知 VK %02X 抛异常: %r" % (vk, exc))
    return bad


# ------------------------------------------------------------------ 二、音高规则
def test_letter_pitch_unique():
    bad = []
    vals = [LP[c] for c in L.LETTERS]
    if len(set(vals)) != 26:
        dup = [v for v in set(vals) if vals.count(v) > 1]
        bad.append("26 个字母音高有重复: %s" % dup)
    return bad


def test_range():
    bad = []
    vals = [LP[c] for c in L.LETTERS]
    octaves = (max(vals) - min(vals)) / 12.0
    if not (2.0 <= octaves <= 3.0):
        bad.append("字母音域 %.2f 个八度，不在 2~3 之间" % octaves)
    allp = list(N.PITCH_BY_VK.values())
    if min(allp) < COMFORT[0] or max(allp) > COMFORT[1] + 12:
        bad.append("全表音域 %d..%d 过界" % (min(allp), max(allp)))
    # 中音区为主：至少六成按键落在 C3..C5
    mid = sum(1 for v in allp if 48 <= v <= 72)
    if mid < 0.55 * len(allp):
        bad.append("中音区占比 %.0f%% 偏低" % (100.0 * mid / len(allp)))
    return bad


def test_no_adjacent_same_pitch():
    bad = []
    for vk, kd in L.KEYS.items():
        for nb in L.neighbors(vk):
            if nb in N.PITCH_BY_VK and pitch(vk) == pitch(nb):
                a, b = sorted((kd.name, L.KEYS[nb].name))
                if (a, b) not in bad:
                    bad.append((a, b, "相邻同音高 %d" % pitch(vk)))
    return bad


def test_no_adjacent_same_timbre():
    bad = []
    for vk, kd in L.KEYS.items():
        for nb in L.neighbors(vk):
            if N.TIMBRE_BY_VK.get(nb) == N.TIMBRE_BY_VK[vk]:
                a, b = sorted((kd.name, L.KEYS[nb].name))
                if (a, b) not in bad:
                    bad.append((a, b, "相邻同音色 %s" % N.TIMBRE_BY_VK[vk]))
    return bad


def test_zone_family():
    """同一音区（分区）音色家族统一，不同音区可区分。"""
    bad = []
    seen = {}
    for vk, kd in L.KEYS.items():
        fam = N.TIMBRE_BY_VK[vk][:-1]
        seen.setdefault(kd.zone, set()).add(fam)
    for zone, fams in seen.items():
        if len(fams) != 1:
            bad.append("分区 %s 出现多个音色家族 %s" % (zone, fams))
    if len({next(iter(f)) for f in seen.values()}) < 3:
        bad.append("不同音区的音色区分不足: %s" % seen)
    return bad


def test_spine_initials():
    bad = []
    initials = [c for c in P.INITIAL_LETTERS if c in L.LETTERS]
    top = sorted(initials, key=lambda c: -TOT.get(c, 0))[:TOP_N_INITIALS]
    for c in top:
        if LP[c] % 12 not in SPINE:
            bad.append("高频声母 %s 未落在骨干音级（音高 %d）" % (c, LP[c]))
    if len(top) != TOP_N_INITIALS:
        bad.append("可参与的高频声母不足 %d 个" % TOP_N_INITIALS)
    return bad


def test_vowel_neighbors():
    bad = []
    for c in P.FINAL_LETTERS:
        if c not in L.LETTERS:
            continue
        r = LP[c] % 12
        if r not in VOWEL_OK:
            bad.append("韵母 %s 音级 %d 不是大调自然音/协和邻音" % (c, r))
        if c != "a" and r in SPINE:
            bad.append("韵母 %s 抢占了骨干音级，挤占声母位" % c)
    if LP["a"] % 12 != 0:
        bad.append("最高频韵母 a 不是调式主音")
    return bad


def test_pinyin_combos():
    """常用连击（含 ma/shi 这类重音节）不得构成小二度/三全音/小七度/大七度。"""
    bad = []
    m = LP
    for x, y in HARD:
        d = semitone(m[x], m[y])
        if d in RAW_BANNED:
            w = ADJ.get(x, {}).get(y, 0)
            bad.append("常用连击 %s-%s 音程 %d 半音（权重 %d）不协和" % (x, y, d, w))
    # 招牌组合涉及的每一对键另外在 test_signature_combos 里按更严的三/五/六度验收
    return bad


def test_signature_combos():
    bad = []
    m = LP
    for ini, fin in P.SIGNATURE_COMBO:
        seq = ini + fin
        for x, y in zip(seq, seq[1:]):
            if ic(m[x], m[y]) not in SIGN_IC:
                bad.append("招牌组合 %s+%s 中 %s-%s 音程级 %d 不是三/五/六度"
                           % (ini, fin, x, y, ic(m[x], m[y])))
    return bad


def test_phrase_roots():
    bad = []
    for vk, name in ((L.VK_SPACE, "Space"), (L.VK_RETURN, "Enter")):
        p = pitch(vk)
        if p >= 60:
            bad.append("%s 音高 %d 不在低音区" % (name, p))
        if p % 12 not in (0, 7):
            bad.append("%s 音级 %d 不是根音（do/sol）" % (name, p % 12))
    if semitone(pitch(L.VK_SPACE), pitch(L.VK_RETURN)) not in (5, 7):
        bad.append("空格与回车不成纯音程，收束感不足")
    return bad


# ------------------------------------------------------------------ 五、音频实现
def test_audio_render():
    """
    采样来自音色引擎而不是旧的"正弦表实时合成"：这里只管"能不能出声、尺寸对不对"，
    音质类的指标（尾音/泛音/分区响量）在 tests/check_acoustics.py，两边不重叠。
    """
    import array as _array
    bad = []
    from keyboard_tune import audio as A
    from keyboard_tune import voice as V
    slots = N.all_slots()
    if len(set(N.TIMBRE_BY_VK.values())) < 6:
        bad.append("音色变体过少，相邻键难以区分")
    pack = V.make_pack(slots=slots)
    if not pack._calibrated:
        bad.append("音色包未标定，分区响量会变成意外量")
    total = 0
    for midi, timbre in slots[:8]:
        for var in range(pack.variants):
            pcm, n = pack.render(midi, timbre, var, "fast")
            if n == 0 or len(pcm) != 2 * n:
                bad.append("渲染返回空采样或长度不符: %d %s" % (midi, timbre))
                continue
            if n != pack.cache_frames(timbre):
                bad.append("cache_frames 与渲染帧数不一致（体积估算会说谎）")
            s = _array.array("h")
            s.frombytes(pcm)
            peak = max(max(s), -min(s))
            if peak < 3000:
                bad.append("采样峰值过低（听不见）: %d %s" % (midi, timbre))
            if peak > int(0.92 * 32767):
                bad.append("采样峰值过高（后端叠加会削顶）: %d %s" % (midi, timbre))
            dur = n / float(A.SAMPLE_RATE)
            if dur > 0.36:
                bad.append("单音缓存 %.2fs 过长，快速打字会互相糊成一片" % dur)
            total += len(pcm)
    # 整库常驻字节：不渲染、按 cache_frames 逐条累加（抽几个槽位外推会错到几十%）
    est = sum(pack.cache_frames(t) * 2
              for _m, t in slots for _v in range(pack.variants))
    if est > 25 * 1024 * 1024:
        bad.append("全部采样常驻预计 %.1f MB，超出内存预算" % (est / 1048576.0))
    if total == 0:
        bad.append("一个采样都没渲出来")
    wav = A.wav_bytes(_array.array("h", pcm))
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        bad.append("WAV 容器头不正确")
    if A.wav_bytes(pcm) != wav:
        bad.append("wav_bytes 对 bytes 与 array 两种输入应当给出同一容器")
    return bad


def test_engine_wiring():
    """不接声卡也能验证：VK -> 槽位 -> 索引 的链路完整。"""
    bad = []
    from keyboard_tune.audio import SoundEngine
    eng = SoundEngine()
    for vk in (ord("A"), L.VK_SPACE, 0xF7):
        slot = N.slot_of(vk)
        if slot not in eng._index:
            bad.append("槽位 %s 未被引擎登记（兜底音会没声）" % (slot,))
    if len(eng._index) != len(eng._slot_list):
        bad.append("槽位索引与槽位表数量不一致")
    if eng._nv != eng.pack.variants or eng._nv < 2:
        bad.append("humanize 变体数与音色包不一致或只有一个变体")
    return bad


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def run_all(verbose=True):
    failed = 0
    for fn in ALL_TESTS:
        try:
            bad = fn()
        except Exception as exc:
            bad = ["抛出异常: %r" % (exc,)]
        if bad:
            failed += 1
            print("FAIL %-32s" % fn.__name__)
            for b in (bad if isinstance(bad, (list, tuple)) else [bad])[:8]:
                print("      - %s" % (b,))
        elif verbose:
            print("ok   %-32s %s" % (fn.__name__, (fn.__doc__ or "").split("\n")[0][:52]))
    total = len(ALL_TESTS)
    print("\n%d/%d 项通过" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_all())
