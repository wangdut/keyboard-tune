# -*- coding: utf-8 -*-
"""
运行支撑模块：日志、单实例互斥、静默化（不弹窗）、异常兜底。

后台常驻程序最容易出问题的不是功能，而是"出问题时用户看不见"：
  * 日志写到 %APPDATA%\\KeyboardTune\\run.log，超过上限自动截断，永不撑爆磁盘；
  * 未捕获异常走 excepthook 记录后不弹 Windows 错误报告对话框（SetErrorMode），
    避免半夜崩溃弹一个模态窗口卡住输入法/键盘；
  * 命名互斥体保证只跑一个实例 —— 两个实例会同时监听同一块键盘，声音叠两遍。
"""
import ctypes
import os
import sys
import threading
import time
import traceback

APP_NAME = "KeyboardTune"
LOG_MAX_BYTES = 512 * 1024        # 日志上限，超过则从头截断重来（不累积、不轮转文件）
FLUSH_LINES = 8                   # 攒够几行再开一次文件（生命周期关键点不受此限）

# 关键错误不弹对话框 / 不弹崩溃报告 / 不弹"未找到程序"窗口
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
SEM_NOOPENFILEERRORBOX = 0x8000

ERROR_ALREADY_EXISTS = 183


def data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def log_path():
    return os.path.join(data_dir(), "run.log")


class Logger(object):
    """极简线程安全日志（不引 logging：文件 handler 的锁与轮转对常驻小程序是负担）。"""

    def __init__(self, path=None, echo=False):
        self.path = path or log_path()
        self.echo = echo
        self._lock = threading.Lock()
        self._buf = []
        self._pending = 0

    def __call__(self, msg, urgent=False):
        self.write(msg, urgent=urgent)

    def write(self, msg, urgent=False):
        """
        批量落盘，但生命周期关键点（启动/崩溃）必须 urgent 立即刷盘：
        只缓存不刷的话，“启动了没、用的哪个后端”这类唯一重要的一行会
        永远留在内存里（常驻程序很少会产生 8 行日志）。
        """
        line = "[%s] %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg)
        if self.echo:
            # 调试模式下逐行即时输出，不然 --debug 看起来像卡住
            try:
                sys.stderr.write(line)
                sys.stderr.flush()
            except Exception:
                pass
        with self._lock:
            self._buf.append(line)
            self._pending += 1
            if self._pending < FLUSH_LINES and not urgent:
                return                      # 常驻程序不该为每行日志开一次文件
            buf = self._buf
            self._buf = []
            self._pending = 0
        self._drain(buf)

    def _drain(self, buf):
        if not buf:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            mode = "a"
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                mode = "w"
            with open(self.path, mode, encoding="utf-8", errors="replace") as fp:
                fp.writelines(buf)
        except OSError:
            pass

    def flush(self):
        with self._lock:
            buf, self._buf, self._pending = self._buf, [], 0
        self._drain(buf)


def quiet_error_mode(log=None):
    """关掉 Windows 的模态错误弹窗；只在 Windows 上生效，失败无所谓。"""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
                              | SEM_NOOPENFILEERRORBOX)
        return True
    except Exception as exc:
        if log:
            log("SetErrorMode 不可用: %r" % (exc,))
        return False


def acquire_single_instance(name=APP_NAME):
    """
    拿到命名互斥体返回 kernel32 句柄（进程存活期间必须持有引用，否则句柄被
    GC 关闭后第二个实例就能挤进来）；已有实例返回 None。
    """
    try:
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return -1                                  # 非 Windows：不做互斥
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_wchar_p)
    handle = kernel32.CreateMutexW(None, False, "Local\\%s_SingleInstance" % name)
    if not handle:
        return -1                                  # 创建失败也继续跑，别因噎废食
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    _MUTEX_KEEPALIVE.append(handle)                # 故意不释放
    return handle


_MUTEX_KEEPALIVE = []


def install_excepthook(log):
    """
    兜底：任何线程里未捕获的异常都只写日志。
    子线程（监听/托盘/渲染）炸了不应该带走整个进程，主循环另有看门狗负责恢复。
    """
    def hook(etype, value, tb):
        try:
            msg = "未捕获异常: %s" % "".join(
                traceback.format_exception(etype, value, tb)).strip()
            try:
                log(msg, urgent=True)        # 崩溃现场必须当场落盘
            except TypeError:                # log 也可能是普通函数
                log(msg)
        except Exception:
            pass
    sys.excepthook = hook
    threading.excepthook = lambda args: hook(
        args.exc_type, args.exc_value, args.exc_traceback)
