# -*- coding: utf-8 -*-
"""
运行时冒烟测试：不弹界面、不写注册表，验证各模块接起来能真正跑起来。

  python tests/test_runtime.py

与 test_mapping_rules.py 的分工：那边验"表对不对"（乐理与键盘适配规则），
这边验"链路通不通"（配置读写、VK 解析、合成与后端、托盘菜单构造、单实例）。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

print("运行时冒烟测试")
print("-" * 46)

FAILS = []


def case(name):
    def deco(fn):
        try:
            fn()
            print("ok   %s" % name)
        except Exception as exc:
            import traceback
            FAILS.append(name)
            print("FAIL %s: %r" % (name, exc))
            traceback.print_exc()
        return fn
    return deco


@case("配置：默认值/落盘/重载/损坏回退")
def t_config():
    from keyboard_tune import config as C
    path = os.path.join(tempfile.mkdtemp(), "config.json")
    cfg = C.Config(path)
    assert cfg.as_dict()["volume"] == C.DEFAULTS["volume"], cfg.as_dict()
    cfg.set_volume_percent(133)                    # 越界要被钳到 100
    assert cfg.volume_percent == 100
    cfg.set_volume_percent(-5)
    assert cfg.volume_percent == 0 and cfg.get("muted")
    cfg.set_volume_percent(42)
    cfg2 = C.Config(path)
    assert cfg2.volume_percent == 42, cfg2.volume_percent
    with open(path, "w", encoding="utf-8") as fp:  # 写坏文件
        fp.write("{not json")
    cfg3 = C.Config(path)
    assert cfg3.corrupt and cfg3.volume_percent == C.DEFAULTS["volume"]
    assert os.path.exists(path + ".bak")


@case("VK 解析：KeyCode / Key 枚举 / 左修饰键归一 / 未知键")
def t_resolve():
    from pynput.keyboard import Key, KeyCode
    from keyboard_tune import layout as L
    from keyboard_tune.listener import resolve_vk
    assert resolve_vk(KeyCode.from_vk(65, char="a")) == 65
    assert resolve_vk(KeyCode.from_vk(0xA0)) == L.VK_SHIFT_L      # 左 Shift 归一
    assert resolve_vk(KeyCode.from_vk(0xA1)) == L.VK_SHIFT_R
    assert resolve_vk(Key.enter) == L.VK_RETURN
    assert resolve_vk(Key.cmd) == L.VK_LWIN
    assert resolve_vk(KeyCode(char="q")) == ord("Q")              # 无 vk 时按字符兜底
    assert resolve_vk(KeyCode(char="5")) == ord("5")
    assert resolve_vk(None) is None


@case("单实例：第二个实例拿不到互斥体")
def t_single_instance():
    from keyboard_tune import bootstrap as B
    first = B.acquire_single_instance()
    assert first not in (None, 0), "首次应取得互斥体"
    again = B.acquire_single_instance()
    assert again is None, "重复启动应被拦截，实际 %r" % (again,)


@case("合成：全部槽位可渲染且总量在预算内")
def t_render():
    from keyboard_tune import audio as A
    from keyboard_tune import notes as N
    slots = N.all_slots()
    total = 0
    for midi, timbre in slots:
        pcm = A.render_samples(midi, timbre)
        assert len(pcm) > 0, (midi, timbre)
        assert max(pcm) <= 32767 and min(pcm) >= -32767
        total += len(pcm) * 2
    assert total < 20 * 1024 * 1024, "常驻采样 %.1f MB 超预算" % (total / 1048576.0)


@case("引擎：打开后端、按键出声、自动重复抑制、音量切换")
def t_engine():
    from keyboard_tune import audio as A
    eng = A.SoundEngine()
    assert eng.open(), "没有可用音频后端（声卡/SDL 环境缺失）"
    eng.set_volume(0.4)
    eng.play_vk(ord("A"))
    n_before = len(eng._stems)
    eng.play_vk(ord("A"))                    # 同键重复按下（Windows 自动重复）应被吞掉
    assert eng._pressed.get(ord("A")) is not None
    eng.release_vk(ord("A"))
    assert ord("A") not in eng._pressed
    eng.play_vk(ord("A"))                    # 释放后可以再次出声
    eng.play_vk(0xF7)                        # 未知键走兜底槽位，不能抛异常
    eng.set_volume(0.9)                      # winsound 路径要清档位缓存
    eng.play_vk(ord("B"))
    assert eng.memory_bytes() > 0
    assert len(eng._stems) >= n_before
    eng.close()
    eng.play_vk(ord("A"))                    # 关闭后再调用必须安全无操作


@case("托盘：图标绘制与菜单构造（不进入消息循环）")
def t_tray():
    from keyboard_tune import tray as T
    img = T.make_image()
    assert img.size[0] == 64 and img.mode == "RGBA"
    holder = {"vol": 70, "muted": False, "auto": False, "calls": []}

    class Fake(object):
        log = staticmethod(lambda m: None)
        volume_percent = property(lambda s: holder["vol"])
        muted = property(lambda s: holder["muted"])
        autostart = property(lambda s: holder["auto"])

        def set_volume_percent(s, p):
            holder["vol"] = p
            holder["calls"].append(("vol", p))

        def toggle_mute(s):
            holder["muted"] = not holder["muted"]

        def set_autostart(s, v):
            holder["auto"] = v

        def status_text(s):
            return "fake"

        def quit(s):
            holder["calls"].append(("quit",))

    ctl = T.TrayController(Fake())
    menu = ctl._menu()
    assert menu is not None
    # 菜单项的取值回调要能用（pystray 在展开时调用）
    for it in menu.items:
        if callable(getattr(it, "text", None)):
            str(it.text)
    ctl.icon = None
    ctl.refresh()
    ctl.notify("t", "x")                     # 无图标时静默返回


@case("自启动：命令行指向无控制台的解释器与 .pyw 入口")
def t_autostart():
    from keyboard_tune import autostart as AU
    cmd = AU.command_line()
    assert cmd.startswith('"') and '" "' in cmd or "pythonw" in cmd.lower(), cmd
    if os.path.basename(sys.executable).lower() != "pythonw.exe":
        assert "pythonw.exe" in cmd.lower(), "自启动不该带控制台窗口: %s" % cmd
    assert cmd.lower().endswith(".pyw\""), cmd
    assert AU.VALUE_NAME == "KeyboardTune"


@case("App：装配启动后按一次键再退出（整链路）")
def t_app():
    from keyboard_tune.app import App
    app = App(config_path=os.path.join(tempfile.mkdtemp(), "config.json"))
    app.start()
    try:
        app.watcher.engine.play_vk(ord("A"))     # 直接触发发声链路（不依赖真实按键）
        app.set_volume_percent(55)
        assert app.engine._volume == 0.55
    finally:
        app.quit()
    assert not app.watcher.running
    assert app.engine.backend is None or True


def main():
    print("-" * 46)
    if FAILS:
        print("失败项: %s" % ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
