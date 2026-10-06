# -*- coding: utf-8 -*-
"""
开机自启动模块：读写当前用户注册表 Run 键（无需管理员权限）。

路径：HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
值名：KeyboardTune   数据类型：REG_SZ   数据：静默启动用的命令行

注意：以 python.exe 启动会闪一个控制台窗口，因此自启动命令固定指向 pythonw.exe
（同目录的无控制台解释器），入口脚本用 .pyw 双保险。
"""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "KeyboardTune"


def _entry_script():
    """本程序的启动脚本绝对路径（优先 main.pyw）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for name in ("main.pyw", "main.py"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            return p
    return os.path.abspath(sys.argv[0])


def command_line():
    """
    注册表里要写的命令行。
    打包成 exe 时 sys.frozen 为真，直接用可执行文件自身；
    源码运行时用 pythonw.exe + 脚本路径，全程不带控制台。
    """
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    base = os.path.splitext(os.path.basename(sys.executable))[0].lower()
    exe = sys.executable
    if base != "pythonw":
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(cand):
            exe = cand
    return '"%s" "%s"' % (exe, _entry_script())


def is_enabled():
    """注册表里是否已经有本程序的自启动项。"""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return bool(value)


def enable():
    """写入自启动项；失败返回 (False, 原因)。"""
    try:
        import winreg
    except ImportError:
        return False, "当前环境没有 winreg 模块"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command_line())
    except OSError as exc:
        return False, "写注册表失败: %r" % (exc,)
    return True, None


def disable():
    """删除自启动项；项不存在视为成功。"""
    try:
        import winreg
    except ImportError:
        return False, "当前环境没有 winreg 模块"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, None
    except OSError as exc:
        return False, "删注册表失败: %r" % (exc,)
    return True, None


def sync(config, log=None):
    """
    以注册表的真实状态为准回写配置（用户可能手删了注册表项），
    返回当前是否启用。程序启动时调用一次。
    """
    log = log or (lambda m: None)
    actual = is_enabled()
    if config.get("autostart") != actual:
        log("自启动状态与配置不一致（配置=%s 注册表=%s），以注册表为准"
            % (config.get("autostart"), actual))
        config.set("autostart", actual)
    return actual
