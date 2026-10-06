# -*- coding: utf-8 -*-
"""
程序入口：python -m keyboard_tune [--debug] [--selftest] [--dump-map]

  --debug     日志同时输出到终端（源码调试用；正式发布走 main.pyw，无控制台）
  --selftest  跑一遍映射规则与音频自检后退出，不启动监听
  --dump-map  打印全部按键的音高/音色表，便于核对
"""
import argparse

from . import bootstrap


def main(argv=None):
    ap = argparse.ArgumentParser(prog="keyboard-tune",
                                 description="键盘打字旋律音效（后台常驻）")
    ap.add_argument("--debug", action="store_true", help="日志输出到终端")
    ap.add_argument("--selftest", action="store_true", help="只做规则自检，不常驻")
    ap.add_argument("--dump-map", action="store_true", help="打印按键-音效映射表")
    ap.add_argument("--once", type=float, default=None,
                    help="运行指定秒数后自动退出（冒烟测试用）")
    args = ap.parse_args(argv)

    if args.dump_map:
        from . import notes as N
        from . import layout as L
        for vk in sorted(L.KEYS):
            print(N.describe(vk))
        return 0

    if args.selftest:
        import importlib.util
        import os
        # tests 不在包内（它是仓库级的验收脚本），按路径加载，避免把测试捆进发布包
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tests", "test_mapping_rules.py")
        spec = importlib.util.spec_from_file_location("_kbd_rules", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run_all()

    if bootstrap.acquire_single_instance() is None:
        # 已有实例：不能重复监听，否则同一块键盘会叠两遍声音
        print("KeyboardTune 已在运行（同一时间只允许一个实例）")
        return 0

    from . app import App
    app = App(echo=args.debug)
    if args.once is not None:
        import threading
        threading.Timer(args.once, app.request_exit).start()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
