# -*- coding: utf-8 -*-
"""
调试入口（带控制台）：python main.py [参数]

与 main.pyw 的唯一区别是默认加 --debug，日志会同时打到终端，方便观察启动、
后端选择、异常这些信息。正式使用请运行 main.pyw（完全静默）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from keyboard_tune.__main__ import main      # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not any(a in ("--debug", "--selftest", "--dump-map") for a in argv):
        argv = argv + ["--debug"]
    raise SystemExit(main(argv))
