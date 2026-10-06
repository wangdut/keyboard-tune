#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线高精度采样库构建器（发行工具，不参与程序运行）。

  python scripts/build_voice_bank.py                 # 按当前用户配置建/补全库
  python scripts/build_voice_bank.py --pack mellow   # 指定音色包（不动用户配置文件）
  python scripts/build_voice_bank.py --check         # 只校验现有库，不渲染
  python scripts/build_voice_bank.py --rebuild       # 忽略已有条目全部重渲染

为什么要单独一个离线脚本（需求 §四.1 方案 B）
  运行时"按下即出声"，一次 full 质量渲染约 23ms（单条计时，纯 Python 算下来已经贴着
  50ms 的延迟上限），密集连击毫无余量。离线渲染没这个约束：这里一律用最高精度（精确
  sin、完整模态列、4 梳状 + 2 全通混响），逐条落盘成 44.1kHz/16bit 单声道 WAV，运行时
  只剩一次 dict 查找 + 一次 26KB 小文件读。

整库要多久（实测口径）
  79 槽 x 6 变体 = 474 条，本机首次建库 ~33s、增量补条 ~0.4s（留用已有采样）。注意单条
  渲染 23ms x 474 = 11s，剩下的差不是代码慢：拿一个与本程序无关的固定纯计算负载跑 8s，
  同样在前 ~3s 保持 20ms/单元、之后跳到 40~78ms —— 持续满载后机器自己降频（或沙箱的 CPU
  配额生效）。所以这个数字只能当量级参考，别拿它当回归基线（运行时一次只渲一条，不存在
  这个现象）。

为什么默认是"逐音高稠密标定"（--dense）
  分区响量补偿是"渲染参数 + 参考音高集合"的导出量，而参考音高集合不进参数指纹。启动
  路径每家族只廉价跑 3 个点（实测 0.45s），非主键区的**平均值**在带内但单键可以偏出
  0.85~0.90；逐音高稠密标定（实测 1.3s）能把 33 个非主键区槽位全部钉进带内
  （tests/check_acoustics.py 的分区响量项就是拿 dense 曲线量的）。建库多花的那 0.9s，
  换运行时每次出声都一致。

库里为什么要带标定曲线
  voicebank.build() 把曲线写进 manifest，运行时 VoiceBank.open() 再恢复回包上；否则
  没命中的那几个键用廉价曲线现场补渲，同一张键盘上相邻键音量就差几个百分点（需求 §三
  要避免的正是这种不一致）。

指纹是这套流程的安全带：音色/映射/混响/尾音/分区比/重音琴条任一参数变了，包目录名就变，
旧库不会被静默复用；而标定密度变了目录名不变，所以 build() 会逐条比对补偿值并重渲对不
上的那些（见它的 stale/trim_tol）。
"""
import argparse
import array
import os
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from keyboard_tune import config as C                      # noqa: E402
from keyboard_tune import notes as N                       # noqa: E402
from keyboard_tune import voice as V                       # noqa: E402
from keyboard_tune import voicebank as VB                  # noqa: E402

# Windows 控制台常被重定向为 GBK，中文与数学符号编不出去会抛 UnicodeEncodeError，
# 把构建结论淹没在 traceback 里（验收脚本同口径处理）。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def load_settings(path, pack_name=None, variants=None):
    """
    读出"运行时会用的那套音色参数"。默认直接读用户配置文件，这样建出来的库与启动时
    现场生成的包对得上指纹；命令行项只是临时覆盖，绝不写回用户的 json。
    """
    cfg = C.Config(path)
    data = cfg.as_dict()
    if cfg.corrupt:
        print("! 配置文件无法解析（已备份为 %s.bak），本次按默认值构建" % cfg.path)
    if pack_name:
        data["voice_pack"] = pack_name
    if variants:
        data["humanize_variants"] = variants
    return cfg.path, data


def estimate(pack, slots, variants):
    """不渲染就算出整库常驻字节：帧数由琴条 decay 与档位决定，各家族差一倍以上。"""
    total = 0
    for _midi, timbre in slots:
        total += pack.cache_frames(timbre) * 2 * variants
    return total


def spot_check(bank, pack, slots, variants):
    """
    抽样验证落盘结果：规格必须是 44.1kHz/16bit/单声道，帧数必须等于 _plan 声明的缓存
    长度（不一致就是文件被截断，运行时会被 VoiceBank 判为坏文件），且不能是静音。
    """
    picks = []
    step = max(1, len(slots) // 6)
    for i in range(0, len(slots), step):
        picks.append(slots[i])
        if len(picks) >= 6:
            break
    bad = []
    for midi, timbre in picks:
        var = variants - 1
        key = pack.key_for(midi, timbre, var)
        rel = bank._entries.get(key)
        if rel is None:
            bad.append("%s 不在清单里" % key)
            continue
        path = os.path.join(bank.dir, rel)
        try:
            with wave.open(path, "rb") as wf:
                if (wf.getframerate() != V.SAMPLE_RATE or wf.getsampwidth() != 2
                        or wf.getnchannels() != 1):
                    bad.append("%s 规格不符" % key)
                    continue
                want = wf.getnframes()
                data = wf.readframes(want)
        except (OSError, ValueError, EOFError) as exc:
            bad.append("%s 读不出 (%r)" % (key, exc))
            continue
        if len(data) != 2 * want:
            bad.append("%s 数据不完整 %d/%d 字节" % (key, len(data), 2 * want))
            continue
        declare = pack.cache_frames(timbre)
        if want != declare:
            bad.append("%s 帧数 %d 与声明的 %d 不符" % (key, want, declare))
            continue
        s = array.array("h")
        s.frombytes(data)
        pk = max(max(s), -min(s))
        if pk < 3000:
            bad.append("%s 峰值仅 %d，接近静音" % (key, pk))
    return bad, len(picks)


def verify(settings, root, variants, slots, quiet):
    """
    建完之后用"启动路径的那个包"再打开一次库：能 open 成功、条目数对得上、抽样文件
    合格，才算这个库真的会被程序用上。这一步是脚本存在的理由 —— 自己验自己的库当然
    对得上，但运行时用的是廉价标定包，只有模拟它才知道会不会白建一场。
    """
    live = V.pack_from_config(settings, slots=slots)        # 运行时同款（3 点标定）
    bank = VB.VoiceBank(live, root=root,
                        log=(lambda m: None if quiet else print("  " + m)))
    if not bank.open():
        return False, ["运行时包打不开这个库（指纹或规格不符）"], 0, 0
    want = VB.expected_keys(live, slots, variants)
    miss = VB.missing(live, slots, root=root, variants=variants)
    bad, n = spot_check(bank, live, slots, variants)
    if miss and not quiet:
        print("! 有 %d 条采样缺失/损坏，运行时会对这些键现场补渲" % len(miss))
        for k in miss[:6]:
            print("    %s" % k)
    bytes_ = 0
    for key, rel in bank._entries.items():
        try:
            bytes_ += os.path.getsize(os.path.join(bank.dir, rel))
        except OSError:
            pass
    ok = bank.available and not miss and not bad
    return ok, bad + (["缺失 %d 条" % len(miss)] if miss else []), n, bytes_


def main(argv=None):
    ap = argparse.ArgumentParser(description="离线构建打字音效的预渲染采样库")
    ap.add_argument("--pack", choices=V.pack_names(), default=None,
                    help="音色包名（默认沿用配置文件里的 voice_pack）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 %%APPDATA%% 下那份）")
    ap.add_argument("--root", default=None, help="采样库根目录（默认 %%APPDATA%%/KeyboardTune/voicebank）")
    ap.add_argument("--quality", choices=("full", "fast"), default="full",
                    help="渲染精度；发行库必须 full，fast 只用于快速自检")
    ap.add_argument("--variants", type=int, default=None,
                    help="每个槽位的 humanize 变体条数（默认沿用音色包）")
    ap.add_argument("--rebuild", action="store_true", help="忽略已有条目，全部重渲染")
    ap.add_argument("--check", action="store_true", help="只校验现有库，不渲染任何采样")
    ap.add_argument("--no-dense", dest="dense", action="store_false",
                    help="用启动同款 3 点标定（快但单键分区响量会偏出 10~15%% 带）")
    ap.add_argument("--limit", type=int, default=0,
                    help="只渲染前 N 条，用于试跑；清单里会真的只登记这些条")
    ap.add_argument("--quiet", action="store_true", help="不打印逐条进度")
    args = ap.parse_args(argv)

    slots = N.all_slots()
    cfg_path, settings = load_settings(args.config, args.pack, args.variants)
    # 标定永远拿完整映射表：参考音高集合决定曲线形状，截断参考点会让小库的曲线与
    # 发行库不同（而密度不进指纹，看不出来）。--limit 只截【渲染范围】，不动标定。
    pack = V.pack_from_config(settings, slots=slots, dense=args.dense)
    variants = max(1, int(pack.variants))
    bank = VB.VoiceBank(pack, root=args.root)
    if args.limit:
        slots = slots[:(args.limit + variants - 1) // variants] or slots[:1]

    print("离线构建采样库")
    print("-" * 52)
    print("配置      %s" % cfg_path)
    print("音色包    %s  指纹 %s" % (pack.name, pack.fingerprint()))
    print("标定      %s" % ("逐音高稠密（发行口径）" if args.dense else "3 点廉价（启动口径）"))
    print("精度      %s   变体 %d 条/槽" % (args.quality, variants))
    print("目录      %s" % bank.dir)
    print("槽位      %d 个 -> 预期 %d 条采样，常驻约 %.1f MB"
          % (len(slots), len(slots) * variants,
             estimate(pack, slots, variants) / 1048576.0))
    if args.limit:
        print("! 试跑范围：只处理前 %d 个槽位（正式发行库请去掉 --limit 重建）" % len(slots))

    t0 = time.time()
    if not args.check:
        def progress(i, total, midi, timbre, var):
            if args.quiet:
                return
            if i % 40 == 0 or i == total:
                el = time.time() - t0
                eta = el / i * (total - i) if i else 0
                print("  %4d/%-4d %4.1f%%  %d %s v%d  剩 %s"
                      % (i, total, 100.0 * i / total, midi, timbre, var,
                         ("%3.0fs" % eta) if eta > 1 else "完成"))

        counts = VB.build(pack, slots, quality=args.quality, root=args.root,
                          log=(lambda m: None if args.quiet else print("  " + m)),
                          progress=progress, variants=variants, rebuild=args.rebuild)
        print("渲染      新渲 %d 条，留用 %d 条，重标定 %d 条，失败 %d 条，写盘 %.1f MB"
              % (counts["written"], counts["kept"], counts.get("retrim", 0),
                 counts["skipped"], counts["bytes"] / 1048576.0))
        if counts["skipped"]:
            print("! 有 %d 条渲染/写盘失败，运行时会对它们回退实时渲染" % counts["skipped"])

    # 校验用启动口径的包，范围与本次 --limit 一致（否则半个库永远校验不过）
    ok, problems, n_sample, bytes_ = verify(settings, args.root, variants,
                                            slots, args.quiet)
    print("校验      抽样 %d 条合格，库内 %.1f MB，耗时 %.1fs" % (n_sample, bytes_ / 1048576.0,
                                                        time.time() - t0))
    if not ok:
        for p in problems:
            print("! %s" % p)
        print("结论      未通过（程序仍会出声，只是走实时渲染回退）")
        return 1
    print("结论      就绪，程序启动即可使用该库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
