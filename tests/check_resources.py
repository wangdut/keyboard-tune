# -*- coding: utf-8 -*-
"""
资源占用自检：验证"内存 ≤100MB、空闲 CPU ≤1%"这两条硬指标。

  python tests/check_resources.py [每窗秒数] [窗口数]

内存上限按需求裁定放宽到 100MB（预采样音色库常驻），CPU 那条仍按原文 ≤1%。

测量口径：进程工作集（WorkingSet，任务管理器"内存"同一列）与进程 CPU 时间增量。
包含完整链路：音频后端 + 全部槽位渲染 + 全局键盘钩子 + 托盘图标。
"""
import ctypes
import ctypes.wintypes as WT
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# GBK 控制台下写中文/数学符号失败时，traceback 本身也打印不出去（实测本脚本会“静默退
# 出 1”：什么都看不到）。errors=replace 保证结论与原因都能落盘，与 check_acoustics 同做法。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", WT.DWORD),
                ("PageFaultCount", WT.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


_kernel32 = ctypes.windll.kernel32
_kernel32.GetCurrentProcess.restype = WT.HANDLE
_kernel32.K32GetProcessMemoryInfo.restype = WT.BOOL
_kernel32.K32GetProcessMemoryInfo.argtypes = [
    WT.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), WT.DWORD]
_kernel32.GetProcessTimes.restype = WT.BOOL
_PU64 = ctypes.POINTER(ctypes.c_ulonglong)
_kernel32.GetProcessTimes.argtypes = [WT.HANDLE, _PU64, _PU64, _PU64, _PU64]


def memory_mb():
    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    _kernel32.K32GetProcessMemoryInfo(_kernel32.GetCurrentProcess(),
                                      ctypes.byref(c), ctypes.sizeof(c))
    return (c.WorkingSetSize / 1048576.0, c.PeakWorkingSetSize / 1048576.0)


def cpu_seconds():
    ct = ctypes.c_ulonglong()
    et = ctypes.c_ulonglong()
    kt = ctypes.c_ulonglong()
    ut = ctypes.c_ulonglong()
    _kernel32.GetProcessTimes(_kernel32.GetCurrentProcess(), ctypes.byref(ct),
                              ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut))
    return (kt.value + ut.value) / 1e7


def main(seconds=10.0, windows=4):
    print("解释器 + 本脚本基线工作集: %.1f MB" % memory_mb()[0])
    from keyboard_tune.app import App
    from keyboard_tune import layout as L
    app = App()
    app.engine.open()
    app.engine.set_volume(app.config.volume)
    app.log("资源自检开始")
    app.watcher.start()
    app.tray.start()                      # 托盘也算常驻开销（pystray + Pillow）

    # 等后台预热线程把采样全部取完：启动期的 CPU 是一次性的，不能算进"空闲占用"。
    # 条数 = 槽位 x 变体（_stems 的键是 (槽, 变体)），拿 slot_count 当上限会在只加载
    # 1/6 时就放行，把预热 CPU 混进第一个采样窗口。
    #
    # 不再做“逐槽 _stem() 一遍”的兜底：新签名是 _stem(idx, var)，旧写法少一个参数，
    # 而且预热本来就把每条都取过了，重复一遍只会干扰读数。
    want = app.engine.slot_count * getattr(app.engine, "_nv", 1)
    t0 = time.monotonic()
    while len(app.engine._stems) < want and time.monotonic() - t0 < 120.0:
        time.sleep(0.2)
    render_s = time.monotonic() - t0

    work, peak = memory_mb()
    # 热身期单独丢弃：SDL 混音线程刚建、延迟加载的 DLL、首次 GC 都会污染读数。
    # 只用一个窗口采样会让"一次性开销"混进"空闲占用"，得出虚高且不可复现的数字，
    # 所以这里丢弃首窗、按最坏窗判定。
    time.sleep(3.0)
    samples = []
    for _ in range(windows):
        c0 = cpu_seconds()
        time.sleep(seconds)               # 空闲期：不按任何键
        samples.append(100.0 * (cpu_seconds() - c0) / seconds)
    work2, peak2 = memory_mb()

    idle_tail = samples[1:]
    mean_tail = sum(idle_tail) / len(idle_tail)
    worst = max(samples)
    print("-" * 56)
    print("音频后端 / 按键覆盖     : %s / %d 键" % (app.engine.backend_name, len(L.KEYS)))
    print("全部采样常驻内存      : %.2f MB" % (app.engine.memory_bytes() / 1048576.0))
    print("预热耗时            : %.2f s（%d 条，一次性开销）"
          % (render_s, want))
    print("工作集（渲染后/空闲后）: %.1f MB / %.1f MB   峰值 %.1f MB"
          % (work, work2, max(peak, peak2)))
    print("空闲 CPU 分窗（每窗 %.0f s）: %s"
          % (seconds, "  ".join("%.2f%%" % s for s in samples)))
    print("空闲 CPU（丢弃首窗）   : 均值 %.2f%%   最坏窗 %.2f%%"
          % (mean_tail, worst))
    print("-" * 56)
    ok = True
    if max(peak, peak2) > 100.0:
        print("超标：工作集 %.1f MB > 100 MB" % max(peak, peak2))
        ok = False
    if worst > 1.0:
        print("超标：空闲 CPU 最坏窗 %.2f%% > 1%%" % worst)
        ok = False
    app.tray.shutdown() if app.tray.icon is not None else None
    app.quit()
    return 0 if ok else 1


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    wins = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    raise SystemExit(main(secs, wins))
