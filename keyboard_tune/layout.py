# -*- coding: utf-8 -*-
"""
键盘布局模块：104 键全尺寸键盘物理信息的唯一来源。

设计要点
  * 以 Windows 虚拟键码（VK）作为"物理按键"的身份标识。字母键 VK 等于
    ord('A')..ord('Z')，数字键 VK 等于 ord('0')..ord('9')。
  * 104 键基准表覆盖全部标准按键（字母/数字/符号/功能/控制/方向/小键盘）。
  * 87 / 68 / 61 键等配列只是"某些物理键不存在"，共有键的 VK 相同，因此
    音高与音色映射天然一致；缺失键不会产生事件，自动适配且不报错。
  * row / col 表达物理网格坐标，用于判定"相邻键"（相邻不得同音高、同音色）。
"""
from collections import namedtuple

# ------------------------------------------------------------------ VK 常量
# 依据 Windows winuser.h。字母/数字键的 VK 就是其大写 ASCII 码。
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT_L = 0x10                    # VK_SHIFT
VK_CONTROL_L = 0x11                  # VK_CONTROL
VK_MENU_L = 0x12                     # VK_MENU (Alt)
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21                      # PageUp
VK_NEXT = 0x22                       # PageDown
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C                   # PrintScreen
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_HELP = 0x2F
VK_NUM0 = 0x60                       # 小键盘 0..9 = 0x60..0x69
VK_NUM9 = 0x69
VK_MULTIPLY = 0x6A
VK_ADD = 0x6B
VK_SUBTRACT = 0x6D
VK_DECIMAL = 0x6E
VK_DIVIDE = 0x6F
VK_F1 = 0x70                         # F1..F12 = 0x70..0x7B
VK_SCROLL = 0x91
VK_NUMLOCK = 0x90
VK_NUM_ENTER = 0x92
VK_SHIFT_R = 0xA1
VK_CONTROL_R = 0xA3
VK_MENU_R = 0xA5
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D                       # 右键菜单键
# ANSI 配列符号键
VK_OEM_3 = 0xC0                      # ` ~
VK_OEM_MINUS = 0xBD                  # - _
VK_OEM_PLUS = 0xBB                   # = +
VK_OEM_4 = 0xDB                      # [ {
VK_OEM_5 = 0xDC                      # \ |
VK_OEM_6 = 0xDD                      # ] }
VK_OEM_1 = 0xBA                      # ; :
VK_OEM_7 = 0xDE                      # ' "
VK_OEM_COMMA = 0xBC                  # , <
VK_OEM_PERIOD = 0xBE                 # . >
VK_OEM_2 = 0xBF                      # / ?
VK_OEM_8 = 0xDF
VK_OEM_102 = 0xE2                    # ISO <> |

# ------------------------------------------------------------------ 分区
ZONE_MAIN = "main"                   # 主键区 61 键
ZONE_FN = "fn"                       # 功能键排 13 键（Esc + F1..F12）
ZONE_STATUS = "status"               # PrtSc / ScrLk / Pause 3 键
ZONE_NAV = "nav"                     # 编辑键 + 方向键 10 键
ZONE_NUM = "numpad"                  # 小键盘 17 键
ZONES = (ZONE_FN, ZONE_STATUS, ZONE_MAIN, ZONE_NAV, ZONE_NUM)

# zone -> 音区（音色家族划分依据：同音区统一质感，不同音区区分层次）
ZONE_REGISTER = {
    ZONE_MAIN: "main",
    ZONE_FN: "control",
    ZONE_STATUS: "control",
    ZONE_NAV: "accent",
    ZONE_NUM: "bass",
}

KeyDef = namedtuple("KeyDef", "vk name zone row col")


def _vks(s):
    """字母/数字串 -> VK 列表。"""
    return [ord(c.upper()) for c in s]


# 主键区 5 行，按物理从左到右顺序给出，列号自动编号
_MAIN_ROWS = [
    [VK_OEM_3, *_vks("1234567890"), VK_OEM_MINUS, VK_OEM_PLUS, VK_BACK],
    [VK_TAB, *_vks("QWERTYUIOP"), VK_OEM_4, VK_OEM_6, VK_OEM_5],
    [VK_CAPITAL, *_vks("ASDFGHJKL"), VK_OEM_1, VK_OEM_7, VK_RETURN],
    [VK_SHIFT_L, *_vks("ZXCVBNM"), VK_OEM_COMMA, VK_OEM_PERIOD, VK_OEM_2,
     VK_SHIFT_R],
    [VK_CONTROL_L, VK_LWIN, VK_MENU_L, VK_SPACE, VK_MENU_R, VK_RWIN, VK_APPS,
     VK_CONTROL_R],
]
_NUM_ROWS = [
    [VK_NUMLOCK, VK_DIVIDE, VK_MULTIPLY, VK_SUBTRACT],
    [0x67, 0x68, 0x69, VK_ADD],
    [0x64, 0x65, 0x66],
    [0x61, 0x62, 0x63],
    [VK_NUM0, VK_DECIMAL, VK_NUM_ENTER],
]
_NAV_ROWS = [
    [VK_INSERT, VK_HOME, VK_PRIOR],
    [VK_DELETE, VK_END, VK_NEXT],
    [None, VK_UP, None],
    [VK_LEFT, VK_DOWN, VK_RIGHT],
]

_NAMES = {
    VK_BACK: "Backspace", VK_TAB: "Tab", VK_RETURN: "Enter",
    VK_ESCAPE: "Escape", VK_CAPITAL: "CapsLock", VK_SHIFT_L: "Shift",
    VK_SHIFT_R: "RShift", VK_CONTROL_L: "Ctrl", VK_CONTROL_R: "RCtrl",
    VK_MENU_L: "Alt", VK_MENU_R: "RAlt", VK_LWIN: "Win", VK_RWIN: "RWin",
    VK_APPS: "Menu", VK_SPACE: "Space", VK_PAUSE: "Pause",
    VK_SCROLL: "ScrollLock", VK_SNAPSHOT: "PrintScreen",
    VK_NUMLOCK: "NumLock", VK_DIVIDE: "NumDivide", VK_MULTIPLY: "NumMultiply",
    VK_SUBTRACT: "NumSubtract", VK_ADD: "NumAdd", VK_DECIMAL: "NumDecimal",
    VK_NUM_ENTER: "NumEnter", VK_INSERT: "Insert", VK_DELETE: "Delete",
    VK_HOME: "Home", VK_END: "End", VK_PRIOR: "PageUp", VK_NEXT: "PageDown",
    VK_UP: "ArrowUp", VK_DOWN: "ArrowDown", VK_LEFT: "ArrowLeft",
    VK_RIGHT: "ArrowRight", VK_OEM_3: "Grave", VK_OEM_1: "Semicolon",
    VK_OEM_7: "Quote", VK_OEM_MINUS: "Minus", VK_OEM_PLUS: "Equal",
    VK_OEM_4: "BracketLeft", VK_OEM_6: "BracketRight", VK_OEM_5: "Backslash",
    VK_OEM_COMMA: "Comma", VK_OEM_PERIOD: "Period", VK_OEM_2: "Slash",
    VK_OEM_8: "Oem8", VK_OEM_102: "Oem102", VK_HELP: "Help",
}
for _i in range(12):
    _NAMES[VK_F1 + _i] = "F%d" % (_i + 1)
for _i in range(10):
    _NAMES[VK_NUM0 + _i] = "Num%d" % _i
for _c in "QWERTYUIOPASDFGHJKLZXCVBNM0123456789":
    _NAMES.setdefault(ord(_c), "Key%s" % _c)


def _build():
    keys = {}

    def add(vk, zone, row, col):
        name = _NAMES.get(vk, "VK%02X" % vk)
        keys[vk] = KeyDef(vk, name, zone, row, col)

    add(VK_ESCAPE, ZONE_FN, 0, 0)
    for i in range(12):
        add(VK_F1 + i, ZONE_FN, 0, i + 1)
    for i, vk in enumerate((VK_SNAPSHOT, VK_SCROLL, VK_PAUSE)):
        add(vk, ZONE_STATUS, 0, i)
    for r, row in enumerate(_MAIN_ROWS):
        for c, vk in enumerate(row):
            add(vk, ZONE_MAIN, r, c)
    for r, row in enumerate(_NAV_ROWS):
        for c, vk in enumerate(row):
            if vk is not None:
                add(vk, ZONE_NAV, r, c)
    for r, row in enumerate(_NUM_ROWS):
        for c, vk in enumerate(row):
            add(vk, ZONE_NUM, r, c)
    return keys


KEYS = _build()                          # vk -> KeyDef，104 键基准全集
if len(KEYS) != 104:                      # 基准表是全部映射的地基，必须自检
    from collections import Counter
    _cnt = Counter(kd.zone for kd in KEYS.values())
    raise AssertionError("104 键基准表数量异常: %d, 分区=%s（应有 VK 重复）"
                        % (len(KEYS), dict(_cnt)))

# 26 个字母键（打字最高频区域，音高互不重复）
LETTERS = "abcdefghijklmnopqrstuvwxyz"
LETTER_VKS = {c: ord(c.upper()) for c in LETTERS}

# 长间隔 / 乐句收束键：分配到低音区根音
PHRASE_END_VKS = (VK_SPACE, VK_RETURN)

# 修饰键与切换键的重复事件抑制（按住不放不刷音）
_HELD_ALLOWED = {VK_SPACE}


def is_letter(vk):
    return 0x41 <= vk <= 0x5A


# ------------------------------------------------------------------ 相邻关系
def _adjacent():
    """
    同分区内：行相同列差 1，或列相同行差 1 —— 视为物理相邻。
    近似口径（行首键宽度不同导致半键错位仍按同列对齐处理），取偏严格的
    一侧更安全：宁可多约束一对，也不放过真正相邻的一对。
    """
    cells = {}
    for kd in KEYS.values():
        cells.setdefault((kd.zone, kd.row), {})[kd.col] = kd
    pairs = set()
    for (zone, row), cols in cells.items():
        seq = sorted(cols)
        for a, b in zip(seq, seq[1:]):
            if b - a == 1:
                pairs.add(frozenset((cols[a].vk, cols[b].vk)))
        for other_key, other_cols in cells.items():
            if other_key[0] != zone or abs(other_key[1] - row) != 1:
                continue
            for c in set(cols) & set(other_cols):
                pairs.add(frozenset((cols[c].vk, other_cols[c].vk)))
    return frozenset(pairs)


ADJACENT = _adjacent()


def are_adjacent(vk_a, vk_b):
    return frozenset((vk_a, vk_b)) in ADJACENT


def neighbors(vk):
    """返回某键的物理相邻 VK 集合。"""
    return NEIGHBOR_MAP.get(vk, _EMPTY)


# neighbors() 的预计算索引：映射构建与规则校验里会被高频调用
_EMPTY = frozenset()
NEIGHBOR_MAP = {}
for _pair in ADJACENT:
    _a, _b = tuple(_pair)
    NEIGHBOR_MAP.setdefault(_a, set()).add(_b)
    NEIGHBOR_MAP.setdefault(_b, set()).add(_a)
NEIGHBOR_MAP = {k: frozenset(v) for k, v in NEIGHBOR_MAP.items()}


# ------------------------------------------------------------------ 音名
NOTE_NAMES_SHARP = ("C", "C#", "D", "D#", "E", "F",
                    "F#", "G", "G#", "A", "A#", "B")


def midi_name(midi):
    """MIDI 音高 -> 音名，如 60 -> C4（A4 = 440Hz 记法）。"""
    return "%s%d" % (NOTE_NAMES_SHARP[midi % 12], midi // 12 - 1)


# ------------------------------------------------------------------ 配列兼容
# 常见配列相对 104 键缺失哪些物理键（用于兼容性测试；共有键映射保持一致）
def present_on(name):
    """返回指定配列上实际存在的 VK 集合。"""
    allv = set(KEYS)
    num = {vk for vk, kd in KEYS.items() if kd.zone == ZONE_NUM}
    nav = {vk for vk, kd in KEYS.items() if kd.zone == ZONE_NAV}
    status = {vk for vk, kd in KEYS.items() if kd.zone == ZONE_STATUS}
    fn = {vk for vk, kd in KEYS.items() if kd.zone == ZONE_FN}
    if name == "104":
        return allv
    if name == "87":                       # TKL：去小键盘
        return allv - num
    if name == "68":                       # 65%/68%：保留主键区+方向键+部分功能键
        return allv - num - status - {vk for vk in fn if vk != VK_ESCAPE} - {
            VK_PRIOR, VK_NEXT, VK_INSERT, VK_DELETE, VK_HOME, VK_END}
    if name == "61":                       # 60%：仅主键区（含 Esc 复用 ~ 位）
        return allv - num - nav - status - fn
    raise ValueError("未知配列: %s" % name)

LAYOUT_NAMES = ("104", "87", "68", "61")
