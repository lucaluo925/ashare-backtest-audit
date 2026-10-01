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

from ashare_audit_standalone import (  # noqa: E402
    normalize_code,
    has_price_limit,
    is_ex_rights,
    limit_pct,
)


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


def build(codes, start, end, out, full_universe=True):
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"baostock 登录失败：{lg.error_msg}")
    frames = []
    try:
        for i, code in enumerate(codes, 1):
            rs = bs.query_history_k_data_plus(
                code, "date,code,open,close,preclose,isST,tradestatus",
                start_date=start, end_date=end, frequency="d", adjustflag="3")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                continue
            d = pd.DataFrame(rows, columns=["date", "code", "open", "close",
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
    for c in ("open", "close", "preclose"):
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
    # 除权日：baostock 在 adjustflag=3 下给的 preclose 已经是**除权除息调整后的
    # 昨收**，也就是交易所算涨跌停价用的那个参考价。于是"前一行的 close 与本行的
    # preclose 不相等"就精确等价于"这一天除权除息/送转了" —— 不用额外下载任何东西。
    #
    # 为什么要把这一列发出去：很多引擎用 `close.shift(1)` 当参考价算涨跌停。
    # 在除权日那是错的，而且可以错得很离谱（送转日原始价会跳 50%）。
    # 有了这一列，审计器能把落在这些天上的成交单独点出来。
    # 判定本身在 ashare_rules.is_ex_rights 里（那边有测试覆盖）——
    # 这个下载脚本在没网的环境里跑不起来，所以规则不留在这里。
    p = p.sort_values(["code", "date"])
    prev_close = p.groupby("code", observed=True)["close"].shift(1)
    p["ex_rights"] = [is_ex_rights(pc, prv)
                      for pc, prv in zip(p["preclose"], prev_close)]

    # 开盘封板：按开盘价成交的引擎，看的是这两个
    p["open_limit_up"] = (p["open"] - up).abs() <= TOL
    p["open_limit_down"] = (p["open"] - dn).abs() <= TOL

    # **新股上市前 5 个交易日不设涨跌幅**（注册制），那几天不存在"封板"。
    #
    # 上市天数只在该股**第一根 K 线晚于取数起点**时才可信 —— 否则它在窗口
    # 开始前就已上市，cumcount 数出来的不是上市天数。判不准就按"有限制"
    # 处理（保守），因为误判成无限制会让真实封板漏掉。
    p = p.sort_values(["code", "date"])
    first = p.groupby("code", observed=True)["date"].transform("min")
    win_start = p["date"].min()
    nth = p.groupby("code", observed=True).cumcount() + 1
    known = first > win_start                 # 窗口内才上市 → 天数可信
    dsl = nth.where(known)
    no_limit = ~pd.Series(
        [has_price_limit(c, d, None if pd.isna(k) else int(k))
         for c, d, k in zip(p["code"], p["date"].dt.strftime("%Y-%m-%d"), dsl)],
        index=p.index)
    n_nl = int(no_limit.sum())
    if n_nl:
        print(f"  新股前 5 日无涨跌幅：{n_nl:,} 行，已清除其封板标记")
    for c in ("limit_up", "limit_down", "open_limit_up", "open_limit_down"):
        p.loc[no_limit, c] = False

    # close_raw 用来算复牌首日的跳空幅度（held_through_suspension 检查）
    p["close_raw"] = p["close"]
    # 这份面板是不是全市场 —— 幸存者偏差只能在全市场面板上查。
    # 标在数据里而不是只在文档里：否则用窄面板跑出来的"未发现幸存者偏差"
    # 会被当成"通过"，而那是本工具最该避免的那种静默降级。
    p["panel_is_full_universe"] = bool(full_universe)

    cols = ["date", "code", "preclose", "ex_rights", "panel_is_full_universe",
            "limit_up", "limit_down", "is_st", "tradable",
            "close_raw", "open_limit_up", "open_limit_down"]
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
    ap.add_argument("--from-trades", default=None,
                    help="直接从成交记录 CSV 读代码与日期范围，只下这些票 —— "
                         "全市场要几个小时，一份几十只票的记录只要几秒。"
                         "代价：**幸存者偏差查不了**（见下）")
    ap.add_argument("--pad-days", type=int, default=45,
                    help="--from-trades 时在成交区间两头各留多少自然日 "
                         "（T+1、停牌穿越、除权参考价都需要邻近交易日）")
    a = ap.parse_args(argv)

    full_universe = True
    start, end = a.start, a.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.from_trades:
        if a.codes:
            raise SystemExit("--from-trades 与 --codes 只能给一个")
        tr = pd.read_csv(a.from_trades)
        col_code = next((c for c in ("code", "代码", "symbol", "ts_code")
                         if c in tr.columns), None)
        col_date = next((c for c in ("date", "日期", "trade_date", "datetime")
                         if c in tr.columns), None)
        if not col_code or not col_date:
            raise SystemExit(f"{a.from_trades} 里找不到代码列或日期列；"
                             f"实际有 {list(tr.columns)}")
        codes = sorted({normalize_code(c) for c in tr[col_code].dropna()})
        d = pd.to_datetime(tr[col_date])
        pad = pd.Timedelta(days=a.pad_days)
        start = (d.min() - pad).strftime("%Y-%m-%d")
        end = (d.max() + pad).strftime("%Y-%m-%d")
        full_universe = False
        print(f"从 {a.from_trades} 读出 {len(codes)} 只股票；"
              f"区间按成交记录两头各留 {a.pad_days} 天 → {start} ~ {end}")
        print("** 这份面板只含你交易过的股票，所以：**")
        print("   - 幸存者偏差（survivorship）**查不了** —— 它要比对全市场的退市股。")
        print("     审计器会读面板里的 panel_is_full_universe 标记并明说这一项没查。")
        print("   - 其余 17 项都正常。想把幸存者偏差也查上，去掉 --from-trades 重建一次。")
    else:
        codes = ([c.strip() for c in a.codes.split(",")] if a.codes
                 else all_a_share_codes())
        print(f"{len(codes)} 只股票，{start} ~ {end}")
        print("注意：默认包含**已退市**股票 —— 少了它们就是幸存者偏差。")
    build(codes, start, end, a.out, full_universe=full_universe)


if __name__ == "__main__":
    main()
