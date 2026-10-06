# -*- coding: utf-8 -*-
"""
键盘监听模块：pynput 全局低级钩子 -> 物理虚拟键码(VK) -> 音效引擎。

关键设计
  * 只监听不拦截：回调一律返回 None。pynput 规定 on_press 返回 True 才继续传递，
    返回 False 会停止监听，因此这里绝不返回布尔值，键盘原有功能 100% 保留。
  * 以 VK（虚拟键码）为按键身份：VK 是"物理位置"的编码，与键盘配列无关。
    87/68/61 键只是少了若干 VK，共有按键的 VK 完全一致，因此映射天然向下兼容；
    程序不会因为遇到没见过的键而报错（未知 VK 落到 notes.FALLBACK_SLOT）。
  * 自我 healing：钩子线程可能因异常退出，看门狗线程定期检测并重建监听器，
    避免"程序在跑但没声音"这种最难排查的失效。
  * 回调里只做一次字典查表和一次后端调用，不做任何合成/IO，保证 ≤50ms 延迟。

已知 Windows 事实（影响映射，如实记录）
  * 主 Enter 与小键盘 Enter 的 VK 都是 0x0D，低级钩子不给扩展标志，无法区分；
    小键盘 Enter 因此使用主 Enter 的音效。
  * NumLock 关闭时小键盘数字键会上报方向键/首页等 VK，此时发的是编辑区音效。
"""
import threading
import time

from . import layout as L

try:
    from pynput.keyboard import Key, Listener
except Exception as exc:                              # pragma: no cover
    Key = None
    Listener = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

# 左修饰键：Windows 实际会上报 VK_LSHIFT/VK_LCONTROL/VK_LMENU（0xA0/0xA2/0xA4），
# 而本项目的基准表用通用码 0x10/0x11/0x12 表示"左/主"修饰键，这里做一次归一。
# 右修饰键上报的就是 0xA1/0xA3/0xA5，与基准表一致，无需归一。
VK_ALIAS = {
    0xA0: L.VK_SHIFT_L,        # VK_LSHIFT   -> Shift
    0xA2: L.VK_CONTROL_L,      # VK_LCONTROL -> Ctrl
    0xA4: L.VK_MENU_L,         # VK_LMENU    -> Alt
}

# char -> VK：仅在极少数情况下 KeyCode.vk 缺失（例如某些注入事件）时兜底，
# 只覆盖字母与数字，够用了；主路径永远是 VK。
_CHAR_VK = {}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _CHAR_VK[_c] = ord(_c)
for _i, _c in enumerate("1234567890"):
    _CHAR_VK[_c] = ord(_c)                  # 数字键 VK 就是 ASCII 码 0x30..0x39
_CHAR_VK.update({" ": L.VK_SPACE, "\r": L.VK_RETURN, "\n": L.VK_RETURN,
                 "\t": L.VK_TAB, "\x08": L.VK_BACK})


def resolve_vk(key):
    """
    pynput 回调参数 -> 物理 VK。
    实际到达的形式有三种：KeyCode(.vk)、Key 枚举(.value 是 KeyCode)、或 None(未知)。
    """
    if key is None:
        return None
    vk = getattr(key, "vk", None)
    if vk is None:                                   # Key 枚举
        val = getattr(key, "value", None)
        vk = getattr(val, "vk", None)
    if vk is None:
        char = getattr(key, "char", None)
        if char:
            vk = _CHAR_VK.get(char.upper())
        return VK_ALIAS.get(vk, vk) if vk is not None else None
    return VK_ALIAS.get(vk, vk)


class KeyboardWatcher(object):
    """把按键事件转交给音效引擎，并负责监听线程的存活维护。"""

    WATCH_INTERVAL = 5.0        # 看门狗巡检周期（秒）
    MAX_RESTART = 50            # 重建上限，避免后端坏了时无限重启刷日志

    def __init__(self, engine, log=None):
        self.engine = engine
        self.log = log or (lambda msg: None)
        self.listener = None
        self._alive = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog = None
        self._restarts = 0
        self._stats = {"press": 0, "unknown": 0}
        self._last_press = 0.0

    # ------------------------------------------------------------ 生命周期
    def start(self, reset_stop=True):
        """启动低级钩子；失败返回 False（程序继续静默运行，不抛异常）。"""
        if Listener is None:
            self.log("pynput 不可用: %r" % (_IMPORT_ERROR,))
            return False
        with self._lock:
            if self.listener is not None:
                return True
            try:
                self.listener = Listener(on_press=self._on_press,
                                         on_release=self._on_release)
            except Exception as exc:
                self.log("创建键盘监听失败: %r" % (exc,))
                return False
        self.listener.daemon = True
        self.listener.start()
        self._alive = True
        if reset_stop:
            self._stop.clear()
        if self._watchdog is None:
            self._watchdog = threading.Thread(target=self._watch,
                                              name="kbd-watchdog", daemon=True)
            self._watchdog.start()
        return True

    def stop(self, timeout=2.0):
        """停止钩子：先置标志让看门狗不再重建，再 join。"""
        self._stop.set()
        self._alive = False
        with self._lock:
            lst = self.listener
            self.listener = None
        if lst is not None:
            try:
                lst.stop()
            except Exception as exc:
                self.log("停止监听异常: %r" % (exc,))
            try:
                lst.join(timeout)
            except Exception:
                pass
        if self._watchdog is not None:
            self._watchdog.join(timeout)
            self._watchdog = None

    @property
    def running(self):
        return bool(self._alive and self.listener is not None
                    and getattr(self.listener, "running", False))

    # ------------------------------------------------------------ 回调
    def _on_press(self, key):
        # 绝不 return False：pynput 会把 False 当作"停止监听"，也会被我方误当成拦截
        try:
            self._stats["press"] += 1
            self._last_press = time.monotonic()
            vk = resolve_vk(key)
            if vk is None:
                self._stats["unknown"] += 1
                return
            self.engine.play_vk(vk)
        except Exception as exc:                     # 回调异常会终止钩子线程，必须吞掉
            if self._stats["press"] <= 5:
                self.log("按键处理异常: %r" % (exc,))

    def _on_release(self, key):
        try:
            vk = resolve_vk(key)
            if vk is not None:
                self.engine.release_vk(vk)           # 释放不出声，只做自动重复抑制
        except Exception:
            pass

    # ------------------------------------------------------------ 看门狗
    def _watch(self):
        """
        钩子线程可能在系统休眠/会话切换/驱动异常后终止。检测到不在跑就重建，
        重建次数超限则放弃（此时程序仍不干扰输入，只是没有音效）。
        """
        while not self._stop.wait(self.WATCH_INTERVAL):
            if self._stop.is_set() or self.listener is None:
                break
            if self.running:
                continue
            if self._restarts >= self.MAX_RESTART:
                continue
            self._restarts += 1
            self.log("键盘监听线程已终止，第 %d 次重建" % self._restarts)
            with self._lock:
                old, self.listener = self.listener, None
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
            if not self.start(reset_stop=False):
                self.log("重建监听失败，稍后继续重试")
