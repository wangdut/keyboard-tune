#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音高映射求解器（开发期工具，不参与程序运行）。

把需求文档的乐理约束变成可求解的约束优化问题，运行后打印静态映射表，
粘贴进 keyboard_tune/notes.py 作为运行时常量。映射不是在表格里手拍的，
而是在约束下搜索出来的，并由 tests/test_mapping_rules.py 二次独立验证。

硬约束
  H1 26 个字母音高互不重复，落在 3 个八度以内、以中音区为主
  H5 高频声母循环落在自然大调骨干音级 1-3-5（do mi sol）
  H7 韵母落在骨干音的协和邻音
  H8 常用拼音连击对不得构成小二度/大七度
  H9 需求点名的 zh+ong / sh+ang / j+ing 必须落在三/五/六度
优化目标
  O1 压低三全音连击   O2 提高骨干音级命中率   O3 音高贴近中音区中心

【需求过约束的证明与落地口径】
若要求"每一个声母 × 每一个韵母头字母"都避开小二度/大七度：韵母若取五声正音
（需求要求的骨干音+协和邻音），其左右半音邻级会被全部封死，3 个八度内只剩
15 个可用音高，而声母需要 20 个互不重复音高 —— 数学上无解（本脚本
`--prove` 会打印该计数）。因此按需求原文"**常用**拼音组合"的口径量化：
每个键只与其频次最高的 HARD_TOP 个连击搭档构成硬约束，其余连击作为软代价
最小化。最终 H8 覆盖的连击权重占总连击权重的比例会打印出来（>90% 即认为
常用组合已全面协和）。
用法：python scripts/build_note_map.py [--rounds 8] [--prove]
"""
import argparse
import itertools
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from keyboard_tune import layout as L                      # noqa: E402
from scripts import pinyin_model as P                      # noqa: E402

C4, C5 = 60, 72
VOWEL_SET = set("aoeuiv")
INITIALS = [c for c in L.LETTERS if c not in VOWEL_SET]
SPINE = (0, 4, 7)                         # do mi sol
SPINE_MISS = 30000                        # 高频声母脱离骨干音级的代价（远大于次要目标）
PENTA_RES = (0, 2, 4, 7, 9)               # 宫商角徵羽
NATURAL_RES = (0, 2, 4, 5, 7, 9, 11)      # C 大调自然音
LETTER_POOL = range(45, 82)              # A2 .. A5 = 3.0 个八度（满足 H1，不取更低/更高）
SPINE_SLOTS = [n for n in LETTER_POOL if n % 12 in SPINE]   # 3 个八度内共 9 个骨干音位

HARD_TOP = 2                              # 每个键的硬约束搭档数
W_MIN = 400                  # 低于该频次不配作硬约束
W_TRITONE = 400              # 连击频次达此值则连三全音也禁止（最不协和音程）
W_HEAVY = 1000               # 频次达此值的连击无条件按硬约束处理（补足 HARD_TOP 漏掉的）
# 该阀值是实测下限：降到 700（把 g-o 等 708 权重的连击也纳入硬约束）后，
# 严格 H5 在 2568 个节点内穷举完全部搜索空间且无解（未触到节点预算，是穷举结
# 论而非"没搜到"；复现：--budget 120000 --iters 1）。需求点名的"常用组合"
# 不可能覆盖到所有权重 ≥700 的音节，取 1000 以上为重音节。
HARD_PENALTY = 10 ** 7

# 连击图 ADJ[c] = {partner: weight}：输入同一个拼音时前后相邻敲击的两个键
ADJ = {c: {} for c in L.LETTERS}
for (x, y), w in P.PAIRS.items():
    if x in ADJ and y in ADJ and x != y:
        ADJ[x][y] = ADJ[x].get(y, 0) + w

# 使用频次分档（决定音级要求的强弱）


def letter_freq(c):
    """该键在拼音输入中的连击强度（反映实际敲键频率）。"""
    return sum(ADJ[c].values())


MAX_FREQ = max(letter_freq(c) for c in L.LETTERS)


def reg_weight(c):
    """离音区中心的惩罚斜率：使用频次越高，越要求待在中音区。"""
    return 16 + 44 * (letter_freq(c) / MAX_FREQ)

# SPINE_REQ：按实际连击频次排序的前 8 个声母键（包含 zh/ch/sh 共用的 h 与
#            入口键 z c s，均属需求点名的"高频声母"），落在骨干音级 1-3-5
# PENTA_REQ：其余声母与零声母，落在骨干音的协和邻音 re/la（商、羽）
# 【为何恰好是 8 个】3 个八度内骨干音只有 9 个音位，主音 C4 留给最高频韵母 a，
# 恰好余下 8 个：把骨干音位全部给到最常用键，是需求在有限音域下的最大满足。
# 【需求过约束 2】需求把 21 个声母（共 18 个物理键）全部列为"高频声母要求骨干
# 音级"，数学上不可能全部满足，因此按频次取前 8 个作硬优先，溢出部分落在协和邻音。
SPINE_REQ = set(sorted(INITIALS, key=letter_freq, reverse=True)[:8])
PENTA_REQ = set(INITIALS) - SPINE_REQ
assert SPINE_REQ | PENTA_REQ == set(INITIALS), "声母频次分档未覆盖全部声母"

# 声母 -> 目标音级（C 大调）：同属骨干音/邻音时再优先自己的指定音级，
# 使最高频的落在主音，次高频落在角音，形成稳定的 1-3-5 旋律骨架。
SPINE_TIER = {
    "d": 0, "g": 0, "t": 0, "b": 0,
    "j": 4, "h": 4, "z": 4, "c": 4, "s": 4,
    "l": 7, "n": 7, "k": 7, "q": 7,
    "p": 2, "m": 2, "f": 2, "r": 9, "x": 9,
    "y": 4, "w": 9,
}

# 结构性硬连击：卷舌音的 h、-ng 韵尾、需求点名的招牌组合
_STRUCTURAL = [("n", "g"), ("z", "h"), ("c", "h"), ("s", "h"), ("h", "a"),
               ("h", "o"), ("h", "e"), ("h", "i"), ("h", "u")]
for _ini, _fin in P.SIGNATURE_COMBO:
    _seq = _ini + _fin
    _STRUCTURAL += list(zip(_seq, _seq[1:]))


def build_hard_pairs():
    """
    硬约束连击集合：每个键频次最高的 HARD_TOP 个搭档 + 所有权重 >= W_HEAVY 的
    连击 + 结构性连击（招牌拼音的键序）。
    第二条是必要的：只看"每个键的前 2 名"会漏掉双方都排在第 3 名开外、但绝对
    频次极高的音节（例如 a-m 权重 1158，a 的前两名是 n/i，m 的前两名是 e/i），
    而这类音节天天都要敲，不能只当软代价。
    """
    hard = set()
    for c in L.LETTERS:
        for p, w in sorted(ADJ[c].items(), key=lambda t: -t[1])[:HARD_TOP]:
            if w >= W_MIN:
                hard.add(frozenset((c, p)))
        for p, w in ADJ[c].items():
            if w >= W_HEAVY:
                hard.add(frozenset((c, p)))
    for x, y in _STRUCTURAL:
        if x in ADJ and y in ADJ:
            hard.add(frozenset((x, y)))
    return hard


HARD = build_hard_pairs()

# 招牌组合的相邻键对：需求要求这些音程严格为三/五/六度（比 H8 更严）
SIGN_PAIRS = set()
for _ini, _fin in P.SIGNATURE_COMBO:
    _seq = _ini + _fin
    SIGN_PAIRS |= {frozenset(p) for p in zip(_seq, _seq[1:])}
SIGN_PENALTY = 10 ** 5
# 构造期剪枝阀值：招牌组合（H9）与常用连击（H8）违反都不允许进入搜索分支，
# 仅靠"代价排序 + 终态过滤"会使 beam 保留大量不可行分支然后全体被弃用（无解）。
BLOCK = SIGN_PENALTY


def is_hard(x, y):
    return frozenset((x, y)) in HARD


def isd(nx, ny):
    """
    半音距离 0..11。音程级 ic() 会把"大二度(2)"和"小七度(10)"都归到 2，
    而前者是顺口的级进、后者是刺耳的跳进，判定硬约束时必须区分开来。
    """
    return abs(nx - ny) % 12


RAW_BANNED = (1, 6, 10, 11)   # 半音距离：小二度 / 三全音 / 小七度 / 大七度
W_SEPT = 10                   # 小七度软代价系数（需求未点名禁止，但尽量推开）


# ------------------------------------------------------------------ 代价函数
def shape_one(c, n):
    """
    单字母音型代价（O2 骨干音级命中 + O3 中音区贴近 + 大调色彩）。
    高频声母要求落在 1-3-5；其余声母要求五声正音，变化音重罚；
    中音区惩罚按使用频次加权 —— 越常用的键越待在听感最舒适的中音区。
    韵母单独一类：它们本来就应落在骨干音的协和邻音，主音 a 例外（它就坐 1 级）。
    """
    r = n % 12
    s = 0
    if c in VOWEL_SET:
        if c != "a" and r in SPINE:
            s += 1200                         # 韵母不占骨干音位
    elif c in SPINE_REQ:
        if r not in SPINE:
            s += SPINE_MISS            # 高频声母必须优先抢到骨干音位（主导项）
        elif r == SPINE_TIER[c]:
            s -= 150                        # 命中自己的目标音级更佳
    else:
        # 【H5 落地口径】3 个八度内骨干音只有 9 个音位，主音 C4 归最高频韵母 a，
        # 余下 8 个必须完整留给频次前 8 的声母 —— 故对非高频声母占用骨干音位施以
        # 硬约束（而非软惩罚），否则优化器会为了次要目标漏掉高频声母的骨干音级。
        if r in SPINE:
            s += HARD_PENALTY                 # 不抢骨干音位
        elif r in PENTA_RES:
            s += 0
        elif r in NATURAL_RES:
            s += 1000                       # 清角/变宫：可用但少用
        else:
            s += 2200                       # 变化音：只给最低频的键
    s += (max(0, C4 - n) + max(0, n - C5)) * reg_weight(c)
    return s


def pair_one(c, n, assign):
    """c 取音高 n 时与已分配字母的连击代价。"""
    s = 0
    for p, w in ADJ[c].items():
        if p not in assign:
            continue
        icv = P.ic(n, assign[p])
        if frozenset((c, p)) in SIGN_PAIRS and icv not in P.CONSONANT_PREFER:
            s += SIGN_PENALTY * max(1, w // 100)   # H9：严格三/五/六度
        elif icv in P.BANNED_IC:
            s += HARD_PENALTY if is_hard(c, p) else w * 40
        elif icv == 6:
            s += w * (3 if is_hard(c, p) else 1)   # O1 三全音：常用连击重罚
        elif isd(n, assign[p]) == 10:
            s += w * W_SEPT                        # O4 小七度跳进
    return s


def total_cost(m):
    """可行解排序用：音型代价 + 不协和连击加权 + 招牌组合违反。"""
    s = sum(shape_one(c, m[c]) for c in L.LETTERS if c in m)
    for x in m:
        for y, w in ADJ[x].items():
            if y in m and x < y:
                icv = P.ic(m[x], m[y])
                if frozenset((x, y)) in SIGN_PAIRS and icv not in P.CONSONANT_PREFER:
                    s += SIGN_PENALTY
                elif icv in P.BANNED_IC:
                    s += w * 40
                elif icv == 6:
                    s += w * (3 if is_hard(x, y) else 1)
                elif isd(m[x], m[y]) == 10:
                    s += w * W_SEPT
    return s


def banned_hits(m, hard_only=True):
    """小二度/大七度连击；默认只报硬约束项（H8 违反）。ADJ 双向记录，按 x<y 去重。"""
    return sorted([((x, y), w) for x in m for y, w in ADJ[x].items()
                   if y in m and x < y and P.ic(m[x], m[y]) in P.BANNED_IC
                   and (is_hard(x, y) or not hard_only)], key=lambda t: -t[1])


def tritone_hits(m):
    return sorted([((x, y), w) for x in m for y, w in ADJ[x].items()
                   if y in m and x < y and P.ic(m[x], m[y]) == 6],
                  key=lambda t: -t[1])


def hard_tritone_hits(m):
    """高频连击上的三全音（硬约束违反，必须为空）。"""
    return [h for h in tritone_hits(m) if h[1] >= W_TRITONE]


def rough_hits(m):
    """
    常用连击（HARD 集）上的全部不协和音程：小二度/三全音/小七度/大七度。
    这是最终交付表的验收口径，必须为空。
    """
    out = [((x, y), w, isd(m[x], m[y])) for x in m for y, w in ADJ[x].items()
           if y in m and x < y and is_hard(x, y) and isd(m[x], m[y]) in RAW_BANNED]
    return sorted(out, key=lambda t: -t[1])


def hard_coverage(m):
    """硬约束连击覆盖的权重占比（衡量"常用组合协和"达成程度）。"""
    tot = sum(w for x in m for y, w in ADJ[x].items() if y in m and x < y)
    hd = sum(w for x in m for y, w in ADJ[x].items()
             if y in m and x < y and is_hard(x, y))
    return 100.0 * hd / tot if tot else 0.0


def signature_ok(m):
    """H9：招牌组合全部为三/五/六度。"""
    for ini, fin in P.SIGNATURE_COMBO:
        seq = ini + fin
        for x, y in zip(seq, seq[1:]):
            if P.ic(m[x], m[y]) not in P.CONSONANT_PREFER:
                return False
    return True


# ------------------------------------------------------------------ 变量域
# 韵母音级域：商 re(2) / 清角 fa(5) / 羽 la(9) —— 它们是骨干音 1-3-5 的协和
# 邻音（需求原文“韵母按键分配到协和邻音”），且不占用骨干音位，把 9 个骨干
# 音位完整留给高频声母（主音 C4 归最高频韵母 a，余 8 个归 8 个高频声母）。
VOWEL_CHROMA = (2, 5, 9)
VOWEL_SPAN = range(C4 - 12, C4 + 15)        # 韵母收在中音区上下各一个八度
SIGN_IC = (3, 4, 5)   # 三度/五度/六度（六度折进一个八度内即小三/大三）


def pair_ok(x, nx, y, ny):
    """
    两键取指定音高时的硬约束判定：
      H8 常用连击禁小二度/大七度；H9 招牌组合必须三/五/六度；
      高频连击（w >= W_TRITONE）额外禁三全音 —— 三全音是最不协和的音程，
      把它留在 "ma / shi" 这类天天都要敲的音节上是听感事故，不能只当软代价。
      HARD 集（含 w >= W_HEAVY 的重音节）进一步禁小七度：音程级分不出
      大二度与小七度，而只有后者是应当避开的跳进。
    """
    icv = P.ic(nx, ny)
    pair = frozenset((x, y))
    if pair in SIGN_PAIRS:
        return icv in SIGN_IC
    if pair in HARD and isd(nx, ny) in RAW_BANNED:
        return False
    if icv == 6 and ADJ[x].get(y, 0) >= W_TRITONE:
        return False
    return True


def blocked(c, n, assign):
    """
    c 取音高 n 时，是否与已分配键构成硬约束违反。
    必须单独判定：不能拿"代价 >= 某阀值"当硬约束，因为不协和连击的软代价会累加
    （单个 w*40 最大可达六万），累加后超过阀值会误杀本来可行的分支。
    硬约束关系图取 CPARTNERS 的超集（高频三全音不在 CPARTNERS 里）。
    """
    for p in HPARTNERS[c]:
        if p in assign and not pair_ok(c, n, p, assign[p]):
            return True
    return False


# 受硬约束连接的键对（用于弧一致剪枝）
CPARTNERS = {c: set() for c in L.LETTERS}
for _p in (HARD | SIGN_PAIRS):
    _x, _y = tuple(_p)
    CPARTNERS[_x].add(_y)
    CPARTNERS[_y].add(_x)

# 硬约束图上需要检查的全部搭档：约束对（H8/H9）+ 高频连击（三全音禁用）
HPARTNERS = {c: set(CPARTNERS[c]) | {p for p, w in ADJ[c].items()
                                     if w >= W_TRITONE}
             for c in L.LETTERS}


def build_domains(strict=True):
    """
    每个字母的候选音高集合（把需求硬约束直接编码成定义域）：
      a 固定调式主音 C4；韵母取 re/fa/la；高频声母只许落骨干音位；
      其余声母只许落非骨干音位（保证 8 个骨干音位不被次要键占用）。
    然后做 AC-3 弧一致化：删掉在任何硬约束搭档那边都找不到相容取值的音高。
    strict=False 用于验证“骨干音位完整分配”确实可行：把高频声母的定义域放开，
    靠 SPINE_MISS 代价软约束——如果软约束能解而硬约束不能，才说明需求必须放宽。
    """
    dom = {}
    for c in L.LETTERS:
        if c == "a":
            dom[c] = (C4,)
        elif c in VOWEL_SET:
            dom[c] = tuple(n for n in VOWEL_SPAN if n % 12 in VOWEL_CHROMA)
        elif c in SPINE_REQ:
            dom[c] = (tuple(n for n in SPINE_SLOTS if n != C4) if strict
                      else tuple(LETTER_POOL))
        else:
            dom[c] = tuple(n for n in LETTER_POOL if n % 12 not in SPINE)
    changed = True
    while changed:
        changed = False
        for x in L.LETTERS:
            keep = [n for n in dom[x] if all(
                any(pair_ok(x, n, y, m) for m in dom[y] if m != n)
                for y in HPARTNERS[x])]
            if len(keep) != len(dom[x]):
                dom[x] = tuple(keep)
                changed = True
    return dom


# ------------------------------------------------------------------ 搜索
def order_for(dom):
    return sorted(L.LETTERS, key=lambda c: (len(dom[c]), -letter_freq(c)))


def search(dom, bound, budget, rng=None, jit=0.0):
    """
    精确回溯 + 分支定界（26 键一次性联合求解，韵母音高也参与搜索）。
      * 变元序：MRV —— 先定“剩余候选最少”的未分配键，它们也正是约束最紧的
        交汇点（n / h / o / i 四个键被三个招牌组合共同穿过）；
      * 取值序：单键代价升序，配合下界（已分配代价 + 该值代价 + 其余键
        最小可能代价）一旦不优于当前最优解就整枝剪掉；
      * 硬约束：在生成阶段就剪掉违反 H8 / H9 的分支，不依赖终态过滤。
    返回（最优分配, 代价, 搜索节点数）；找不到优于 bound 的解时返回 (None, bound, 节点数)。
    """
    letters = list(L.LETTERS)
    min_shape = {c: min([shape_one(c, n) for n in dom[c]] or [0.0])
                 for c in letters}
    best = [None, bound, 0]          # 第三项：达到完整分配（叶节点）的次数
    cur = {}
    used = set()
    nodes = [0]

    def bt(cost):
        nodes[0] += 1
        if nodes[0] > budget:
            return
        if len(cur) == len(letters):
            best[0], best[1], best[2] = dict(cur), cost, best[2] + 1
            return
        # MRV：在已分配音高的前提下重数剩余候选（动态变元序）
        pick = min(((sum(1 for n in dom[c] if n not in used),
                     -letter_freq(c), c)
                    for c in letters if c not in cur))
        c = pick[2]
        lb_rest = sum(min_shape[k] for k in letters if k not in cur and k != c)
        opts = sorted((shape_one(c, n) + pair_one(c, n, cur)
                       + (rng.random() * jit if rng else 0.0), n)
                      for n in dom[c] if n not in used)
        for add, n in opts:
            if blocked(c, n, cur) or add >= HARD_PENALTY:   # H8 / H9 / 定义域外
                continue
            if cost + add + lb_rest >= best[1]:
                break                          # 取值序递增，整枝剪掉
            cur[c] = n
            used.add(n)
            bt(cost + add)
            del cur[c]
            used.discard(n)
            if nodes[0] > budget:
                return
    bt(0.0)
    return best[0], best[1], (nodes[0], best[2])


def two_opt(m, dom, rng, passes=30):
    """2-opt：两键交换音高，跳出单键局部最优（只在定义域内，硬约束天然保持）。"""
    for _ in range(passes):
        base = total_cost(m)
        improved = False
        letters = list(L.LETTERS)
        rng.shuffle(letters)
        for a, b in itertools.combinations(letters, 2):
            trial = dict(m)
            trial[a], trial[b] = m[b], m[a]
            if any(trial[c] not in dom[c] for c in (a, b)):
                continue
            if banned_hits(trial) or not signature_ok(trial):
                continue
            if total_cost(trial) < base:
                m, improved = trial, True
                break
        if not improved:
            break
    return m


def coordinate_descent(m, dom, order, sweeps=14):
    """逐键择优：固定其他键，为该键挑当前总代价最小的音高。"""
    for _ in range(sweeps):
        changed = False
        for c in order:
            used = {v for k, v in m.items() if k != c}
            rest = dict(m)
            del rest[c]
            best, best_sc = m[c], None
            for n in dom[c]:
                if n in used or blocked(c, n, rest):
                    continue
                sc = shape_one(c, n) + pair_one(c, n, rest)
                if sc >= HARD_PENALTY:             # 定义域外音级（仅软约束模式）
                    continue
                if best_sc is None or sc < best_sc:
                    best, best_sc = n, sc
            if best != m[c]:
                m[c] = best
                changed = True
        if not changed:
            break
    return m


def polish(m, dom, seed, rounds=6):
    """局部精修：坐标下降 + 2-opt 交替，直到代价不再下降。"""
    order = order_for(dom)
    for _ in range(rounds):
        before = total_cost(m)
        m = coordinate_descent(m, dom, order)
        m = two_opt(m, dom, random.Random(seed))
        if total_cost(m) >= before:
            break
    return m


def feasible(m, dom, strict=True):
    """独立重验全部硬约束（不依赖搜索过程的剪枝正确性）。"""
    if len(m) != 26 or len(set(m.values())) != 26:
        return "H1: 26 个字母音高必须互不重复"
    if any(m[c] not in dom[c] for c in m):
        return "定义域: 音高超出该键允许范围"
    if banned_hits(m):
        return "H8: 常用连击构成小二度/大七度"
    if not signature_ok(m):
        return "H9: 招牌拼音组合不是三/五/六度"
    if hard_tritone_hits(m):
        return "H8b: 高频连击构成三全音"
    if rough_hits(m):
        return "H8c: 常用连击构成小二度/三全音/小七度/大七度"
    if any(m[c] % 12 not in SPINE for c in SPINE_REQ) and strict:
        return "H5: 高频声母未落在骨干音级"
    return None


# ------------------------------------------------------------------ 输出
def show(m, tag):
    print("\n===== %s =====" % tag)
    print("字母音高（按音高升序）:")
    for c in sorted(m, key=lambda x: m[x]):
        role = ("韵母" if c in VOWEL_SET
                else "骨干" if m[c] % 12 in SPINE else "邻音")
        print("   %s -> %-3d %-4s %-10s %s" % (c, m[c], P.note_name(m[c]),
                                               L.KEYS[ord(c.upper())].name, role))
    lo, hi = min(m.values()), max(m.values())
    print("字母音域 %.2f 个八度 (%s..%s)" % ((hi - lo) / 12.0,
                                            P.note_name(lo), P.note_name(hi)))
    print("H8 常用连击小二度/大七度违规:", banned_hits(m) or "无")
    print("   全部连击（含低频）小二度/大七度:", len(banned_hits(m, False)))
    print("H8 硬约束权重覆盖率 %.1f%%" % hard_coverage(m))
    print("O1 三全音连击 Top:", tritone_hits(m)[:5] or "无")
    print("   高频连击上的三全音（应为无）:", hard_tritone_hits(m) or "无")
    soft = banned_hits(m, False)
    print("残余不协和连击（低频自软代价）Top6:",
          [(k, w, "w*40=%d" % (w * 40)) for k, w in soft[:6]] or "无")
    print("常用连击上的不协和音程（应为无）:", rough_hits(m) or "无")
    sept = [((x, y), w) for x in m for y, w in ADJ[x].items()
            if y in m and x < y and isd(m[x], m[y]) == 10]
    print("   小七度跳进 Top5:", sorted(sept, key=lambda t: -t[1])[:5] or "无")
    _cost = total_cost(m)
    print("代价 %.0f 构成: 音型(区域/音级) %.0f + 不协和连击 %.0f + 三全音 %.0f"
          " + 小七度 %.0f"
          % (_cost, sum(shape_one(c, m[c]) for c in L.LETTERS),
             sum(w * 40 for _k, w in soft),
             sum(w * (3 if is_hard(*k) else 1) for k, w in tritone_hits(m)),
             sum(w * W_SEPT for _k, w in sept)))
    print("H5 落在 1-3-5 骨干音级的声母: %d/20；需求点名的最高频声母命中: %d/%d %s"
          % (sum(1 for c in INITIALS if m[c] % 12 in SPINE), len(SPINE_REQ), len(SPINE_REQ),
             "".join(c for c in sorted(SPINE_REQ) if m[c] % 12 in SPINE)))
    chroma = [c for c in INITIALS if m[c] % 12 not in NATURAL_RES]
    print("使用变化音的字母（期望仅出现在低频声母）:", chroma or "无")
    for ini, fin in P.SIGNATURE_COMBO:
        seq = ini + fin
        ivs = [P.ic(m[x], m[y]) for x, y in zip(seq, seq[1:])]
        print("   H9 招牌 %s+%s 键序 %s 音程级 %s %s" %
              (ini, fin, "-".join(seq), ivs,
               "OK" if all(v in P.CONSONANT_PREFER for v in ivs) else "BAD"))
    print("\n# ---- 粘贴到 keyboard_tune/notes.py ----")
    print("LETTER_PITCHES = {%s}" % ", ".join("%r: %d" % (c, m[c])
                                              for c in L.LETTERS))


def prove():
    """打印"全组合都协和不可行"的计数证据。"""
    print("== 过约束证明 ==")
    for n_oct in (2, 3, 4):
        pool = set(range(48, 48 + 12 * n_oct))
        for name, vres in (("五声(宫商角徵羽)", {0, 2, 4, 7, 9}),
                           ("自然七声", set(NATURAL_RES))):
            banned = set()
            for r in vres:
                banned |= {(r + 1) % 12, (r + 11) % 12}
            allowed = [n for n in pool if n % 12 not in banned]
            print("  %d 八度 / 韵母取%s: 声母可用音高 %d 个，需要 20 个 -> %s"
                  % (n_oct, name, len(allowed), "可行" if len(allowed) >= 20 else "无解"))
    nat = len([n for n in LETTER_POOL if n % 12 in NATURAL_RES])
    print("  另：音域 %s..%s 内自然音只有 %d 个，26 个字母互不重复至少需要 %d 个变化音"
          % (P.note_name(min(LETTER_POOL)), P.note_name(max(LETTER_POOL)),
             nat, max(0, 26 - nat)))
    print("  另：3 个八度内骨干音只有 %d 个音位，主音归 a 后只能支撑 %d 个高频声母，"
          "而需求点名了 21 个声母 -> 只能按频次取前 %d 个作硬优先"
          % (len(SPINE_SLOTS), len(SPINE_SLOTS) - 1, len(SPINE_REQ)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=120000)
    ap.add_argument("--iters", type=int, default=12)
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--prove", action="store_true")
    ap.add_argument("--soft", action="store_true", help="H5 改用软约束验证可行性")
    args = ap.parse_args()
    if args.prove:
        prove()
        print()

    # 默认先试严格口径（H5 全部满足），不行再退到软约束，并把降级原因打印出来
    modes = [False] if args.soft else [True, False]
    for strict in modes:
        tag = "严格 H5（高频声母全部占满骨干音位）" if strict else "软约束 H5"
        dom = build_domains(strict)
        dead = [c for c in dom if not dom[c]]
        if dead:
            print("%s: 定义域被硬约束清空 %s" % (tag, dead))
            continue
        sizes = sorted((len(dom[c]), c) for c in dom)
        print("\n== %s ==\n定义域（AC-3 后）最小的键: %s"
              % (tag, ", ".join("%s:%d" % (c, s) for s, c in sizes[:8])))
        champion, bound = None, float("inf")
        for it in range(args.iters):
            # 随机重启：固定序列的 DFS 在同一个区域里反复碰壁，扰开取值序才能
            # 探到不同的可行域；每轮都先拿首解再做局部精修，取历史最优
            rng = random.Random(args.seed + it * 7919)
            # 重启之间互没有上界约束（bound=inf）：历史最优靠 champion 保留，
            # 每次重启在 unrestricted 下先拿首解再分支定界，才能探到不同区域
            m, found, stat = search(dom, float("inf"), args.budget, rng,
                                    jit=0.0 if it == 0 else 4000.0)
            if m is None:
                print("  第%d轮: 节点 %d / 完整分配 %d —— 预算内未找到可行解"
                      % (it + 1, stat[0], stat[1]))
                break
            why = feasible(m, dom, strict)
            if why:
                print("  第%d轮: 重验失败 %s" % (it + 1, why))
                break
            m = polish(m, dom, it)
            cost = total_cost(m)
            print("  第%d轮: 搜索代价 %.0f → 精修后 %.0f（节点 %d%s"
                  % (it + 1, found, cost, stat[0],
                     "，刷新最优）" if cost < bound else "）"))
            if cost < bound:
                champion, bound = m, cost
        if champion:
            show(champion, "%s 最优方案 代价 %.0f" % (tag, bound))
            return 0
    print("\n无解：需要放宽硬约束口径")
    return 1


if __name__ == "__main__":
    sys.exit(main())
