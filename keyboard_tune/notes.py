# -*- coding: utf-8 -*-
"""
按键 -> (音高, 音色) 映射模块。

数据来源与生成规则
  1. 26 个字母键的音高是 scripts/build_note_map.py 在约束下搜索出来的静态表
     （见 tests/test_mapping_rules.py 的独立复验），不是手拍的：
       H1 26 音互不重复，音域 A2..A5（恰好 3 个八度，中音区为主）
       H5 连击频次前 8 的声母（c d g h l n s z）全部落在自然大调骨干音级 1-3-5
       H7 韵母 a o e i u v 落在骨干音的协和邻音（主音 do + 商 re + 角 mi + 羽 la）
       H8 常用拼音连击不构成小二度/三全音/小七度/大七度
       H9 zh+ong / sh+ang / j+ing 逐对都是三度/五度/六度
  2. 其余按键（数字/符号/功能/控制/方向/小键盘）允许复用音高，但按分区轮转填充
     并跳过与物理相邻键相同的音高 —— 满足"复用音高的按键必须隔开"。
     空格/回车这类乐句收束键先行占位，取低音区根音（C3 / G3，纯五度收束）。
  3. 音色按"键盘分区 = 一个音区 = 一个音色家族"划分；同一家族内准备 a/b 两个
     细微变体，按棋盘格 (row + col) % 2 交替。这样"同一音区质感统一"与
     "相邻按键不得同音色"两条要求同时成立（网格相邻必然奇偶不同）。
"""
from . import layout as L

# ------------------------------------------------------------------ 字母音高
# 由约束求解产出（严格 H5 模式，代价 97120）：声母骨干音 / 韵母协和邻音 /
# 招牌拼音组合全部三度五度六度；常用连击（含 "ma/pa/shi" 这类权重重音节）
# 上，小二度/三全音/小七度/大七度的出现次数为零。变化音 6 个（f j k q r x）：
# A2..A5 内自然音只有 22 个，26 个互不重复音高至少要 4 个变化音，而把不协和
# 音程的硬约束范围从"每键前 2 名搭档"扩到"权重 ≥1000 的全部连击"后，可用
# 解只存在于带 6 个变化音的区域（阀值降到 700 则穷举无解）。
LETTER_PITCHES = {
    "a": 60, "b": 74, "c": 52, "d": 48, "e": 50, "f": 46, "g": 79,
    "h": 76, "i": 69, "j": 61, "k": 58, "l": 55, "m": 65, "n": 64,
    "o": 57, "p": 71, "q": 49, "r": 66, "s": 67, "t": 77, "u": 62,
    "v": 53, "w": 45, "x": 73, "y": 81, "z": 72,
}

# ------------------------------------------------------------------ 音色家族
# 分区 -> 家族（audio 模块按家族实现合成嗓音，每族 a/b 两档变体）
ZONE_FAMILY = {
    L.ZONE_MAIN: "key",        # 主键区：电钢/拨弦感，打字主体
    L.ZONE_FN: "tick",         # 功能键：短促清亮的边键感
    L.ZONE_STATUS: "tick",
    L.ZONE_NAV: "soft",        # 编辑/方向键：柔化玻璃感
    L.ZONE_NUM: "warm",        # 小键盘：温暖木质感（低音区）
}

# ------------------------------------------------------------------ 音高填充
# 乐句收束键：低音区根音，先占位，让相邻键自动避开同音高
PHRASE_PITCHES = {
    L.VK_SPACE: 48,            # C3 do
    L.VK_RETURN: 55,           # G3 sol（与空格成纯五度收束）
    L.VK_NUM_ENTER: 45,        # A2，小键盘区的收束根音
    L.VK_BACK: 59,             # B3，删除音轻提但不刺耳
    L.VK_TAB: 64,              # E4
    L.VK_ESCAPE: 50,           # D3，退出感向下
}

# 各分区可用音高（按阅读顺序轮转取用；都取自 C 大调自然音，保持调式色彩）
ZONE_POOLS = {
    L.ZONE_MAIN: (48, 50, 52, 53, 55, 57, 59, 62, 64, 65, 67, 69, 71, 72, 74),
    L.ZONE_FN: (72, 74, 76, 77, 79, 81),
    L.ZONE_STATUS: (74, 76, 79),
    L.ZONE_NAV: (64, 67, 69, 71, 72, 74, 76),
    L.ZONE_NUM: (45, 47, 48, 50, 52, 53, 55, 57, 59),
}

# 小键盘数字键：按 1..9,0 的顺序在低音区内做一条上扬的自然音阶，
# 连续敲数字时是一句顺的旋律，同时与主键区数字键相差一个八度以上以便区分。
NUM_DIGIT_ORDER = ("Num1", "Num2", "Num3", "Num4", "Num5",
                   "Num6", "Num7", "Num8", "Num9", "Num0")
NUM_DIGIT_PITCHES = (48, 50, 52, 53, 55, 57, 59, 60, 62, 45)


def _conflict(vk, midi, table):
    """该音高是否已被物理相邻键占用（禁止相邻同音高）。"""
    for nb in L.neighbors(vk):
        if table.get(nb) == midi:
            return True
    return False


def _build_pitch_table():
    table = {}
    for ch, midi in LETTER_PITCHES.items():
        table[ord(ch.upper())] = midi
    for vk, midi in PHRASE_PITCHES.items():
        table[vk] = midi
    # 小键盘数字优先按旋律序指定，避免被后面的轮转池打散
    for name, midi in zip(NUM_DIGIT_ORDER, NUM_DIGIT_PITCHES):
        vk = _VK_BY_NAME.get(name)
        if vk is not None:
            table[vk] = midi

    for zone in L.ZONES:
        pool = ZONE_POOLS[zone]
        keys = sorted((kd for kd in L.KEYS.values() if kd.zone == zone),
                      key=lambda kd: (kd.row, kd.col))
        idx = 0
        for kd in keys:
            if kd.vk in table:
                idx += 1
                continue
            chosen = None
            for step in range(len(pool) * 4):
                cand = pool[(idx + step) % len(pool)]
                if not _conflict(kd.vk, cand, table):
                    chosen = cand
                    break
            if chosen is None:
                # 兜底：在整个舒适音域内找第一个不与邻居冲突的音高（必然存在，
                # 一个键最多 4 个邻居）
                chosen = next(m for m in range(45, 85)
                              if not _conflict(kd.vk, m, table))
            table[kd.vk] = chosen
            idx += 1
    return table


_VK_BY_NAME = {kd.name: kd.vk for kd in L.KEYS.values()}

PITCH_BY_VK = _build_pitch_table()


def _timbre_for_kd(kd):
    family = ZONE_FAMILY[kd.zone]
    return "%s%s" % (family, "ab"[(kd.row + kd.col) % 2])


TIMBRE_BY_VK = {vk: _timbre_for_kd(kd) for vk, kd in L.KEYS.items()}

# 兜底：非 104 键基准上的按键（例如某些笔记本多媒体键、ISO <> 键），
# 给一个中性音高与音色，保证"有反馈且不报错"，同时不干扰正式映射。
FALLBACK_SLOT = (60, "keya")


def _key_of(vk):
    return L.KEYS.get(vk)


def pitch_of(vk):
    """物理键 -> MIDI 音高。"""
    kd = _key_of(vk)
    if kd is None:
        return FALLBACK_SLOT[0]
    return PITCH_BY_VK.get(vk, FALLBACK_SLOT[0])


def timbre_of(vk):
    """物理键 -> 音色标识（家族 + 变体，如 keya / keyb / tickb）。"""
    kd = _key_of(vk)
    if kd is None:
        return FALLBACK_SLOT[1]
    return TIMBRE_BY_VK.get(vk, FALLBACK_SLOT[1])


def slot_of(vk):
    """物理键 -> (音高, 音色)，audio 模块以此为预渲染槽位。"""
    return (pitch_of(vk), timbre_of(vk))


def all_slots():
    """全部需要预渲染的 (音高, 音色) 槽位（含兜底槽位，保证未知键也有声）。"""
    return sorted(set(slot_of(vk) for vk in L.KEYS) | {FALLBACK_SLOT})


def name_of(vk):
    kd = _key_of(vk)
    return kd.name if kd else "VK%02X" % vk


def describe(vk):
    """一行文本，用于日志与调试输出。"""
    return "%-12s midi=%-3d %-4s timbre=%s" % (
        name_of(vk), pitch_of(vk), L.midi_name(pitch_of(vk)), timbre_of(vk))
