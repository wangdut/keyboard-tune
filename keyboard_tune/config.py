# -*- coding: utf-8 -*-
"""
配置存储模块：音量等设置的读取/保存。

配置文件位置：%APPDATA%\\KeyboardTune\\config.json（当前用户目录，无需管理员权限）。
写入采用"临时文件 + 原子替换"，程序中途被杀掉也不会留下半个坏文件；读取失败时
自动备份原文件并回落默认值，保证程序永远能起来。
"""
import json
import os
import tempfile
import threading

APP_NAME = "KeyboardTune"
DEFAULTS = {
    "volume": 70,          # 主音量 0..100
    "muted": False,        # 静音开关（保留音量值）
    "autostart": False,    # 开机自启动（与注册表实际状态双向同步）
    "backend": "auto",     # 音效后端：auto / pygame / simpleaudio / winsound
    "scheme": "default",   # 映射方案（预留）
}


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def config_path():
    return os.path.join(config_dir(), "config.json")


class Config(object):
    """线程安全的键值配置（内存副本 + 落盘）。"""

    def __init__(self, path=None):
        self._lock = threading.RLock()
        self.path = path or config_path()
        self._data = dict(DEFAULTS)
        self.corrupt = False        # 供日志提示：原文件是否无法解析
        self.load()

    # ------------------------------------------------------------ 读写
    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fp:
                    raw = json.load(fp)
            except FileNotFoundError:
                return dict(self._data)
            except (OSError, ValueError) as exc:
                self.corrupt = True
                self._data = dict(DEFAULTS)
                self._corrupt_note = repr(exc)
                self._backup()
                return dict(self._data)
            if not isinstance(raw, dict):
                self.corrupt = True
                self._data = dict(DEFAULTS)
                return dict(self._data)
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
            self._data = self._sanitize(merged)
            return dict(self._data)

    def save(self):
        """原子写：先写同目录临时文件，再 replace。"""
        with self._lock:
            data = dict(self._data)
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="cfg", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(data, fp, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except OSError:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
        except OSError:
            # 配置盘不进去不应该影响打字音效，调用方（托盘）会保留内存值
            return False
        return True

    def _backup(self):
        try:
            if os.path.exists(self.path):
                os.replace(self.path, self.path + ".bak")
        except OSError:
            pass

    # ------------------------------------------------------------ 取值
    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value, flush=True):
        with self._lock:
            self._data[key] = value
            self._data = self._sanitize(self._data)
            if flush:
                self.save()

    def update(self, mapping, flush=True):
        with self._lock:
            self._data.update(mapping)
            self._data = self._sanitize(self._data)
            if flush:
                self.save()

    def as_dict(self):
        with self._lock:
            return dict(self._data)

    def _sanitize(self, d):
        """把外部来源（手改过的 json）拉回合法区间，避免下游做防御判断。"""
        try:
            vol = int(round(float(d.get("volume", 70))))
        except (TypeError, ValueError):
            vol = 70
        d["volume"] = min(100, max(0, vol))
        d["muted"] = bool(d.get("muted", False))
        d["autostart"] = bool(d.get("autostart", False))
        if d.get("backend") not in ("auto", "pygame", "simpleaudio", "winsound"):
            d["backend"] = "auto"
        return d

    # ------------------------------------------------------------ 便捷
    @property
    def volume(self):
        return self.get("volume") / 100.0 if not self.get("muted") else 0.0

    @property
    def volume_percent(self):
        return self.get("volume")

    def set_volume_percent(self, percent, flush=True):
        percent = min(100, max(0, int(round(percent))))
        self.update({"volume": percent, "muted": percent == 0}, flush=flush)
        return percent

    def toggle_mute(self, flush=True):
        with self._lock:
            muted = not self.get("muted")
            self._data["muted"] = muted
            if flush:
                self.save()
            return muted
