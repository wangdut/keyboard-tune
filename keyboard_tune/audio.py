# -*- coding: utf-8 -*-
"""
音效播放管理：按键 -> (音高, 音色, humanize 变体) -> 采样 -> 后端出声。

采样从哪来（需求 §四.1 方案 B）
  * 首选磁盘采样库 voicebank：离线按最高精度渲染好、44.1kHz/16bit 落盘的 WAV 片段，
    按键瞬间只有一次 dict 查找 + 必要时一次小文件读，不做任何合成计算。
  * 库里没有这一条（还没建库/文件丢了/参数刚改过）就用 voice.VoicePack 现场补渲一帧
    "fast" 质量并缓存，之后不再重算 —— 程序永远能出声，只是首按多 ~20ms。
  * 旧的"基础波形实时合成"（正弦表 + 线性包络 + 整数倍分音）已整体删除：合成器味的
    电子音正是需求 §一.2 禁止的东西，而"实时合成"正是 §四.1 要求从根源上摒弃的路线。
    音色逻辑现在只存在于 voice.py 一处（§六.1）。

其余设计不变
  * 预渲染 + 内存采样：延迟由音频后端的 buffer 决定（pygame.mixer 以 buffer=256 帧
    初始化，约 5.8ms），远低于 50ms 要求。
  * 复音与防爆音：后端固定 24 条通道，通道占满时抢用最旧的通道（打断而不是排队），
    从根上避免"音效堆积"；同时用按下/释放状态抑制 Windows 的自动重复。
  * 后端回退链：pygame.mixer（首选，真复音，走通道音量）→ winsound（系统自带，
    单通道抢音，音量只能算进采样）。simpleaudio 未实现：它的 WaveObject 只接受文件
    路径，而本工程的采样库虽然落盘、读回来却是内存里交给后端，换它没有收益。
  * humanize：同一槽位有 K 个变体采样（音分/响量在需求限额内偏移），按键时轮转取一个。
"""
import array
import io
import os
import threading
import time
import wave

from . import notes as N
from . import layout as L
from . import voice as V
from . import voicebank as VB

# 采样率单一来源于 voice：两处不一致时采样的音高与时长会一起变，但不是报错而是怪声
SAMPLE_RATE = V.SAMPLE_RATE
MAX_VOICES = 24


def wav_bytes(pcm):
    """
    16bit 单声道 PCM -> WAV 容器字节（pygame 与 winsound 都吃这个）。
    pcm 可以是 array('h')（采样库读出来的）或 int16 小端 bytes（现场渲染给的）。
    """
    raw = pcm if isinstance(pcm, (bytes, bytearray)) else pcm.tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    return buf.getvalue()


def _pcm_bytes(pcm):
    """常驻字节数：array('h') 每个采样 2 字节，bytes 本身就是字节数。"""
    return len(pcm) if isinstance(pcm, (bytes, bytearray)) else 2 * len(pcm)


# ------------------------------------------------------------------ 后端
class BackendBase(object):
    name = "base"
    polyphonic = False

    def open(self):
        raise NotImplementedError

    def has(self, key):
        """后端是否已经持有这条采样（持有则调用方不必再构造 WAV 字节）。"""
        return False

    def play(self, index, data, volume):
        raise NotImplementedError

    def set_volume(self, volume):
        pass

    def close(self):
        pass


class PygameBackend(BackendBase):
    """真复音：24 条通道，占满则抢用最旧通道（打断而非排队）。"""

    name = "pygame"
    polyphonic = True

    def __init__(self):
        self._mixer = None
        self._sounds = {}
        self._volume = 1.0
        self._rr = 0

    def open(self):
        # 必须在 import pygame 之前设置：否则它会往 stdout 打社区横幅，
        # 对号称"完全静默"的常驻程序是多余输出（--debug 终端里尤其明显）。
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame
        pygame.mixer.pre_init(SAMPLE_RATE, -16, 1, 256)   # 小 buffer = 低延迟
        pygame.mixer.init(SAMPLE_RATE, -16, 1, 256)
        pygame.mixer.set_num_channels(MAX_VOICES)
        self._mixer = pygame.mixer
        self._Sound = pygame.mixer.Sound

    def has(self, key):
        return key in self._sounds

    def play(self, index, data, volume):
        snd = self._sounds.get(index)
        if snd is None:
            if data is None:                 # 调用方以为有、其实没有：不猜，直接丢这一声
                return None
            snd = self._sounds[index] = self._Sound(buffer=data)
        ch = self._mixer.find_channel(True)               # 无空闲通道则抢用最旧的
        ch.set_volume(volume)
        ch.play(snd)
        return ch

    def set_volume(self, volume):
        self._volume = volume

    def close(self):
        try:
            if self._mixer is not None:
                self._mixer.quit()
        except Exception:
            pass
        self._sounds.clear()


class WinSoundBackend(BackendBase):
    """系统自带兜底：PlaySoundW(SND_ASYNC) 单通道，新音直接取代旧音。"""

    name = "winsound"
    polyphonic = False

    SND_ASYNC = 0x0001
    SND_NODEFAULT = 0x0002
    SND_MEMORY = 0x0004

    def open(self):
        import winsound
        self._winsound = winsound

    def play(self, index, data, volume):
        # winsound 无通道音量接口，音量已在采样数据里预先套好（见 _wav）
        self._winsound.PlaySound(data, self.SND_ASYNC | self.SND_NODEFAULT
                                 | self.SND_MEMORY)
        return None

    def close(self):
        try:
            self._winsound.PlaySound(None, self.SND_ASYNC | self.SND_NODEFAULT)
        except Exception:
            pass


def _to_array(pcm):
    """int16 小端 bytes -> array('h')；已经是 array 就原样返回。"""
    if not isinstance(pcm, (bytes, bytearray)):
        return pcm
    a = array.array("h")
    a.frombytes(bytes(pcm))
    return a


def _gain_cache(pcm, gain):
    """
    给 winsound 兜底路径套用主音量（整段重算一次，按档位缓存结果）。
    真复音后端不走这里（它用通道音量），所以不会为每次音量改动重算采样。
    """
    src = _to_array(pcm)
    if gain >= 0.999:
        return src
    n = len(src)
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        w = int(src[i] * gain)
        out[i] = -32767 if w < -32767 else (32767 if w > 32767 else w)
    return out


class SoundEngine(object):
    """
    按键 -> 声音 的门面：持有槽位表、采样缓存、音色包/采样库与后端实例。

    采样库（VoiceBank）与音色包（VoicePack）的关系：库里存的是离线 full 精度渲染的
    采样 + 逐音高稠密标定的分区曲线；open() 会把那条曲线恢复到本包上，因此库里没有的
    那几条现场补渲出来也不会与邻居响量不同源。所以 bank.open() 必须在预热之前。
    """

    # Windows 自动重复抑制：同一个键在"按下未释放"期间不重复发音；
    # 超过该间隔仍收到"已按下"的键，视为释放事件丢失后的自愈重触发。
    STALE_HOLD = 1.0

    def __init__(self, config=None, log=None, pack=None, bank=None, root=None):
        self.config = config
        self.log = log or (lambda msg: None)
        self.backend = None
        self.backend_name = "none"
        self._lock = threading.Lock()
        self._stems = {}          # (槽位, 变体) -> 满刻度 PCM（array 或 bytes）
        self._data = {}           # (槽位, 变体) -> (音量档位, 已套音量的 WAV 字节)
        self._pressed = {}        # vk -> 最近一次按下时间
        self._rr = {}             # 槽位 -> 上一次用掉的 humanize 变体
        self._index = {}          # (midi, timbre) -> int
        self._ready = False
        self._volume = 0.7
        self._polyphonic = False
        self._errors = 0
        self._live = 0            # 回退到实时渲染的条数（自检与 --debug 日志）

        slots = N.all_slots()
        for i, slot in enumerate(slots):
            self._index[slot] = i
        self._slot_list = slots

        # 音色包由配置驱动（§六.2/§六.3），标定参考点取真实映射表的音高：
        # 每个家族只用自己会发的那些音高做参考（理由见 voice.calibrate）
        if pack is None:
            pack = (V.pack_from_config(config, slots=slots)
                    if config is not None else V.make_pack(slots=slots))
        self.pack = pack
        self._nv = max(1, int(pack.variants))
        if bank is None:
            # 播放层自己按 (槽位,变体) 缓存且不会丢弃，库再缓存一份就是纯重复占内存，
            # 所以运行时给 0 预算（库自己的默认预算是给单独使用与建库自检的）
            bank = VB.VoiceBank(pack, root=root, log=self.log, cache_budget=0)
        self.bank = bank

    @property
    def slot_count(self):
        """待预加载的 (音高, 音色) 槽位总数。"""
        return len(self._slot_list)

    @property
    def voice_name(self):
        return self.pack.name

    @property
    def bank_ready(self):
        return bool(self.bank is not None and self.bank.available)

    # ------------------------------------------------------------ 初始化
    def open(self, prefer=None):
        self._volume = self.config.volume if self.config is not None else 0.7
        order = ("pygame", "winsound")
        if prefer and prefer in order:
            order = (prefer,) + tuple(x for x in order if x != prefer)
        for name in order:
            try:
                be = PygameBackend() if name == "pygame" else WinSoundBackend()
                be.open()
            except Exception as exc:                 # 依赖缺失/声卡不可用
                self.log("音效后端 %s 不可用: %r" % (name, exc))
                continue
            self.backend = be
            self.backend_name = be.name
            self._polyphonic = be.polyphonic
            break
        if self.backend is None:
            self.log("所有音效后端都不可用，程序继续静默运行")
            return False
        # 采样库先打开：它会把库里的标定曲线恢复到 pack 上，预热与回退渲染都依赖它
        if self.bank is not None:
            try:
                self.bank.open()
            except Exception as exc:                 # 库层出错不影响"能出声"
                self.log("采样库打开异常: %r" % (exc,))
                self.bank.available = False
        self._ready = True
        # 字母键槽位先加载（打字主力），其余在后台线程补齐，启动不卡
        threading.Thread(target=self._warm, daemon=True).start()
        return True

    def _warm(self):
        try:
            letter = {N.slot_of(ord(c.upper())) for c in L.LETTERS}
            self._preload(i for i, slot in enumerate(self._slot_list)
                          if slot in letter)
            self._preload(range(len(self._slot_list)))
            if self._live:
                self.log("其中 %d 条采样库未覆盖，已实时补渲并缓存" % self._live)
        except Exception as exc:
            self.log("采样预热异常: %r" % (exc,))

    def _preload(self, idxs):
        for idx in idxs:
            for var in range(self._nv):
                if not self._ready:
                    return
                self._stem(idx, var)

    # ------------------------------------------------------------ 采样来源
    def _next_variant(self, idx):
        """
        轮转取一个 humanize 变体。不用 random：轮转保证 K 次连续击键一定把 K 个
        变体各用一次（随机会让"同一个键听起来一样"的概率远高于需求想避开的那种机械感）；
        首次访问按槽位错开起点，避免整片键盘同时切到同一变体。
        """
        if self._nv <= 1:
            return 0
        v = self._rr.get(idx)
        v = (idx * 7 + 3) % self._nv if v is None else (v + 1) % self._nv
        self._rr[idx] = v
        return v

    def _stem(self, idx, var):
        """
        取一条满刻度采样：采样库优先，没命中就现场补渲一帧并缓存（之后不再重算）。

        补渲用 "fast"：那是运行时兜底路径，预算是需求 §四.3 的 50ms；离线建库才用
        full。听感差异已在验收里量化（响量 <0.6dB、起音 0ms、尾音 <25ms）。
        """
        key = (idx, var)
        with self._lock:
            hit = self._stems.get(key)
        if hit is not None:
            return hit
        midi, timbre = self._slot_list[idx]
        pcm = None
        if self.bank is not None and self.bank.available:
            pcm = self.bank.frames(midi, timbre, var)   # 命中不了就是 None，不抛
        if pcm is None:
            pcm, _n = self.pack.render(midi, timbre, var, "fast")
            self._live += 1
        with self._lock:
            self._stems[key] = pcm
            self._data.pop(key, None)
        return pcm

    def _wav(self, idx, var):
        """
        取该 (槽位,变体) 可直接交给后端的 WAV 字节。

        真复音后端走通道音量，采样保持满刻度，而且**不在这里缓存**：后端自己按 key
        缓存 Sound，这里再存一份 bytes 就是 426 条 × 26KB 的纯重复内存。它只在后端
        还没这条采样时被调用一次。
        winsound 只能靠重算采样，拿 5% 为一档缓存，拖滑条时不会每一点都重算。
        """
        if self._polyphonic:
            return wav_bytes(self._stem(idx, var))
        key = (idx, var)
        step = max(1, int(round(self._volume * 20)))
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and hit[0] == step:
                return hit[1]
        data = wav_bytes(_gain_cache(self._stem(idx, var), step / 20.0))
        with self._lock:
            self._data[key] = (step, data)
        return data

    # ------------------------------------------------------------ 播放
    def play_vk(self, vk):
        """按键瞬间调用；必须在监听线程里做尽量少的计算。"""
        if not self._ready or self._volume <= 0.0:
            return
        slot = N.slot_of(vk)
        idx = self._index.get(slot)
        if idx is None:
            return
        now = time.monotonic()
        last = self._pressed.get(vk)
        if last is not None and now - last < self.STALE_HOLD:
            return                       # Windows 自动重复：不刷音
        self._pressed[vk] = now
        var = self._next_variant(idx)
        key = (idx, var)
        try:
            # 后端已经持有这条采样就不要构造 WAV 字节了：那 26KB 只是白白多存一份
            data = None if self.backend.has(key) else self._wav(idx, var)
            self.backend.play(key, data, self._volume)
        except Exception as exc:
            self._errors += 1
            if self._errors <= 3 or self._errors % 200 == 0:
                self.log("播放失败(%d): %r" % (self._errors, exc))

    def release_vk(self, vk):
        """释放事件只用于状态跟踪（不出声），避免长按刷音并保留自愈能力。"""
        if vk in self._pressed:
            self._pressed[vk] = None
            del self._pressed[vk]

    def set_volume(self, volume):
        self._volume = max(0.0, min(1.0, volume))
        try:
            self.backend.set_volume(self._volume)
        except Exception:
            pass
        if not self._polyphonic:
            # winsound 路径：音量大改后丢弃已套音量的缓存，下次按键懒重算
            with self._lock:
                self._data.clear()

    def close(self):
        self._ready = False
        try:
            if self.backend is not None:
                self.backend.close()
        except Exception as exc:
            self.log("关闭音频后端异常: %r" % (exc,))
        if self.bank is not None:
            try:
                self.bank.close()
            except Exception:
                pass
        with self._lock:
            self._stems.clear()
            self._data.clear()
            self._rr.clear()

    def memory_bytes(self):
        """当前常驻内存的采样字节数（用于自检与日志）。"""
        with self._lock:
            total = sum(_pcm_bytes(p) for p in self._stems.values())
            total += sum(len(v[1]) for v in self._data.values())
        return total
