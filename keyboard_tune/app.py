# -*- coding: utf-8 -*-
"""
应用装配层：把配置、音效引擎、键盘监听、托盘四个模块接起来。

单独抽出这一层的目的：四个功能模块彼此不知道对方存在（只通过 App 暴露的方法
交互），换后端/换托盘库/改配置格式时只动这里。

主线程只做一件事——阻塞等待退出事件，因此空闲 CPU 接近 0（音效与监听都在
各自的线程里，日志是批量落盘）。
"""
import threading

from . import autostart
from . import bootstrap
from . import config as C
from . import layout as L
from . audio import SoundEngine
from . listener import KeyboardWatcher
from . tray import TrayController


class App(object):
    def __init__(self, echo=False, config_path=None):
        self.log = bootstrap.Logger(echo=echo)
        bootstrap.install_excepthook(self.log)
        bootstrap.quiet_error_mode(self.log)

        self.config = C.Config(config_path)
        if self.config.corrupt:
            self.log("配置文件无法解析，已备份为 config.json.bak 并使用默认值")
        self.engine = SoundEngine(config=self.config, log=self.log)
        self.watcher = KeyboardWatcher(self.engine, log=self.log)
        self.tray = TrayController(self)
        self._exit = threading.Event()
        self._quitting = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 托盘接口
    @property
    def volume_percent(self):
        return self.config.volume_percent

    @property
    def muted(self):
        return bool(self.config.get("muted"))

    @property
    def autostart(self):
        return bool(self.config.get("autostart"))

    def set_volume_percent(self, percent):
        percent = self.config.set_volume_percent(percent)
        self.engine.set_volume(percent / 100.0)
        self.tray.notify("音量", "%d%%" % percent)
        return percent

    def toggle_mute(self):
        muted = self.config.toggle_mute()
        self.engine.set_volume(self.config.volume)
        self.tray.notify("键盘打字音效", "已静音" if muted else "已取消静音")
        return muted

    def set_autostart(self, want):
        ok, err = (autostart.enable() if want else autostart.disable())
        if ok:
            self.config.set("autostart", bool(want))
        else:
            self.log(err or "自启动设置失败")
        self.tray.notify("开机自启动",
                         "已开启" if ok and want else ("已关闭" if ok else "设置失败"))
        return ok

    def status_text(self):
        """
        就绪状态一行字。听着不对/完全没声时先看这一行：它能说清用的是哪个后端、
        哪个音色包、以及采样到底是从磁盘库读的还是在实时补渲（后者意味着没建库）。
        """
        bank = self.engine.bank
        src = ("采样库 %d 条" % bank.count()) if self.engine.bank_ready else "实时渲染"
        return "后端 %s · %d 键 · 槽位 %d · 音色 %s · %s" % (
            self.engine.backend_name, len(L.KEYS), self.engine.slot_count,
            self.engine.voice_name, src)

    # ------------------------------------------------------------ 生命周期
    def start(self):
        # 先留痕再开后端：声卡驱动异常 / mixer.init 卡住时，日志里至少能看出
        # “进程起过、死在哪一步”，否则只有一片空白，无从判断是没启动还是启动失败。
        self.log("启动中…", urgent=True)
        prefer = self.config.get("backend")
        self.engine.open(prefer=None if prefer in (None, "auto", "simpleaudio")
                         else prefer)
        self.engine.set_volume(self.config.volume)
        self.log("就绪：%s · 音量 %d%%" % (self.status_text(),
                                        self.config.volume_percent),
                 urgent=True)   # 这行是“用的哪个后端、盖了多少键”的证据
        autostart.sync(self.config, self.log)
        if not self.watcher.start():
            self.log("键盘监听启动失败：将只有托盘，没有打字音效", urgent=True)
        self.tray.start()
        return self

    def run(self):
        """静默常驻：阻塞直到用户从托盘退出。"""
        self.start()
        self._exit.wait()
        self.quit()
        return 0

    def quit(self):
        """停监听 -> 停音效 -> 落配置 -> 解除主线程阻塞。可重复调用。"""
        with self._lock:
            if self._quitting:
                return
            self._quitting = True
        self.log("退出中…")
        held = self.engine.memory_bytes() / 1048576.0
        try:
            self.watcher.stop()
        except Exception as exc:
            self.log("停止监听异常: %r" % (exc,))
        try:
            self.engine.close()                 # 停止全部发声并释放后端
        except Exception as exc:
            self.log("停止音效异常: %r" % (exc,))
        try:
            self.config.save()
        except Exception as exc:
            self.log("保存配置异常: %r" % (exc,))
        self.log("已退出，退出前常驻采样 %.2f MB" % held)
        self.log.flush()
        self._exit.set()

    def request_exit(self):
        self._exit.set()
