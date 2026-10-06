# -*- coding: utf-8 -*-
"""
音效管理模块：把 (音高, 音色) 槽位合成为极短的 PCM 采样并在按键瞬间播放。

设计要点
  * 预渲染 + 内存采样：按键时不做任何合成计算，只查表播放，延迟由音频后端的
    buffer 决定（pygame.mixer 以 buffer=256 帧初始化，约 5.8ms），远低于 50ms 要求。
  * 纯标准库合成（array + wave + math 正弦表），不引入 numpy —— 常驻内存
    只有采样本身（71 个槽位实测合计 1.42MB），满足内存 ≤50MB 的要求。
  * 复音与防爆音：后端固定 24 条通道，通道占满时抢用最旧的通道（打断而不是排队），
    从根上避免"音效堆积"；同时用按下/释放状态抑制 Windows 的自动重复。
  * 后端回退链：pygame.mixer（首选，真复音）→ winsound（系统自带，单通道抢音）。
    simpleaudio 未实现：它的 WaveObject 只接受文件路径，本工程所有采样都在内存中，
    落盘换后端不划算；如需可在 BackendBase 之上新增一个实现。
  * 音量：全局主音量 0..1，调整立即生效（pygame 走通道音量，winsound 走重放增益）
    并由 config 模块持久化。
"""
import array
import io
import math
import os
import struct
import threading
import time
import wave

from . import notes as N
from . import layout as L

SAMPLE_RATE = 44100
MAX_VOICES = 24
SINE_N = 4096                  # 正弦表长度（线性插值查表，避免逐点 math.sin）
SINE = [math.sin(2 * math.pi * i / SINE_N) for i in range(SINE_N)]
MASK = SINE_N - 1

# 家族嗓音参数：分音配比 / 衰减时长 / 响度补偿 / 泛音拉伸（拨弦感的非谐性）
FAMILY_PARAMS = {
    "key": ((1.0, 0.42, 0.20, 0.08), 0.30, 1.00, 0.00060),
    "tick": ((1.0, 0.26, 0.10), 0.11, 0.72, 0.0),
    "soft": ((1.0, 0.16, 0.06), 0.20, 0.80, 0.0),
    "warm": ((1.0, 0.34, 0.14, 0.04), 0.17, 0.92, 0.00025),
}
# 变体：a 为家族基准，b 做"高次分音衰减更快 + 基频极轻失谐"的微调，
# 使相邻按键听感有细微差别但同属一种音色（不破坏"同音区音色统一"）。
VARIANT_TWEAK = {"a": (1.0, 0.0), "b": (0.62, 0.35)}


def midi_to_freq(midi):
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def _envelope(n, attack_s, decay_s):
    """线性起音 + 指数衰减 + 收尾淡出（去爆音 click）。"""
    env = array.array("f", bytes(4 * n))
    a = max(1, int(attack_s * SAMPLE_RATE))
    d = n - a
    drop = math.exp(math.log(1e-3) / max(1, d))       # 衰减到 -60dB
    v = 1.0
    rel = max(1, int(0.006 * SAMPLE_RATE))
    for i in range(a):
        env[i] = i / a
    for i in range(d):
        env[a + i] = v
        v *= drop
    tail = min(n, rel)
    for i in range(tail):
        env[n - tail + i] *= 1.0 - i / tail
    return env


def render_samples(midi, timbre):
    """合成一个槽位的 16bit 单声道 PCM。"""
    family = timbre[:-1]
    variant = timbre[-1]
    parts, decay, gain, stretch = FAMILY_PARAMS.get(family, FAMILY_PARAMS["key"])
    harm, detune = VARIANT_TWEAK.get(variant, VARIANT_TWEAK["a"])
    base = midi_to_freq(midi)
    n = int(decay * SAMPLE_RATE)
    env = _envelope(n, 0.004, decay)
    acc = array.array("f", bytes(4 * n))
    for h, amp in enumerate(parts, start=1):
        if amp <= 0:
            continue
        # 非谐性拉伸：让高次泛音略高于整数倍，得到"打击/拨弦"的实体感
        f = base * h * (1.0 + stretch * h * h)
        if f >= SAMPLE_RATE * 0.45:
            break
        step = f * SINE_N / SAMPLE_RATE       # 每个采样点在正弦表上前移多少格
        phase = (detune * 137.0 * h) % SINE_N
        a_loc = amp * (harm if h > 1 else 1.0)
        for i in range(n):
            idx = int(phase) & MASK
            frac = phase - int(phase)
            acc[i] += a_loc * (SINE[idx] * (1.0 - frac) + SINE[(idx + 1) & MASK] * frac)
            phase += step
    peak = 0.0
    for i in range(n):
        v = acc[i] * env[i]
        acc[i] = v
        av = -v if v < 0 else v
        if av > peak:
            peak = av
    if peak <= 0:
        return array.array("h", bytes(2 * n))
    scale = (0.82 * 32767.0 / peak) * gain * 0.5
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        v = int(acc[i] * scale)
        out[i] = -32767 if v < -32767 else (32767 if v > 32767 else v)
    return out


def wav_bytes(pcm):
    """16bit 单声道 PCM -> WAV 容器字节（pygame 与 winsound 都吃这个）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# ------------------------------------------------------------------ 后端
class BackendBase(object):
    name = "base"
    polyphonic = False

    def open(self):
        raise NotImplementedError

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

    def play(self, index, data, volume):
        snd = self._sounds.get(index)
        if snd is None:
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


def _gain_cache(pcm, gain):
    """给 winsound 兜底路径套用主音量（整段重算一次，按档位缓存结果）。"""
    if gain >= 0.999:
        return pcm
    n = len(pcm)
    raw = struct.unpack("<%dh" % n, pcm)
    out = array.array("h", bytes(2 * n))
    for i, v in enumerate(raw):
        w = int(v * gain)
        out[i] = -32767 if w < -32767 else (32767 if w > 32767 else w)
    return out


class SoundEngine(object):
    """按键 -> 声音 的门面：持有槽位表、渲染缓存与后端实例。"""

    # Windows 自动重复抑制：同一个键在"按下未释放"期间不重复发音；
    # 超过该间隔仍收到"已按下"的键，视为释放事件丢失后的自愈重触发。
    STALE_HOLD = 1.0

    def __init__(self, config=None, log=None):
        self.config = config
        self.log = log or (lambda msg: None)
        self.backend = None
        self.backend_name = "none"
        self._lock = threading.Lock()
        self._stems = {}          # index -> 满刻度 PCM
        self._data = {}           # index -> (音量档位, 已套音量的 WAV 字节)
        self._pressed = {}        # vk -> 最近一次按下时间
        self._index = {}          # (midi, timbre) -> int
        self._ready = False
        self._volume = 0.7
        self._polyphonic = False
        self._errors = 0

        slots = N.all_slots()
        for i, slot in enumerate(slots):
            self._index[slot] = i
        self._slot_list = slots

    @property
    def slot_count(self):
        """待预渲染的 (音高, 音色) 槽位总数。"""
        return len(self._slot_list)

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
        self._ready = True
        # 字母键槽位先渲染（打字主力），其余在后台线程补齐，启动不卡
        threading.Thread(target=self._warm, daemon=True).start()
        return True

    def _warm(self):
        try:
            letter = {N.slot_of(ord(c.upper())) for c in L.LETTERS}
            for idx, slot in enumerate(self._slot_list):
                if slot in letter:
                    self._stem(idx)
            for idx in range(len(self._slot_list)):
                self._stem(idx)
        except Exception as exc:
            self.log("采样预渲染异常: %r" % (exc,))

    def _stem(self, idx):
        with self._lock:
            if idx in self._stems:
                return self._stems[idx]
        pcm = render_samples(*self._slot_list[idx])
        with self._lock:
            self._stems[idx] = pcm
            self._data.pop(idx, None)
        return pcm

    def _wav(self, idx):
        """
        取该槽位可直接交给后端的 WAV 字节。
        真复音后端用通道音量，采样保持满刻度（档位 20）；winsound 只能靠重算采样，
        按 5% 为一档缓存，拖滑条时不会每一点都重算。
        """
        step = 20 if self._polyphonic else max(1, int(round(self._volume * 20)))
        with self._lock:
            hit = self._data.get(idx)
            if hit is not None and hit[0] == step:
                return hit[1]
        data = wav_bytes(_gain_cache(self._stem(idx), step / 20.0))
        with self._lock:
            self._data[idx] = (step, data)
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
        try:
            self.backend.play(idx, self._wav(idx), self._volume)
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
        with self._lock:
            self._stems.clear()
            self._data.clear()

    def memory_bytes(self):
        """当前常驻内存的采样字节数（用于自检与日志）。"""
        with self._lock:
            total = sum(len(p) * 2 for p in self._stems.values())
            total += sum(len(v[1]) for v in self._data.values())
        return total
