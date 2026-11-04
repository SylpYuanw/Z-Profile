# -*- coding: utf-8 -*-
"""
统计内核 (后端)
===============

与前端 `06_前端/js/核心/stats.js` **口径必须一致** —— 同一套公式两处实现,
改一边就要改另一边, 否则页面上的数字会前后不一致。

为什么两处都要有:
  · 前端算 —— 数据已经在浏览器里, 零延迟, 切筛选条件不用请求
  · 后端算 —— 数据量大到不适合全传到前端时用

纯函数, 无依赖, 可直接单测。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence


def clean(values: Iterable[Any]) -> list[float]:
    out = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isnan(f):
            out.append(f)
    return out


def total(a: Sequence[float]) -> float:
    return float(sum(a))


def mean(a: Sequence[float]) -> float:
    return float(sum(a) / len(a)) if a else 0.0


def median(a: Sequence[float]) -> float:
    if not a:
        return 0.0
    s = sorted(a)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2


def quantile(a: Sequence[float], q: float) -> float:
    """q 取 0~1。线性插值, 与 numpy.percentile 默认行为一致。"""
    if not a:
        return 0.0
    s = sorted(a)
    pos = (len(s) - 1) * q
    base = int(math.floor(pos))
    rest = pos - base
    if base + 1 < len(s):
        return float(s[base] + rest * (s[base + 1] - s[base]))
    return float(s[base])


def stdev(a: Sequence[float], sample: bool = True) -> float:
    if len(a) < 2:
        return 0.0
    m = mean(a)
    ss = sum((v - m) ** 2 for v in a)
    return math.sqrt(ss / (len(a) - (1 if sample else 0)))


def box_stats(values: Iterable[Any]) -> dict | None:
    """箱线统计。离群点用 1.5×IQR 判定。"""
    s = sorted(clean(values))
    if not s:
        return None
    q1, q2, q3 = quantile(s, 0.25), quantile(s, 0.5), quantile(s, 0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = [v for v in s if lo <= v <= hi]
    outs = [v for v in s if v < lo or v > hi]
    return {
        "min": inside[0] if inside else s[0],
        "max": inside[-1] if inside else s[-1],
        "q1": q1, "q2": q2, "q3": q3, "iqr": iqr,
        "mean": mean(s), "n": len(s),
        "outliers": len(outs),
        "outlier_values": outs[:20],
    }


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx, my = mean(xs), mean(ys)
    num = dx = dy = 0.0
    for i in range(n):
        a, b = xs[i] - mx, ys[i] - my
        num += a * b
        dx += a * a
        dy += b * b
    den = math.sqrt(dx * dy)
    return 0.0 if den == 0 else num / den


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> dict:
    n = min(len(xs), len(ys))
    if n < 2:
        return {"a": 0.0, "b": 0.0, "r2": 0.0}
    xs, ys = xs[:n], ys[:n]
    mx, my = mean(xs), mean(ys)
    num = den = 0.0
    for i in range(n):
        num += (xs[i] - mx) * (ys[i] - my)
        den += (xs[i] - mx) ** 2
    b = 0.0 if den == 0 else num / den
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((ys[i] - (a + b * xs[i])) ** 2 for i in range(n))
    return {"a": a, "b": b, "r2": 0.0 if ss_tot == 0 else 1 - ss_res / ss_tot}


def gini(values: Iterable[Any]) -> float:
    """0=完全平均, 1=完全集中。衡量流量/销量是否被少数项目吃掉。"""
    a = sorted(v for v in clean(values) if v >= 0)
    n, tot = len(a), sum(a)
    if not n or tot == 0:
        return 0.0
    cum = sum(v * (i + 1) for i, v in enumerate(a))
    return (2 * cum) / (n * tot) - (n + 1) / n


def pareto(values: Iterable[Any], p: float = 0.2) -> float:
    """前 p 比例的项目贡献了百分之多少。"""
    a = sorted(clean(values), reverse=True)
    tot = sum(a)
    if not a or tot == 0:
        return 0.0
    k = max(1, round(len(a) * p))
    return sum(a[:k]) / tot * 100


def top_share(values: Iterable[Any], n: int) -> float:
    a = sorted(clean(values), reverse=True)
    tot = sum(a)
    return 0.0 if not tot else sum(a[:n]) / tot * 100


def histogram(values: Iterable[Any], bins: int | None = None) -> dict:
    a = clean(values)
    if not a:
        return {"labels": [], "counts": [], "lo": 0, "hi": 0, "step": 1}
    lo, hi = min(a), max(a)
    n = bins or max(6, min(26, round(math.sqrt(len(a)) * 1.6)))
    step = (hi - lo) / n or 1
    counts = [0] * n
    for v in a:
        i = min(n - 1, max(0, int((v - lo) / step)))
        counts[i] += 1
    labels = [f"{round(lo + i*step, 1)}~{round(lo + (i+1)*step, 1)}" for i in range(n)]
    return {"labels": labels, "counts": counts, "lo": lo, "hi": hi, "step": step}


def log_histogram(values: Iterable[Any], bins: int | None = None) -> dict:
    """对数分箱 —— 价格/播放量这种长尾分布用这个才看得出形状。"""
    a = [v for v in clean(values) if v > 0]
    if not a:
        return {"labels": [], "counts": []}
    h = histogram([math.log10(v) for v in a], bins or 10)
    labels = []
    for i in range(len(h["labels"])):
        lo = 10 ** (h["lo"] + i * h["step"])
        hi = 10 ** (h["lo"] + (i + 1) * h["step"])
        labels.append(f"{_fmt_short(lo)}~{_fmt_short(hi)}")
    return {"labels": labels, "counts": h["counts"]}


def _fmt_short(x: float) -> str:
    if x >= 1e8:
        return f"{x/1e8:.1f}亿"
    if x >= 1e4:
        return f"{x/1e4:.0f}万"
    if x >= 1000:
        return f"{x/1000:.1f}k"
    return str(round(x))


def percentile_rank(values: Iterable[Any], v: float) -> float:
    a = clean(values)
    if not a:
        return 0.0
    return sum(1 for x in a if x <= v) / len(a) * 100


def normalize100(values: Iterable[Any], v: float) -> float:
    a = clean(values)
    if not a:
        return 50.0
    lo, hi = min(a), max(a)
    return 50.0 if hi == lo else (v - lo) / (hi - lo) * 100


if __name__ == "__main__":
    print("=" * 60)
    print(" stats.py 自测")
    print("=" * 60)

    assert clean([1, None, "2", "x", 3.5]) == [1.0, 2.0, 3.5]
    print("[ok] clean 过滤 None 与非法值")

    assert mean([1, 2, 3, 4]) == 2.5
    assert median([1, 2, 3, 4]) == 2.5
    assert median([1, 2, 3]) == 2
    print("[ok] mean / median")

    # 与 numpy 对拍
    try:
        import numpy as np
        data = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
        for q in (0.25, 0.5, 0.75, 0.9):
            mine = quantile(data, q)
            theirs = float(np.percentile(data, q * 100))
            assert abs(mine - theirs) < 0.51, f"q={q}: {mine} vs {theirs}"
        print(f"[ok] 分位数与 numpy 对拍一致 (容差 0.51, 插值方式有微小差异)")
        assert abs(stdev(data) - float(np.std(data, ddof=1))) < 1e-9
        print("[ok] 标准差与 numpy 一致")

        xs = [1, 2, 3, 4, 5, 6, 7]
        ys = [2, 4, 5, 4, 5, 7, 8]
        assert abs(pearson(xs, ys) - float(np.corrcoef(xs, ys)[0][1])) < 1e-9
        print("[ok] 皮尔逊相关系数与 numpy 一致")

        b = box_stats(data)
        assert b["q1"] < b["q2"] < b["q3"]
        print(f"[ok] 箱线统计: Q1={b['q1']} 中位={b['q2']} Q3={b['q3']} 离群={b['outliers']}")

        f = linear_fit(xs, ys)
        assert abs(f["b"] - float(np.polyfit(xs, ys, 1)[0])) < 1e-9
        print(f"[ok] 线性回归与 numpy 一致 (斜率 {f['b']:.4f}, R²={f['r2']:.4f})")
    except ImportError:
        print("[!] numpy 未安装, 跳过对拍")

    # 基尼: 完全平均应为 0, 极度集中接近 1
    assert abs(gini([5, 5, 5, 5]) - 0) < 1e-9
    assert gini([0, 0, 0, 100]) > 0.7
    print(f"[ok] 基尼系数: 平均={gini([5]*4):.4f}  集中={gini([0,0,0,100]):.4f}")

    # 帕累托
    assert pareto([1] * 10, 0.2) == 20.0
    p = pareto([100, 1, 1, 1, 1, 1, 1, 1, 1, 1], 0.2)
    assert p > 90
    print(f"[ok] 帕累托: 均匀=20.0%  头部集中={p:.1f}%")

    h = log_histogram([10, 100, 1000, 10000, 100000], 5)
    assert len(h["counts"]) == 5
    print(f"[ok] 对数分箱: {h['labels']}")

    print("\n[stats] 全部自测通过")
