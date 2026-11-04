# -*- coding: utf-8 -*-
"""复用与本机 Edge 同构建版本的驱动缓存，避免每次启动联网查询。"""
import ctypes
import os
from pathlib import Path


def file_version(path):
    if os.name != "nt" or not path.is_file():
        return None
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    info = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
    return (info[4] >> 16, info[4] & 65535, info[5] >> 16, info[5] & 65535)


def cached_edge_driver(binary=None):
    browser = Path(binary) if binary else Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    browser_version = file_version(browser)
    if not browser_version:
        return None
    cache = Path(os.environ.get("SE_CACHE_PATH", str(Path.home() / ".cache/selenium"))) / "msedgedriver/win64"
    candidates = []
    for path in cache.glob("*/msedgedriver.exe"):
        driver_version = file_version(path)
        if driver_version and driver_version[:3] == browser_version[:3]:
            candidates.append((driver_version, path))
    return max(candidates, default=(None, None))[1]


def create_edge(options):
    from selenium import webdriver
    from selenium.webdriver.edge.service import Service
    cached = cached_edge_driver(options.binary_location or None)
    if cached:
        return webdriver.Edge(options=options, service=Service(str(cached)))
    # 首次无兼容缓存时限制自动准备的网络等待，不允许无提示长期卡住。
    os.environ.setdefault("SE_AVOID_STATS", "true")
    os.environ.setdefault("SE_TIMEOUT", "20")
    return webdriver.Edge(options=options)
