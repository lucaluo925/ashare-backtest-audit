#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 股回测制度规则审计器 —— 单文件版，零安装。

    python3 ashare_audit_standalone.py 你的成交记录.csv --framework qlib

唯一依赖：pandas。

**本文件是生成的，不要手改** —— 改 src/ 下的源模块，再跑 tools/bundle.py。
生成源：ashare_rules.py、audit_formats.py、ashare_audit.py
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

# ============================================================
# 来自 src/ashare_rules.py
# ============================================================
"""A 股制度规则 —— 零依赖，可单独分发。

涨跌幅、印花税、熔断日、板块划分，全部对着一手资料核过，出处见各条注释。

**这里最容易错的不是规则本身，是规则的生效日期。** 知道"创业板是 ±20%"
但没把 2020-08-24 这个日期写进条件，回测就会在 2016–2020 那四年半里
把创业板的涨跌停全部判错，而且不会报任何错。
"""

# ---------- 涨跌幅 ----------
#
# 生效日期是这里最容易错的东西：知道规则、却没把日期写进条件，
# 是本项目报告 A7 记的那个错（创业板 4.6 年的涨跌停对引擎不可见）。

CHINEXT_20PCT_FROM = "2020-08-24"   # 创业板注册制改革，±10% → ±20%
STAR_20PCT_FROM = "2019-07-22"      # 科创板开市即 ±20%，无历史切换
ST_10PCT_FROM = "2026-07-06"        # ST 从 ±5% 改为 ±10%
BSE_PCT = 0.30                      # 北交所


def limit_pct(code, is_st, date):
    """当日涨跌停幅度。date 为 'YYYY-MM-DD' 字符串。"""
    if code.startswith("bj."):
        return BSE_PCT
    if is_st:
        return 0.10 if date >= ST_10PCT_FROM else 0.05
    if code.startswith("sh.688"):
        return 0.20
    if code.startswith("sz.3"):
        return 0.20 if date >= CHINEXT_20PCT_FROM else 0.10
    return 0.10


# ---------- 印花税 ----------
#
# (生效日, bps, 是否双边)。倒序，取第一个 date >= eff 的。
# 2008-09-19 改单边出自财税明电[2008]2 号；早于这一天买入侧也要交税，
# 这一条本项目起初漏了，实测年化误差约 1.1%（报告 A11）。

STAMP_DUTY_SCHEDULE = (
    ("2023-08-28", 5.0, False),
    ("2008-09-19", 10.0, False),
    ("2008-04-24", 10.0, True),
    ("2007-05-30", 30.0, True),
)


def _lookup(date):
    if not isinstance(date, str):
        raise TypeError(f"date 必须是 'YYYY-MM-DD' 字符串，收到 {type(date).__name__}")
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise ValueError(f"date 格式应为 YYYY-MM-DD，收到 {date!r}")
    for eff, bps, both in STAMP_DUTY_SCHEDULE:
        if date >= eff:
            return bps, both
    raise ValueError(f"印花税表没有覆盖 {date}（最早 {STAMP_DUTY_SCHEDULE[-1][0]}）")


def stamp_duty_bps_on(date):
    return _lookup(date)[0]


def stamp_is_both_sides(date):
    return _lookup(date)[1]


# ---------- 熔断 ----------
#
# A 股史上仅此一次：机制 2016-01-01 生效、2016-01-08 暂停，其间触发两次。
# 这两天 tradestatus 仍为 1、成交额也不为 0，所以绝大多数面板判它们可交易。

CIRCUIT_BREAKER_DAYS = ("2016-01-04", "2016-01-07")


def is_circuit_breaker(date):
    return date in CIRCUIT_BREAKER_DAYS


# ---------- 板块 ----------

def board(code):
    """返回 主板 / 创业板 / 科创板 / 北交所。"""
    if code.startswith("bj."):
        return "北交所"
    if code.startswith("sh.688"):
        return "科创板"
    if code.startswith("sz.3"):
        return "创业板"
    return "主板"

# ============================================================
# 来自 src/audit_formats.py
# ============================================================
"""把各家回测的输出读成审计器要的形状。

两件事：
1. **代码格式归一化** —— 每个项目用的格式都不同，而这是纯机械的
2. **持仓表 → 成交事件** —— 很多回测只输出逐日持仓，不输出成交明细

A 股代码格式一览（归一到面板口径 sh.600000 / sz.000001 / bj.830799）：

| 来源 | 写法 |
|---|---|
| 本项目面板 | `sh.600000` |
| QMT / Tushare | `600000.SH` |
| **PTrade** | `600000.SS`  ← 上交所是 SS |
| rqalpha / 聚宽 | `600000.XSHG` `000001.XSHE` |
| 通达信 / 部分数据源 | `SH600000` |
| akshare / 裸代码 | `600000` ← 得按号段推市场 |

裸代码推市场的号段（**这是最容易错的一处**）：
    6            → 上交所（含 688 科创板）
    0, 3         → 深交所（含 300 创业板）
    4, 8, 920    → 北交所
北交所的 920 段是 2024 年新增的，很多老代码里没有，会把它错判成上交所。
"""


_XSH = {"XSHG": "sh", "XSHE": "sz", "BJSE": "bj"}
_SUF = {"SH": "sh", "SS": "sh", "SZ": "sz", "SE": "sz", "BJ": "bj"}


def _by_number(num):
    """按号段推市场。北交所 920 段必须在 9 之前判，否则会漏。"""
    if num.startswith("920"):
        return "bj"
    if num[0] == "6":
        return "sh"
    if num[0] in "03":
        return "sz"
    if num[0] in "48":
        return "bj"
    raise ValueError(f"无法从号段判断市场：{num!r}")


def normalize_code(code):
    """任意常见写法 → 面板口径 xx.999999。形状可疑一律抛异常，不静默放行。"""
    if not isinstance(code, str):
        code = str(code)
    s = code.strip().upper().replace(" ", "")
    if not s:
        raise ValueError("空代码")

    if "." in s:
        a, b = s.split(".", 1)
        if a.isdigit():                       # 600000.SH / .SS / .XSHG
            num, tag = a, b
        elif b.isdigit():                     # SH.600000
            num, tag = b, a
        else:
            raise ValueError(f"无法解析：{code!r}")
        mkt = _XSH.get(tag) or _SUF.get(tag) or _SUF.get(tag[:2])
        if mkt is None:
            raise ValueError(f"未知市场标记 {tag!r}（来自 {code!r}）")
    elif s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():   # SH600000
        mkt, num = s[:2].lower(), s[2:]
    elif s.isdigit():                                        # 裸代码
        num, mkt = s, None
    else:
        raise ValueError(f"无法解析：{code!r}")

    num = num.zfill(6)
    if len(num) != 6 or not num.isdigit():
        raise ValueError(f"证券代码应为 6 位数字，得到 {num!r}（来自 {code!r}）")
    if mkt is None:
        mkt = _by_number(num)
    return f"{mkt}.{num}"


def read_trades(path, date_col="date", code_col="code", side_col="side"):
    """成交明细 CSV → 标准列 date/code/side。列名可映射。"""
    d = pd.read_csv(path)
    miss = [c for c in (date_col, code_col, side_col) if c not in d.columns]
    if miss:
        raise ValueError(f"{path} 缺少列 {miss}；实际有 {list(d.columns)}")
    out = pd.DataFrame({
        "date": pd.to_datetime(d[date_col]),
        "code": d[code_col].map(normalize_code),
        "side": d[side_col].astype(str).str.strip().str.lower(),
    })
    bad = set(out["side"]) - {"buy", "sell"}
    # 常见别名
    alias = {"b": "buy", "s": "sell", "买入": "buy", "卖出": "sell",
             "long": "buy", "short": "sell", "1": "buy", "-1": "sell"}
    if bad:
        out["side"] = out["side"].map(lambda x: alias.get(x, x))
        still = set(out["side"]) - {"buy", "sell"}
        if still:
            raise ValueError(f"无法识别的买卖方向：{sorted(still)}")
    return out


def positions_to_trades(path, date_col="date", code_col="code",
                        qty_col="weight"):
    """逐日持仓表 → 成交事件。

    很多回测（qlib 的 positions、以及大量自写回测）只输出逐日持仓，
    没有成交明细。持仓从 0 变正 = 买入，从正变 0 = 卖出。

    **只取 0↔非0 的跃迁，不把加减仓算成新成交** —— 审计器查的是
    "这笔成交在当天可不可能发生"，加仓与开仓在这件事上性质相同，
    但把每次权重微调都算一笔会让分母虚高、占比失真。
    """
    d = pd.read_csv(path)
    for c in (date_col, code_col, qty_col):
        if c not in d.columns:
            raise ValueError(f"{path} 缺少列 {c!r}；实际有 {list(d.columns)}")
    d = pd.DataFrame({"date": pd.to_datetime(d[date_col]),
                      "code": d[code_col].map(normalize_code),
                      "q": pd.to_numeric(d[qty_col], errors="coerce").fillna(0.0)})
    d = d.sort_values(["code", "date"])
    w = d.pivot_table(index="date", columns="code", values="q",
                      aggfunc="sum").fillna(0.0).sort_index()
    held = w.abs() > 1e-12
    prev = held.shift(1).fillna(False).infer_objects(copy=False)
    rows = []
    for side, mask in (("buy", held & ~prev), ("sell", ~held & prev)):
        idx = mask.stack()
        idx = idx[idx].index
        rows += [(dt, code, side) for dt, code in idx]
    if not rows:
        raise ValueError("持仓表里没有任何 0↔非0 的跃迁，推不出成交")
    return (pd.DataFrame(rows, columns=["date", "code", "side"])
            .sort_values(["date", "code"]).reset_index(drop=True))

# ============================================================
# 来自 src/ashare_audit.py
# ============================================================
"""A 股回测的制度规则审计器。

用途：拿一份**别人的**回测成交记录，检查它有没有踩 A 股特有的制度坑。

与通用未来函数检测器（peekahead / Freqtrade 的 lookahead analysis 等）的区别：
那些查的是"代码有没有读到未来的值"，与市场无关。这里查的是
**"A 股这一年的规则是什么，你的回测按的是哪一年的规则"** —— 目前没有工具做这个。

五项检查，每一项都对应本项目真犯过并修好的错：

| 检查 | 查什么 | 出处 |
|---|---|---|
| limit_fill  | 涨停当天算不算买得进、跌停当天算不算卖得出 | 结果 01 |
| limit_rule  | 涨跌幅是否按**生效日期**取（创业板 2020-08-24 等）| 报告 A7 |
| stamp_duty  | 印花税是否分段（2023-08-28 减半、2008-09-19 前双边）| 报告 A11 |
| circuit     | 2016-01-04 / 01-07 熔断日算不算可交易 | 报告 A11 |
| survivorship| 退市股票在不在样本里 | 面板含 241 只已退市 |

输入：成交记录 CSV，至少要有 date, code, side（buy/sell）三列，price 可选。
输出：findings 列表，按严重程度分组。
"""




# 只依赖零依赖的规则副本 —— 审计器要能单独分发，不能拖着整个研究项目走。
# ashare_rules 与 build_panel / backtest 的一致性由 test_ashare_rules 守住。
# （规则已并入本文件）

SEV = ("严重", "中等", "轻微")

# 常见框架的默认假设。用 --framework 直接套，省得用户自己填。
#
# 来源：各项目 main 分支源码，2026-09 读取。**是读源码得到的，不是跑出来的**，
# 用之前请自行核对（每条都给了文件位置，一分钟能查完）。
FRAMEWORK_PROFILES = {
    "qlib": dict(
        limit_pct=0.095, stamp_bps=None,
        note="qlib/config.py 的 REG_CN 默认 limit_threshold=0.095，"
             "一个数套用全部股票、全部年份；exchange.py 里 limit_threshold "
             "是 float 或 (str,str) 表达式对，**没有任何板块或日期逻辑**。"
             "成本 open_cost=0.0015 / close_cost=0.0025 为定值，不随日期变，"
             "所以跨 2023-08-28 的回测印花税一段是错的。",
    ),
    "rqalpha": dict(
        limit_pct=None, stamp_bps=None,
        note="rqalpha 的涨跌停价**从数据源取**（price_board），不是自己按百分比算 —— "
             "架构上是对的，正确性取决于数据包。印花税有日期逻辑："
             "STOCK_PIT_TAX_CHANGE_DATE=2023-08-28，之前 0.001、之后 0.0005，"
             "这一条是对的；但未覆盖 2008-09-19 双边改单边，"
             "且只在卖出侧计税，所以 2008-09-19 之前的样本会漏计买入侧。",
    ),
    "backtrader": dict(
        limit_pct=None, stamp_bps=None,
        note="通用框架，不含任何 A 股制度规则 —— 涨跌停、印花税、T+1 "
             "全部要使用者自己加。不是缺陷，是它本来就不针对 A 股。",
    ),
}


def _f(sev, check, msg, detail=""):
    return dict(severity=sev, check=check, message=msg, detail=detail)


def check_limit_fill(trades, panel):
    """买入日封涨停 / 卖出日封跌停 —— 回测成交了，实盘成交不了。"""
    out = []
    m = panel.set_index(["date", "code"])
    bad_buy = bad_sell = 0
    ex = []
    for t in trades.itertuples():
        key = (t.date, t.code)
        if key not in m.index:
            continue
        row = m.loc[key]
        if t.side == "buy" and bool(row.get("limit_up", False)):
            bad_buy += 1
            if len(ex) < 5:
                ex.append(f"{t.date.date()} {t.code} 买入，但当日封涨停")
        if t.side == "sell" and bool(row.get("limit_down", False)):
            bad_sell += 1
            if len(ex) < 5:
                ex.append(f"{t.date.date()} {t.code} 卖出，但当日封跌停")
    n = len(trades)
    if bad_buy or bad_sell:
        pct = (bad_buy + bad_sell) / max(n, 1)
        out.append(_f("严重" if pct > 0.01 else "中等", "limit_fill",
                      f"{bad_buy + bad_sell} 笔成交发生在封板日（占 {pct:.2%}）"
                      " —— 回测成交了，实盘成交不了，收益被高估",
                      "；".join(ex)))
    return out


def check_limit_rule(trades, assumed_pct=None):
    """回测若对全样本用同一个涨跌幅，跨过生效日的那部分就是错的。"""
    out = []
    if assumed_pct is None:
        return out
    # 按**偏离幅度**分级，不是一律报严重。
    #
    # 这一条是跑演示时自己发现的：qlib 的默认值 0.095 是对 ±10% 的安全余量
    # （怕四舍五入），把它和"创业板实际 ±20% 却按 ±9.5% 算"报成同一级，
    # 等于用大量噪声把真问题淹掉 —— 正是本项目报告 A14 记的那种失效。
    RELATIVE_TOL = 0.10          # 偏离在 10% 以内视为取整余量
    big, small = {}, 0
    for t in trades.itertuples():
        d = str(t.date.date())
        correct = limit_pct(t.code, getattr(t, "is_st", False), d)
        rel = abs(correct - assumed_pct) / correct
        if rel <= 1e-9:
            continue
        if rel <= RELATIVE_TOL:
            small += 1
        else:
            big.setdefault((t.code[:5], d[:4], correct), 0)
            big[(t.code[:5], d[:4], correct)] += 1
    if big:
        tot = sum(big.values())
        ex = "；".join(f"{k[0]}* {k[1]}年 实际 ±{k[2]:.0%}（{v} 笔）"
                       for k, v in sorted(big.items(), key=lambda x: -x[1])[:5])
        out.append(_f("严重", "limit_rule",
                      f"{tot} 笔成交的涨跌幅假设（±{assumed_pct:.1%}）与当日实际规则"
                      f"差出 {RELATIVE_TOL:.0%} 以上", ex))
    if small:
        out.append(_f("轻微", "limit_rule",
                      f"另有 {small} 笔的假设（±{assumed_pct:.1%}）与实际相差在 "
                      f"{RELATIVE_TOL:.0%} 以内",
                      "多半是故意留的取整余量，不是规则用错"))
    return out


def check_stamp_duty(trades, assumed_bps=None):
    """印花税按当期税率回溯全样本 —— 2023-08-28 之前差一倍，2008-09-19 之前还差一边。"""
    out = []
    if assumed_bps is None:
        return out
    err_sell = err_buy = 0
    for t in trades.itertuples():
        d = str(t.date.date())
        if t.side == "sell":
            if abs(stamp_duty_bps_on(d) - assumed_bps) > 1e-9:
                err_sell += 1
        elif stamp_is_both_sides(d):
            err_buy += 1          # 买入侧本应征税，回测多半没算
    msgs = []
    if err_sell:
        msgs.append(f"{err_sell} 笔卖出用了错误税率（假设 {assumed_bps:.1f}bp）")
    if err_buy:
        msgs.append(f"{err_buy} 笔买入发生在 2008-09-19 之前（当时双边征收），"
                    "回测若只在卖出侧计税则漏计")
    if msgs:
        out.append(_f("严重", "stamp_duty", "；".join(msgs),
                      "税率表：2023-08-28 起 5bp 单边；2008-09-19~2023-08-27 "
                      "10bp 单边；2008-04-24~2008-09-18 10bp 双边；"
                      "2007-05-30~2008-04-23 30bp 双边"))
    return out


def check_circuit_breaker(trades):
    out = []
    hit = [t for t in trades.itertuples()
           if str(t.date.date()) in CIRCUIT_BREAKER_DAYS]
    if hit:
        out.append(_f("严重", "circuit",
                      f"{len(hit)} 笔成交发生在熔断日 {CIRCUIT_BREAKER_DAYS}",
                      "2016-01-04 下午、2016-01-07 全天（只交易了 29 分钟）"
                      "全市场已停，这两天 tradestatus 仍为 1、成交额也不为 0，"
                      "所以绝大多数面板会判它们可交易"))
    return out


def check_survivorship(trades, panel):
    """样本里一只退市股都没有 → 几乎一定是幸存者偏差。"""
    out = []
    traded = set(trades["code"])
    last = panel.groupby("code")["date"].max()
    panel_end = panel["date"].max()
    delisted = set(last.index[last < panel_end - pd.Timedelta(days=30)])
    if not delisted:
        out.append(_f("中等", "survivorship",
                      "对照面板里没有任何已退市股票 —— 无法判断",
                      "换一份含退市股的面板再查"))
        return out
    n = len(traded & delisted)
    if n == 0:
        out.append(_f("严重", "survivorship",
                      f"成交记录覆盖 {len(traded)} 只股票，其中已退市 0 只；"
                      f"而同期全市场有 {len(delisted)} 只退市",
                      "只在活到今天的股票上回测，会系统性高估收益"))
    return out


def run(trades, panel, assumed_limit_pct=None, assumed_stamp_bps=None):
    f = []
    f += check_limit_fill(trades, panel)
    f += check_limit_rule(trades, assumed_limit_pct)
    f += check_stamp_duty(trades, assumed_stamp_bps)
    f += check_circuit_breaker(trades)
    f += check_survivorship(trades, panel)
    return sorted(f, key=lambda x: SEV.index(x["severity"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="A 股回测的制度规则审计")
    ap.add_argument("trades", help="成交记录 CSV：date, code, side[, price]")
    ap.add_argument("--panel", default="data/panel.parquet",
                    help="对照面板 parquet，需含 date/code/limit_up/limit_down/is_st 列")
    ap.add_argument("--assumed-limit-pct", type=float, default=None,
                    help="被审回测假设的涨跌幅，如 0.10")
    ap.add_argument("--assumed-stamp-bps", type=float, default=None,
                    help="被审回测假设的印花税，如 5")
    ap.add_argument("--framework", choices=sorted(FRAMEWORK_PROFILES),
                    help="直接套用某框架的默认假设")
    ap.add_argument("--positions", action="store_true",
                    help="输入是逐日持仓表而非成交明细，由 0↔非0 跃迁推成交")
    ap.add_argument("--date-col", default="date")
    ap.add_argument("--code-col", default="code")
    ap.add_argument("--side-col", default="side")
    ap.add_argument("--qty-col", default="weight",
                    help="仅 --positions：权重或股数列")
    a = ap.parse_args(argv)

    if a.framework:
        prof = FRAMEWORK_PROFILES[a.framework]
        if a.assumed_limit_pct is None:
            a.assumed_limit_pct = prof["limit_pct"]
        if a.assumed_stamp_bps is None:
            a.assumed_stamp_bps = prof["stamp_bps"]
        print(f"框架档案 {a.framework}：{prof['note']}\n")

    if a.positions:
        tr = positions_to_trades(a.trades, a.date_col, a.code_col, a.qty_col)
        print(f"由持仓表推出 {len(tr)} 笔成交（只取 0↔非0 的跃迁）\n")
    else:
        tr = read_trades(a.trades, a.date_col, a.code_col, a.side_col)
    pan = pd.read_parquet(a.panel, columns=["date", "code", "limit_up",
                                            "limit_down", "is_st"])
    fs = run(tr, pan, a.assumed_limit_pct, a.assumed_stamp_bps)
    print(f"审计 {len(tr)} 笔成交，{tr['code'].nunique()} 只股票，"
          f"{tr['date'].min().date()} ~ {tr['date'].max().date()}\n")
    if not fs:
        print("未发现问题。（注意：未发现不等于没有 —— 本工具只查这五项）")
        return
    cur = None
    for x in fs:
        if x["severity"] != cur:
            cur = x["severity"]
            print(f"【{cur}】")
        print(f"  [{x['check']}] {x['message']}")
        if x["detail"]:
            print(f"      {x['detail']}")



if __name__ == "__main__":
    main()
