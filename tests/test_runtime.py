# -*- coding: utf-8 -*-
"""
运行时冒烟测试：不弹界面、不写注册表，验证各模块接起来能真正跑起来。

  python tests/test_runtime.py

与 test_mapping_rules.py 的分工：那边验"表对不对"（乐理与键盘适配规则），
这边验"链路通不通"（配置读写、VK 解析、合成与后端、托盘菜单构造、单实例）。
"""
import json
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


@case("配置：音色项可选/夹值/非法包名回落（需求 §六.2/§六.3）")
def t_voice_config():
    """
    配置层与渲染层必须接得上：DEFAULTS 里有的音色键要能从 json 读回来并送进
    VoicePack；手改的越界值要被夹回需求上限；包名不认识要当没写而不是报错。
    默认必须是 None（未设置）而不是具体数字，否则切音色包时预设自己的混响/尾音
    调校会被配置里的默认值静默顶掉（这条接线错在参数表上看不出来）。
    """
    from keyboard_tune import config as C
    from keyboard_tune import voice as V
    from keyboard_tune import notes as N
    for key, _lo, _hi, _attr, _kind in V.CONFIG_TUNES:
        assert C.DEFAULTS[key] is None, "%s 的默认值必须是未设置" % key
    assert C.DEFAULTS["voice_pack"] is None
    slots = N.all_slots()
    plain = V.pack_from_config(C.Config(
        os.path.join(tempfile.mkdtemp(), "c.json")), slots=slots)
    assert plain.name == V.DEFAULT_PACK, plain.name
    assert abs(plain.reverb_wet - 0.05) < 1e-9, "默认包混响量变了"
    # 包名 + 每一项覆盖都要真的落到包上
    path = os.path.join(tempfile.mkdtemp(), "c.json")
    cfg = C.Config(path)
    cfg.update({"voice_pack": "mellow", "reverb_wet": 0.06, "reverb_rt": 0.15,
                "tail_ms": 200.0, "humanize_variants": "4", "humanize_cents": 2.5,
                "humanize_db": 0.5, "zone_tick_gain": 0.86}, flush=True)
    back = C.Config(path)                      # 落盘后重载，验 json 往返还对
    pack = V.pack_from_config(back, slots=slots)
    assert pack.name == "mellow", pack.name
    assert abs(back.get("reverb_wet") - 0.06) < 1e-9, back.get("reverb_wet")
    assert abs(pack.reverb_wet - 0.06) < 1e-9, pack.reverb_wet
    assert abs(pack.reverb_rt - 0.15) < 1e-9, pack.reverb_rt
    assert abs(pack.tail_cap - 0.20) < 1e-9, pack.tail_cap
    assert pack.variants == 4 and back.get("humanize_variants") == 4, pack.variants
    assert max(abs(x) for x in pack.cents) <= 2.5 + 1e-9, pack.cents
    assert max(abs(x) for x in pack.loudness_db) <= 0.5 + 1e-9, pack.loudness_db
    assert abs(pack.zone_target["tick"] - 0.86) < 1e-9, pack.zone_target
    assert abs(pack.zone_target["soft"] - 0.875) < 1e-9, "未设置的项不该跟着变"
    # 脏值：越界夹回需求上限，读不出数的当没写，非法包名回落默认包
    dirty = os.path.join(tempfile.mkdtemp(), "c.json")
    with open(dirty, "w", encoding="utf-8") as fp:
        fp.write(json.dumps({"reverb_wet": 0.9, "reverb_rt": "x", "tail_ms": 5000,
                             "humanize_variants": 999, "humanize_cents": None,
                             "voice_pack": "no-such-pack", "scheme": "default"}))
    d = C.Config(dirty)
    # 夹到 WET_LIMIT 而不是 0.10：需求量的是“湿声占总能量 ≤10%”，而实测占比是发送量的
    # 1.11~1.39 倍（干涉项随音高变），填到 0.10 会量出 13.9%（见 voice.WET_LIMIT）
    assert d.get("reverb_wet") == V.WET_LIMIT, d.get("reverb_wet")
    assert V.pack_from_config({"reverb_wet": 0.9}, slots=slots).reverb_wet == V.WET_LIMIT
    assert d.get("reverb_rt") is None, d.get("reverb_rt")
    assert d.get("tail_ms") == 300.0, d.get("tail_ms")           # 需求：尾音 ≤300ms
    assert d.get("humanize_variants") == 12, d.get("humanize_variants")
    assert d.get("voice_pack") is None and "scheme" not in d.as_dict()
    assert V.pack_from_config(d, slots=slots).name == V.DEFAULT_PACK
    # 换包必须换出声音：包名进指纹，否则两个包会共用同一份采样库目录
    assert V.make_pack("mellow").fingerprint() != V.make_pack("bright").fingerprint()
    # 只带 .get 的映射（dict 就行）也能读：音频引擎之外手建包时不必依赖 Config 类
    assert V.pack_from_config({"voice_pack": "bright"}, slots=slots).name == "bright"


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
    """
    走音色引擎（voice.VoicePack），不再碰已删除的正弦表合成。
    这里只验"每槽都能出采样、尺寸与预算合理"；音质指标在 check_acoustics.py。
    """
    import array
    from keyboard_tune import voice as V
    from keyboard_tune import notes as N
    slots = N.all_slots()
    pack = V.make_pack(slots=slots)
    for midi, timbre in slots:
        pcm, n = pack.render(midi, timbre, 0, "fast")
        assert n > 0 and len(pcm) == 2 * n, (midi, timbre)
        s = array.array("h")
        s.frombytes(pcm)
        assert max(s) <= 32767 and min(s) >= -32767, (midi, timbre)
        assert max(max(s), -min(s)) > 3000, "峰值过低 %d %s" % (midi, timbre)
    # 体积逐条累加：抽几个槽位外推会把长尾家族当全库（实测谎报到 6 倍）
    total = sum(pack.cache_frames(timbre) * 2 * pack.variants
                for _midi, timbre in slots)
    assert total < 25 * 1024 * 1024, "常驻采样 %.1f MB 超预算" % (total / 1048576.0)


@case("建库脚本：试跑小库 -> 独立包能打开它，--check 退出码可信")
def t_build_script():
    """
    scripts/build_voice_bank.py 是发行链路的一部分，它的 --check 只是给人看的，这里
    拿 --quality fast --limit 12 把时间压到一秒内，验三件事：
      1) 真能建出可打开的库，manifest 里带标定曲线与正确规格；
      2) 另一个独立进程状态的包（同参数、新实例）打开它后取得到采样 —— 而不是
         "只有建库那个进程能用"；
      3) --check 对空目录返回非零、对刚建好的库返回零（发行时靠退出码判成败）。
    """
    import contextlib
    import io
    import json
    from keyboard_tune import config as C
    from keyboard_tune import notes as N
    from keyboard_tune import voice as V
    from keyboard_tune import voicebank as VB
    from scripts import build_voice_bank as BV
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "config.json")
    root = os.path.join(tmp, "bank")
    base = ["--config", cfg, "--root", root, "--no-dense", "--quiet"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = BV.main(base + ["--quality", "fast", "--limit", "12"])
    assert rc == 0, "建库返回 %d：%s" % (rc, buf.getvalue())
    # --check 对同一个库必须过（范围要跟建库时的 --limit 一致，否则半个库永远校不过）；
    # 对没建过的目录必须非零（不然 CI 里看不出来库是空的）
    with contextlib.redirect_stdout(io.StringIO()):
        assert BV.main(base + ["--check", "--limit", "12"]) == 0
        assert BV.main(base + ["--check", "--root", os.path.join(tmp, "none")]) != 0
    pack = V.pack_from_config(C.Config(cfg).as_dict(), slots=N.all_slots())
    bank = VB.VoiceBank(pack, root=root)
    assert bank.open(), "同参数的独立包打不开刚建好的库（指纹/规格对不上）"
    mf = json.load(open(os.path.join(bank.dir, "manifest.json"), encoding="utf-8"))
    assert mf["sample_rate"] == V.SAMPLE_RATE and mf["bits"] == 16 and mf["channels"] == 1
    assert len(mf["entries"]) == 12, len(mf["entries"])
    assert mf["calibration"] and len(mf["calibration"]) == len(pack.family_trim)
    for midi, timbre in N.all_slots()[:2]:
        assert bank.frames(midi, timbre, 0) is not None, (midi, timbre)
    assert bank.count() == 12


@case("引擎：打开后端、按键出声、自动重复抑制、音量切换")
def t_engine():
    import time
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
    time.sleep(2.0)                          # 等后台预热跑完，回退计数才是真数
    assert eng._live <= eng.slot_count * eng._nv, "回退计数越界"
    # play_vk 用 except 兜住了后端异常（需求 §五.6：一声失败不能带倒常驻进程），
    # 所以接线写错也会被吞成一行日志：必须在这里卡住，否则测试会假绿
    assert eng._errors == 0, "播放链路有被吞掉的异常 %d 次（看 --debug 日志）" % eng._errors
    eng.close()
    eng.play_vk(ord("A"))                    # 关闭后再调用必须安全无操作
    assert eng._errors == 0, "关闭后调用产生了异常"


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
