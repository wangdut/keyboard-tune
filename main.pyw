# -*- coding: utf-8 -*-
"""
正式发布入口（.pyw 扩展名 = Windows 用 pythonw.exe 打开，不带控制台窗口）。

双击运行、放进启动菜单、或由注册表 Run 项拉起，都用这个文件。
想在终端里看日志请改用：python main.py --debug  或  python -m keyboard_tune --debug
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from keyboard_tune.__main__ import main      # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
