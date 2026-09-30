#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建参考面板：审计器唯一需要的外部数据。

审计器要回答"这一天这只票能不能买/卖"，所以需要一张
(日期, 代码) → 当日是否封涨停 / 封跌停 / 是否 ST 的表。

本脚本用 **baostock**（免费、无需注册）自建这张表，不依赖任何商业数据源。

    pip install baostock pandas pyarrow
    python3 make_reference_panel.py --start 2016-01-01 --out panel.parquet

**关键在于涨跌停是算出来的，不是抄来的**，而算它必须用**当日生效的规则**：
创业板 2020-08-24 才从 ±10% 放宽到 ±20%、科创板开市即 ±20%、
北交所 ±30%、ST 在 2026-07-06 之前是 ±5%。
用一个固定百分比套全历史，是本工具要查的头号问题 —— 建数据时当然不能自己犯。

涨停价的计算口径：`round(前收盘 × (1 + 幅度), 2)`，A 股按四舍五入到分。
判定"封板"用收盘价等于涨停价（而非涨幅 ≥ 9.8% 之类的近似）。
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ashare_audit_standalone import limit_pct  # noqa: E402


# 判定封板用**半分容差**，不用取整后精确相等。
#
# 原因是**平局**：当 前收 × (1+幅度) 正好落在半分上（如 4.75×1.1 = 5.225），
# pandas / numpy 的 .round() 用银行家舍入（就近取偶）→ 5.22，
# 而交易所是四舍五入 → 5.23。两者在平局上系统性相反。
#
# 方向是单边的：银行家舍入有一半的平局向下取，算出的涨停价低于真实值，
# 于是"收盘 == 涨停价"不成立 → **漏判涨停**。
#
# 平局并不罕见：ST 股 ±5%，价格又多是 0.1 的整数倍，很容易凑出半分。
# 第一版就是取整后精确相等，在 11,354 行交叉比对里漏掉 12 个涨停（1.4%），
# 而且其中多数是 ST 股 —— 一个**系统性低估工具自己要查的那个问题**的 bug。
# 改成半分容差后，三个字段与独立构建的参考面板 100% 一致。
TOL = 0.005                      # 半分


def limit_prices(preclose, pct):
    """涨停价 / 跌停价的**理论值**（不取整，判定时用 TOL 容差）。"""
    return preclose * (1 + pct), preclose * (1 - pct)


def build(codes, start, end, out):
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"baostock 登录失败：{lg.error_msg}")
    frames = []
    try:
        for i, code in enumerate(codes, 1):
            rs = bs.query_history_k_data_plus(
                code, "date,code,close,preclose,isST,tradestatus",
                start_date=start, end_date=end, frequency="d", adjustflag="3")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                continue
            d = pd.DataFrame(rows, columns=["date", "code", "close",
                                            "preclose", "isST", "tradestatus"])
            frames.append(d)
            if i % 200 == 0:
                print(f"  {i}/{len(codes)} …", flush=True)
    finally:
        bs.logout()

    if not frames:
        raise SystemExit("一条数据都没取到")
    p = pd.concat(frames, ignore_index=True)
    p["date"] = pd.to_datetime(p["date"])
    for c in ("close", "preclose"):
        p[c] = pd.to_numeric(p[c], errors="coerce")
    p["is_st"] = p["isST"].astype(str).str.strip().eq("1")
    p["tradable"] = p["tradestatus"].astype(str).str.strip().eq("1")
    p = p.dropna(subset=["close", "preclose"])
    p = p[p["preclose"] > 0]

    # **逐行按当日生效的规则取幅度** —— 这是整张表的关键
    dstr = p["date"].dt.strftime("%Y-%m-%d")
    pct = [limit_pct(c, st, d)
           for c, st, d in zip(p["code"], p["is_st"], dstr)]
    p["limit_pct"] = pct
    up, dn = limit_prices(p["preclose"], p["limit_pct"])
    p["limit_up"] = (p["close"] - up).abs() <= TOL
    p["limit_down"] = (p["close"] - dn).abs() <= TOL

    # close_raw 用来算复牌首日的跳空幅度（held_through_suspension 检查）
    p["close_raw"] = p["close"]
    cols = ["date", "code", "limit_up", "limit_down", "is_st", "tradable",
            "close_raw"]
    p[cols].to_parquet(out, index=False)
    print(f"→ {out}  {len(p):,} 行  "
          f"封涨停 {int(p['limit_up'].sum()):,}  封跌停 {int(p['limit_down'].sum()):,}")


def all_a_share_codes():
    """全部 A 股代码（含**已退市**的 —— 幸存者偏差就从这里开始）。"""
    import baostock as bs
    bs.login()
    try:
        rs = bs.query_stock_basic()
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    d = pd.DataFrame(rows, columns=["code", "code_name", "ipoDate",
                                    "outDate", "type", "status"])
    d = d[d["type"] == "1"]                      # 1 = 股票
    return sorted(d["code"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="建审计器用的参考面板（baostock）")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default="panel.parquet")
    ap.add_argument("--codes", default=None,
                    help="逗号分隔的代码，如 sh.600000,sz.000001；默认全市场")
    a = ap.parse_args(argv)
    end = a.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    codes = ([c.strip() for c in a.codes.split(",")] if a.codes
             else all_a_share_codes())
    print(f"{len(codes)} 只股票，{a.start} ~ {end}")
    print("注意：默认包含**已退市**股票 —— 少了它们就是幸存者偏差。")
    build(codes, a.start, end, a.out)


if __name__ == "__main__":
    main()
