# -*- coding: utf-8 -*-
"""
托盘控制模块：pystray 图标 + 右键菜单（音量 / 开机自启动 / 退出）。

为什么没有真的"滑条"：pystray 用的是系统原生菜单（Win32 TrackPopupMenu），
菜单项里没有滑杆控件，所以按需求允许的另一种形式实现——音量档位单选 + 逐级
加减，配合图标提示气泡显示当前音量，调整后立即生效并持久化。

图标不依赖任何外部图片文件：用 Pillow 现画一个简约键盘图案（圆角外壳 +
两排按键 + 空格条），这样程序拷到任何机器上都不会因为缺资源而起不来。
"""
import threading


def make_image(size=64, accent=(90, 170, 250)):
    """画一个 64x64 的键盘图标，返回 PIL.Image。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0
    # 外壳
    d.rounded_rectangle([2 * s, 14 * s, 62 * s, 50 * s], radius=5 * s,
                        fill=(32, 36, 44, 255), outline=(120, 130, 150, 255),
                        width=max(1, int(1.5 * s)))
    # 三排按键 + 空格：中间那一排用强调色，暗示"打字的主音区"
    for row, keys in enumerate(((8, 12), (6, 13), (7, 11))):
        x0, count = keys
        top = 19 * s + row * 8 * s
        for i in range(count):
            x = x0 * s + i * 4.2 * s
            col = accent if row == 1 else (205, 210, 220, 255)
            d.rounded_rectangle([x, top, x + 3.2 * s, top + 3.4 * s],
                                radius=0.8 * s, fill=col)
    d.rounded_rectangle([20 * s, 43 * s, 44 * s, 46.4 * s], radius=1.2 * s,
                        fill=(205, 210, 220, 255))
    return img


def _steps():
    """音量档位（单选组）。"""
    return (10, 25, 40, 55, 70, 85, 100)


class TrayController(object):
    """
    把 App 的若干操作暴露成托盘菜单。App 需要提供：
      volume_percent / set_volume_percent(p) / muted / toggle_mute()
      autostart / set_autostart(bool) / status_text() / quit()
    """

    def __init__(self, app, name="KeyboardTune"):
        self.app = app
        self.name = name
        self.icon = None
        self._thread = None
        self._stopped = threading.Event()

    # ------------------------------------------------------------ 菜单构造
    def _menu(self):
        """
        pystray 的 MenuItem(text, action, checked=None, radio=False, enabled=True)
        里 text/checked/enabled 都接受可调用：菜单展开时才取值，因此不需要手动
        通知刷新 —— 音量改了，下次展开自动是新状态。
        """
        import pystray
        from pystray import MenuItem as Item
        app = self.app
        step = 5

        def noop(*_a):                      # 只展示用的禁用项也需要一个 action
            return None

        def make_vol(pct):
            return Item("%d%%" % pct,
                        lambda *_a: app.set_volume_percent(pct),
                        checked=lambda _i: app.volume_percent == pct,
                        radio=True)

        volume_menu = pystray.Menu(
            Item("调高 %d%%" % step,
                 lambda *_a: app.set_volume_percent(
                     min(100, app.volume_percent + step))),
            Item("调低 %d%%" % step,
                 lambda *_a: app.set_volume_percent(
                     max(0, app.volume_percent - step))),
            pystray.Menu.SEPARATOR,
            *[make_vol(p) for p in _steps()])

        return pystray.Menu(
            Item(lambda _i: "当前音量 %d%%" % app.volume_percent, noop,
                 enabled=False),
            Item("音量", volume_menu),
            Item("静音", lambda *_a: app.toggle_mute(),
                 checked=lambda _i: app.muted),
            pystray.Menu.SEPARATOR,
            Item("开机自启动",
                 lambda *_a: app.set_autostart(not app.autostart),
                 checked=lambda _i: app.autostart),
            pystray.Menu.SEPARATOR,
            Item(lambda _i: app.status_text(), noop, enabled=False),
            Item("退出程序", lambda *_a: self.shutdown()),
        )

    # ------------------------------------------------------------ 生命周期
    def start(self):
        """在独立线程里跑消息循环；失败只记日志，不影响监听与音效。"""
        try:
            import pystray
        except ImportError as exc:
            self.app.log("托盘不可用: %r" % (exc,))
            return False
        try:
            self.icon = pystray.Icon(self.name, make_image(),
                                     "键盘打字音效 · %s" % self.app.status_text(),
                                     self._menu())
        except Exception as exc:
            self.app.log("创建托盘图标失败: %r" % (exc,))
            return False
        self._thread = threading.Thread(target=self._run, name="tray",
                                         daemon=True)
        self._thread.start()
        return True

    def _run(self):
        try:
            self.icon.run()
        except Exception as exc:
            self.app.log("托盘消息循环异常退出: %r" % (exc,))

    def notify(self, title, text):
        """气泡提示；托盘不可用时静默忽略。"""
        try:
            if self.icon is not None:
                self.icon.notify(text, title)
        except Exception:
            pass

    def refresh(self):
        """让菜单/提示重新取值（pystray 默认会在打开菜单时回调，这里用于外部主动刷新）。"""
        try:
            if self.icon is not None:
                self.icon.title = "键盘打字音效 · %s" % self.app.status_text()
                self.icon.update_menu()
        except Exception:
            pass

    def shutdown(self):
        """用户点退出：先收图标（菜单线程不能阻塞），再做资源清理。"""
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception as exc:
            self.app.log("停止托盘异常: %r" % (exc,))
        threading.Thread(target=self.app.quit, name="quit", daemon=True).start()

    def wait(self, timeout=None):
        """主线程用：阻塞直到托盘被关闭（即用户点了"退出"）。"""
        return self._stopped.wait(timeout)

    def join(self, timeout=2.0):
        if self._thread is not None:
            self._thread.join(timeout)
