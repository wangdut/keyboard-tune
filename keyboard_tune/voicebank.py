# -*- coding: utf-8 -*-
"""
预渲染采样库（需求 §四.1 方案 B）：音色引擎离线渲染的 WAV 片段落盘，运行时只读不放。

为什么要有这一层
  * 按键瞬间不做任何合成计算。实测单次 full 渲染（纯 Python，13k 样本 × 6 个振荡器
    + 4 梳状混响）约 23ms（fast 约 15ms）—— 已经贴着需求 §四.3 的 50ms 触发延迟上限，
    密集连击时更是一点余量都没有。读一个 26KB 的小文件是亚毫秒级，延迟就由音频后端的 buffer
    决定（pygame 256 帧 ≈ 5.8ms）。
  * 离线渲染可以毫无顾虑地用最高精度（精确 sin、完整模态列、4 梳状 + 2 全通混响），
    不受启动耗时约束。

目录结构（每个音色包一个"名字-指纹"目录，换包或改参数天然隔离）
  %APPDATA%\\KeyboardTune\\voicebank\\acoustic-1a2b3c4d\\
      manifest.json                指纹 + 采样清单（一条 JSON，启动只读一次）
      key/060_key_a_1.wav          按家族分子目录、文件名可读，方便用户自行替换
      warm/045_warm_c_0.wav

指纹校验失败的后果不是"报错"而是"当没有库"：调用方回退实时渲染，程序照常出声。
manifest 最后才写，因此中途被杀掉的半个库不会被当成有效库（没有 manifest 就是空库）。

manifest 里除了采样清单还存标定曲线（calibration / voice_gains），原因在 VoiceBank.open：
分区响量补偿是"渲染参数 + 参考音高集合"的导出量，而前者进指纹、后者不进。库里是离线
逐半音稠密标定的结果，启动时只能廉价地跑 3 个点（实测 0.45s，稠密 1.3s），两者 declared
指纹完全相同。不恢复就会让没命中的那几个键用廉价曲线渲染，响量与库里的邻居差几个
百分点 —— 同一张键盘上相邻键音量不一致，正是需求 §三 要避免的那种廉价感。
"""
import array
import json
import os
import tempfile
import wave
import zlib

from . import bootstrap as B
from . import voice as V

MANIFEST = "manifest.json"
CACHE_BUDGET = 40 << 20           # 常驻采样缓存上限（需求放宽到 100MB，这里留一半给别的）


def root_dir(root=None):
    """采样库根目录（所有音色包都放在这下面）。"""
    return root or os.path.join(B.data_dir(), "voicebank")


def bank_dir(pack, root=None):
    """某个音色包的目录：名字 + 参数指纹，指纹变了指纹目录就变。"""
    return os.path.join(root_dir(root), "%s-%s" % (pack.name, pack.fingerprint()))


def rel_name(midi, timbre, variant):
    """
    采样文件名：可读且与 VoicePack.key_for 一一对应，用户想自己换一段 WAV 进去
    （需求 §六.1"方便后续替换音色"）也能靠文件名找到对应的键。
    """
    fam = timbre[:-1]
    return "%s/%03d_%s_%s_%d.wav" % (fam, int(midi), fam, timbre[-1:],
                                      int(variant))


def wav_dump(path, pcm, n):
    """int16 小端 PCM -> 标准 44 字节头 WAV（原子写：临时文件 + replace）。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wt", suffix=".wav", dir=folder or ".")
    try:
        with os.fdopen(fd, "wb") as raw:
            with wave.open(raw, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(V.SAMPLE_RATE)
                wf.writeframes(pcm)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return os.path.getsize(path)


class VoiceBank(object):
    """
    读侧：按 (midi, timbre, variant) 取 int16 采样，命中不了就返回 None。

    线程模型：监听线程只调 frames()，它只做 dict 查找 + 必要时一次文件读；写侧
    （build）只在离线脚本里跑，因此这里不加锁，也不允许在读取路径上做任何渲染。
    """

    def __init__(self, pack, root=None, log=None, cache_budget=CACHE_BUDGET):
        self.pack = pack
        self.log = log or (lambda msg: None)
        self.dir = bank_dir(pack, root)
        self.manifest_path = os.path.join(self.dir, MANIFEST)
        self._entries = {}          # key -> 相对文件名
        self._cache = {}            # key -> array('h')
        self._bytes = 0
        self._budget = max(0, int(cache_budget))
        self.available = False
        self.fingerprint = pack.fingerprint()

    # ------------------------------------------------------------ 打开
    def open(self):
        """
        读 manifest 并校验指纹/规格，然后把库里存的标定曲线恢复到 self.pack 上。

        任何不匹配都不是错误：available=False，调用方据此回退实时渲染（需求 §五.6
        的"异常处理"里最要紧的一条）。恢复曲线只在指纹与规格都通过之后做：指纹不
        通过时库里那些响量补偿对本次运行毫无意义，把脏曲线留在包上会让回退渲染比
        不恢复更难听。

        注意本方法会就地修改传入的 pack（family_trim / family_voice）。这是有意的：
        audio.py 拿同一个 pack 做缺失回退渲染，曲线必须与库里采样的响量同源。
        """
        self._entries = {}
        self.available = False
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except (OSError, ValueError) as exc:
            self.log("采样库不可用（%r），改用实时渲染" % (exc,))
            return False
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
            self.log("采样库 manifest 结构异常，改用实时渲染")
            return False
        if raw.get("fingerprint") != self.fingerprint:
            self.log("采样库指纹不符（库=%s 当前=%s），改用实时渲染"
                     % (raw.get("fingerprint"), self.fingerprint))
            return False
        if (int(raw.get("sample_rate", 0)) != V.SAMPLE_RATE
                or int(raw.get("bits", 0)) != 16
                or int(raw.get("channels", 0)) != 1):
            self.log("采样库规格不符（需求 44100Hz/16bit/单声道），改用实时渲染")
            return False
        self._entries = dict(raw["entries"])
        restore_calibration(self.pack, raw, self.log)
        self.available = True
        self.log("采样库就绪：%d 条（%s）" % (len(self._entries), self.dir))
        return True

    def has(self, midi, timbre, variant):
        return self.pack.key_for(midi, timbre, variant) in self._entries

    def count(self):
        return len(self._entries)

    def memory_bytes(self):
        return self._bytes

    # ------------------------------------------------------------ 取采样
    def frames(self, midi, timbre, variant):
        """-> array('h') 或 None（None 表示调用方该实时渲染这一条）。"""
        key = self.pack.key_for(midi, timbre, variant)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        rel = self._entries.get(key)
        if rel is None or not self.available:
            return None
        arr = self._read(os.path.join(self.dir, rel), key)
        if arr is None:
            # 文件丢了/坏了：摘掉这一条，本次与以后都走实时渲染，不再反复试
            self._entries.pop(key, None)
            return None
        used = arr.itemsize * len(arr)          # itemsize 是属性不是方法
        if self._bytes + used <= self._budget:
            self._cache[key] = arr
            self._bytes += used
        return arr

    def _read(self, path, key):
        try:
            with wave.open(path, "rb") as wf:
                if (wf.getframerate() != V.SAMPLE_RATE or wf.getsampwidth() != 2
                        or wf.getnchannels() != 1):
                    self.log("采样 %s 规格不符，忽略" % key)
                    return None
                want = wf.getnframes()
                data = wf.readframes(want)
        except (OSError, ValueError, EOFError) as exc:
            self.log("采样 %s 读取失败: %r" % (key, exc))
            return None
        # 必须是"声明多少帧就读到多少帧"。wave 对截断文件不抛错，只给半截数据，
        # 直接送进后端就是一次尾音被硬切掉的爆音（需求 §一.2 明确禁止戛然而止），
        # 所以长度不符一律视为坏文件，摘掉条目改走实时渲染。
        if len(data) != 2 * want:
            self.log("采样 %s 数据不完整（%d/%d 字节），忽略" % (key, len(data), 2 * want))
            return None
        n = len(data) // 2
        arr = array.array("h")
        arr.frombytes(data)
        if sys_big_endian():
            arr.byteswap()
        return arr

    def close(self):
        self._cache.clear()
        self._bytes = 0
        self._entries.clear()
        self.available = False


def sys_big_endian():
    import sys
    return sys.byteorder != "little"


# ------------------------------------------------------------------ 标定曲线进出
def calibration_payload(pack):
    """把包里的导出量整理成可 JSON 化的形态（tuple 不行，JSON 只有 list）。"""
    return {
        "calibration": dict((fam, [[int(m), float(v)] for m, v in pts])
                            for fam, pts in pack.family_trim.items()),
        # 分区标量增益：只当信息留存，因为 family_voice 本身就在指纹 payload 里，
        # 增益不同的两个包根本对不上指纹，不需要（也不能）靠这里覆盖。
        "voice_gains": dict((fam, [bar, float(gain)])
                            for fam, (bar, gain) in pack.family_voice.items()),
    }


def restore_calibration(pack, raw, log=None):
    """
    把 manifest 里的标定曲线装回 pack（就地）。返回是否真的装上了。

    为什么需要这一步：分区响量补偿不进指纹（同一套渲染参数用不同的参考音高集合标定，
    曲线不同而 declared 指纹相同），所以一个只用 3 个点廉价标定的运行时包，可以直接使用
    离线逐半音稠密标定建出来的库。前提是回退渲染也得用同一条曲线，否则库里 0.875 的键
    和实时渲染 0.83 的键会在同一句打字里交替出现 —— 需求 §三 要的分区比就是白做。

    表缺失或内容畸形都不算错误：留着 pack 自己标定出的曲线照常出声。
    """
    table = raw.get("calibration")
    if not isinstance(table, dict):
        return False
    curve = {}
    for fam, pts in table.items():
        # 只认当前包存在的家族：换包会换指纹，这里出现陌生家族只能说明库脏了
        if fam not in pack.family_voice or not isinstance(pts, list):
            continue
        fixed = []
        for pt in pts:
            try:
                fixed.append((int(pt[0]), float(pt[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if fixed:
            curve[fam] = tuple(sorted(fixed))
    if not curve:
        return False
    pack.family_trim = curve
    pack._calibrated = True
    if log is not None:
        log("已恢复采样库的标定曲线（%d 个家族）" % len(curve))
    return True


# ------------------------------------------------------------------ 写侧（离线构建）
def build(pack, slots, quality="full", root=None, log=None, progress=None,
          variants=None, rebuild=False, trim_tol=1e-6):
    """
    渲染并落盘整个采样库。返回 {"written": n, "kept": n, "bytes": n, "skipped": n}。

      slots     可迭代的 (midi, timbre)
      quality   "full" 用于发行库（最高精度）；"fast" 只用于自检
      rebuild   True 时忽略已有条目全部重渲染
      trim_tol  留用旧采样允许的响量补偿绝对偏差（补偿是倍率，量级 0.8~1.2），理由见下面
                的 stale。默认只容浮点噪声：阈值想宽一点并不能省多少时间（稠密曲线本来
                就只在建库时跑），但留 0.4% 的差异就足以让运行时同参数渲染与库里字节不同。
    manifest 只在最后写：中途崩掉留下的是"没有清单的 WAV 堆"，读侧一律视为空库。
    """
    counts = {"written": 0, "kept": 0, "bytes": 0, "skipped": 0, "retrim": 0}
    mine = dict(pack.family_trim)         # 本次的权威曲线（调用方刚标定出来的）
    bank = VoiceBank(pack, root=root, log=log)
    stored = {}
    if not rebuild:
        bank.open()                        # 有可用库就只补缺（顺带把库里曲线装进 pack）
        stored = dict(pack.family_trim)
    # open() 会就地改 pack，这里必须拿回来：否则只补缺时，本次新标定的曲线会被库里旧
    # 曲线顶掉，新渲染的几条用新曲线、留用的几百条是旧曲线，同一键区响量不一致。
    pack.family_trim = mine
    entries = {} if rebuild else dict(bank._entries)
    nv = variants or pack.variants
    todo = []
    for midi, timbre in slots:
        fam = timbre[:-1]
        # stale：库里那条曲线与本次这条对这个键的补偿不一样。标定密度变了而 declared
        # 指纹不变（补偿不进指纹，见 VoicePack.fingerprint）就会碰到这一情况：不重渲染
        # 就是交出一个"库里某几个键的响量与新渲染对不上"的发行库。
        stale = bool(stored) and abs(V.trim_at(stored, fam, midi)
                                     - V.trim_at(mine, fam, midi)) > trim_tol
        for var in range(nv):
            key = pack.key_for(midi, timbre, var)
            rel = entries.get(key)
            on_disk = rel is not None and os.path.exists(os.path.join(bank.dir, rel))
            if on_disk and not stale:
                counts["kept"] += 1
                continue
            if on_disk:
                counts["retrim"] += 1      # 文件在但曲线变了，推倒重渲
            todo.append((midi, timbre, var, key))
    total = len(todo)
    for i, (midi, timbre, var, key) in enumerate(todo):
        try:
            pcm, n = pack.render(midi, timbre, var, quality)
            rel = rel_name(midi, timbre, var)
            size = wav_dump(os.path.join(bank.dir, rel), pcm, n)
        except OSError as exc:
            log and log("写采样失败 %s: %r" % (key, exc))
            counts["skipped"] += 1
            continue
        except Exception as exc:            # 单条渲染异常不该带走整轮构建
            log and log("渲染失败 %s: %r" % (key, exc))
            counts["skipped"] += 1
            continue
        entries[key] = rel
        counts["written"] += 1
        counts["bytes"] += size
        if progress is not None:
            progress(i + 1, total, midi, timbre, var)
    if not entries:
        log and log("没有任何采样可写，采样库保持不可用")
        return counts
    payload = {
        "voice": pack.name,
        "fingerprint": pack.fingerprint(),
        "sample_rate": V.SAMPLE_RATE,
        "bits": 16,
        "channels": 1,
        "quality": quality,
        "variants": nv,
        "entries": entries,
    }
    payload.update(calibration_payload(pack))
    _write_json(bank.manifest_path, payload)
    counts["total"] = len(entries)
    return counts


def _write_json(path, payload):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mf", suffix=".json", dir=folder or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def expected_keys(pack, slots, variants=None):
    """指纹校验用：当前参数下应该有哪些采样。"""
    nv = variants or pack.variants
    return [pack.key_for(midi, timbre, v)
            for midi, timbre in slots for v in range(nv)]


def missing(pack, slots, root=None, variants=None):
    """列出"参数指纹变了/文件丢了"的条目，供自检与构建脚本报告。"""
    bank = VoiceBank(pack, root=root)
    have = bank.open()
    want = expected_keys(pack, slots, variants)
    if not have:
        return want
    return [k for k in want if k not in bank._entries
            or not os.path.exists(os.path.join(bank.dir, bank._entries[k]))]


def crc32(text):
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF
