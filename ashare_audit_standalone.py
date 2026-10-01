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

from decimal import ROUND_HALF_UP, Decimal

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


# ---------- 新股上市初期：不设涨跌幅 ----------
#
# 注册制下新股**上市前 5 个交易日不设涨跌幅限制**（盘中有 30%/60% 临时停牌，
# 但那不是价格上限）。第 6 个交易日起才按板块的常规幅度。
#
# 各板块开始适用的日期不同：
REGISTRATION_FROM = {
    "科创板": "2019-07-22",      # 开市即注册制
    "创业板": "2020-08-24",      # 注册制改革
    "主板": "2023-04-10",        # 全面注册制首批上市
}
NO_LIMIT_DAYS = 5               # 上市后前 5 个交易日

# 为什么这条必须有：不加它，新股上市头几天会被算出一个涨停价，
# 而那几天股价可以翻倍 —— 于是要么凭空判出"封板"，要么把真实的
# 大幅波动当成异常。这是审计器发布之后才发现的一个自身 bug。
#
# **规则已写进这里，但审计器还没用上它**，说清楚免得被当成已修：
# 参考面板的构建脚本（make_reference_panel.py）会用它清掉新股前 5 日的
# 封板标记；而 ashare_audit 的 check_limit_rule **拿不到上市天数** ——
# 成交记录里没有这一列、面板里也没有、CLI 也没有入口。所以审计器对
# 新股前 5 个交易日仍然会说"实际 ±10%"。这不是接一下线就能修的，
# 要么面板加一列上市天数，要么这一项永远只在面板侧生效。


def has_price_limit(code, date, days_since_listing=None):
    """当日是否**存在**涨跌幅限制。

    days_since_listing：上市后的第几个交易日（上市首日 = 1）。
    **传 None 表示不知道** —— 那就按"有限制"处理（保守），因为误判成
    "无限制"会让真实的封板漏掉，而封板漏掉正是本工具要查的东西。
    """
    if days_since_listing is None:
        return True
    if days_since_listing > NO_LIMIT_DAYS:
        return True
    eff = REGISTRATION_FROM.get(board(code))
    if eff is None:
        # 北交所新股上市初期的安排我没核到原文 —— 按"有限制"处理（保守），
        # 不照着沪深猜。原来这里是 REGISTRATION_FROM[...]，北交所代码直接
        # KeyError，是代码审查抓出来的。
        return True
    return date < eff          # 注册制生效之前，老规则仍有涨跌幅


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


# ---------- 其余交易费用 ----------
#
# 来源：券商官方费用公示（2023-09-13 生效版本）。**只覆盖当前档，不含历史**
# —— 过户费的历史沿革我没有核到一手资料，所以这里不编造日期表。
# 印花税是唯一做了历史分段的一项（见上），因为那几档有明确的财税文件。
#
# 单位统一成"占成交金额的比例"。
TRANSFER_FEE_RATE = 0.00001      # 过户费 0.001%，**双边**，沪深统一

# 经手费有历史分段，而且**减半日和印花税是同一天**。
# 出处（一手）：《上海证券交易所关于调整股票交易经手费收费标准的通知》——
#   A 股由成交金额的 **0.00487% 双向** 下调为 **0.00341% 双向**，
#   **自 2023-08-28 起**（深交所同步、同比例；北交所降 50%，未核其费率）。
# 这个日期与印花税 0.1% → 0.05% 是同一天（同批降费政策）。
#
# **0.00487% 这一档本身从哪天开始，我没核到原文**，所以它被用于 2023-08-28 之前的
# 全部日期 —— 这是"手上最好的已知值"，不是"已核的历史全貌"。
# 跨 2008 年之前的样本不要依赖这个数。
HANDLING_FEE_SCHEDULE = (
    ("2023-08-28", 0.0000341),   # 0.00341%
    ("0000-01-01", 0.0000487),   # 0.00487%，起始日未核
)

# 证管费：《关于调整上海证券市场证券交易监管费收费标准的通知》
#   按交易额 0.04‰ → **0.02‰**，**自 2012-01-01 起**（清算参数 2012-09-01 起调整）。
# 注意：这份通知**没有写明单边还是双边**；"双边"的口径来自券商费用公示。
REGULATORY_FEE_SCHEDULE = (
    ("2012-01-01", 0.00002),     # 0.02‰
    ("0000-01-01", 0.00004),     # 0.04‰
)

# 当前档，保留给不关心历史的调用方（等于各自 schedule 的第一项）
HANDLING_FEE_RATE = HANDLING_FEE_SCHEDULE[0][1]
REGULATORY_FEE_RATE = REGULATORY_FEE_SCHEDULE[0][1]


def _rate_on(schedule, date, what):
    if not isinstance(date, str):
        raise TypeError(f"date 必须是 'YYYY-MM-DD' 字符串，收到 {type(date).__name__}")
    for eff, rate in schedule:
        if date >= eff:
            return rate
    raise ValueError(f"{what}费率表没有覆盖 {date}")


def handling_fee_rate_on(date):
    """经手费率（单边，占成交金额）。2023-08-28 起 0.00341%，之前 0.00487%。"""
    return _rate_on(HANDLING_FEE_SCHEDULE, date, "经手")


def regulatory_fee_rate_on(date):
    """证管费率（单边，占成交金额）。2012-01-01 起 0.02‰，之前 0.04‰。"""
    return _rate_on(REGULATORY_FEE_SCHEDULE, date, "证管")
MIN_COMMISSION_YUAN = 5.0        # 佣金起点 5 元/笔（券商普遍）
MAX_COMMISSION_RATE = 0.003      # 佣金上限 0.3%


def regulatory_cost_rate(date, side):
    """**不含券商佣金**的监管类费用合计（占成交金额）。

    这是任何人都躲不掉的地板，而多数回测只算了佣金和印花税，
    漏掉过户费、经手费、证管费 —— 三项双边合计约 0.0064%，
    一轮买卖约 1.3bp。单看不大，高换手策略上会累积。
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side 应为 buy/sell，收到 {side!r}")
    r = (TRANSFER_FEE_RATE + handling_fee_rate_on(date)
         + regulatory_fee_rate_on(date))
    if side == "sell":
        r += stamp_duty_bps_on(date) / 1e4
    elif stamp_is_both_sides(date):
        r += stamp_duty_bps_on(date) / 1e4
    return r


def commission_on(notional, rate, min_yuan=MIN_COMMISSION_YUAN):
    """实际佣金 —— **起点 5 元**是小额交易上最大的一项成本失真。

    10,000 元的单子按万 2.5 只有 2.5 元，但实际收 5 元，实际费率翻倍；
    5,000 元的单子实际费率是名义的 4 倍。回测用固定 bps 时，
    资金越小、单笔越小，低估得越厉害。
    """
    if notional < 0:
        raise ValueError("成交金额不能为负")
    return max(notional * rate, min_yuan) if notional > 0 else 0.0


# ---------- 熔断 ----------
#
# A 股史上仅此一次：机制 2016-01-01 生效、2016-01-08 暂停，其间触发两次。
# 这两天 tradestatus 仍为 1、成交额也不为 0，所以绝大多数面板判它们可交易。

CIRCUIT_BREAKER_DAYS = ("2016-01-04", "2016-01-07")


def is_circuit_breaker(date):
    return date in CIRCUIT_BREAKER_DAYS


# ---------- 申报数量：整手、递增单位、单笔上限 ----------
#
# 出处（一手）：
#   《上海证券交易所交易规则（2023 年修订）》
#     3.3.8 买入申报数量应当为 100 股（份）或其整数倍；
#           卖出时余额不足 100 股（份）的部分应当一次性申报卖出
#     3.3.9 单笔申报最大数量不超过 100 万股（份）
#     3.3.11 A 股申报价格最小变动单位 0.01 元
#   《深圳证券交易所交易规则》
#     3.3.8 同上（买入 100 股整数倍、卖出零股一次性申报）
#     3.3.10 单笔申报最大数量不超过 100 万股（份）
#     3.3.13 A 股申报价格最小变动单位 0.01 元
#     —— 沪深主板各有自己的条款，不是拿上交所的往深市套
#   《上海证券交易所科创板股票交易特别规定》第二十条
#     限价申报 ≥200 股且 ≤10 万股；市价申报 ≥200 股且 ≤5 万股；
#     卖出余额不足 200 股的部分一次性申报卖出
#   《深圳证券交易所创业板交易特别规定》2.8
#     限价申报 ≤30 万股；市价申报 ≤15 万股（盘后定价 ≤100 万股，本表不含）
#
# **1 股递增只在科创板和北交所**：沪深主板与创业板买入仍须 100 股的整数倍。
# 科创板那半句有原文（特别规定第二十条）；主板"100+1"的说法在交易所规则
# 原文里没有找到对应条款，所以下面按规则原文的 100 股整数倍处理。
#
# **北交所的申报数量规则我一条原文都没核到**，所以下面的表里干脆没有北交所。
# 不是忘了 —— 填一个"看起来对"的数（比如照主板抄 100 万股）正是这个项目
# 反复改掉的那类错：把一处规则推广到没查过的地方，而且不留痕迹。
# 查不到就不判，order_shares_ok() 对北交所返回 None（"未核"），不返回 True。
#
# 这一组规则只有在回测输出**股数**时才查得动。绝大多数回测输出的是权重，
# 那种情况下审计器不猜，直接跳过并说明为什么跳过。

TICK_SIZE_YUAN = 0.01          # A 股申报价格最小变动单位


# ---------------------------------------------------------------------------
# 涨跌停价的**规范实现**（标量）。
#
# 为什么放在这里、而不是放在用它的那个模块里：这套整数分算术在
# build_panel（向量化，1100 万行）和 pretrade_check（逐笔下单前）里各要用一次。
# 同一条规则两套算法，迟早在某一侧差 1 分而没人发现 —— 所以规范版在这里，
# 向量化那版必须和它做平价校验（见 test_limit_band_parity.py）。
#
# 恒等式：half_up(cents * num / den) == (2*cents*num + den) // (2*den)
# 在本项目 1100 万行面板上实测 0 处不一致。
#
# **不能用 round()**：numpy/pandas 的 round 是五成双（banker's），交易所是四舍五入。
# 实测：±10% 的涨跌停价在 1100 万行里 543,479 行（4.92%）不一致，而且
# **100% 单向** —— round() 总是把涨停价算低 1 分，于是所有涨停票都变成"可买"。
# ±20% 一行都不差：×1.2 = 12k/10，第三位小数只会是 {0,2,4,6,8}，永远不是 5。
# 这条"±20% 没有差异"也在 test_limit_band_parity 里独立验过，不是推理。
# ---------------------------------------------------------------------------


def to_cents(yuan):
    """元 → 整数分，四舍五入（不是 round() 的五成双）。

    走 `Decimal(repr(x))`，不走 `int(x*100+0.5)`，也不走两段舍入：
      - `int(x*100+0.5)`：实测 0.005~300.005 的 30,000 个半分价里错 1,851 个
        （6.17%）—— 浮点乘法把 x.xx5 压到半分以下时，加 0.5 进不上去；
      - 先舍到毫、再舍到分：18.0049999 会给 18.01，经典的双重舍入错误；
      - `round(x*100)`：错 15,000 个（正好一半），五成双。
    `repr()` 给的是能唯一还原该 float 的最短十进制串，所以字面量的十进制意图
    被保住，再用十进制 ROUND_HALF_UP 落到分。

    **用在前收这种 2 位小数的价格上时，上面这些差别一个都碰不到**
    （2 位小数的价格在 float64 乃至 float32 里都没有半分歧义）。
    真正用得上的是策略自己算出来的委托价 —— 那种数带一长串小数，x.xx5 是常态。
    """
    if isinstance(yuan, Decimal):
        d = yuan
    elif isinstance(yuan, str):
        d = Decimal(yuan)
    else:
        d = Decimal(repr(float(yuan)))
    return int(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def limit_price_band_cents(preclose, code, is_st, date, days_since_listing=None):
    """当日可申报价格区间 [跌停价, 涨停价]，单位=**整数分**。

    无涨跌幅限制（注册制新股上市前 5 个交易日）返回 (None, None) ——
    不是返回一个很宽的区间：「有区间」和「没有区间」在下游是两件事。

    **参考价必须是数据商给的 preclose（除权参考价），不是前一天的收盘价。**
    除权日这两者能差一倍以上（见上面 is_ex_rights 处记的实测例子）。
    注意这条只对**未复权**序列成立：复权序列里 close.shift(1) 本身就等于
    除权参考价（复权因子恒等式），本项目实测到 2.3e-07。
    """
    if not has_price_limit(code, date, days_since_listing):
        return None, None
    c = to_cents(preclose)
    if c <= 0:
        raise ValueError("preclose 必须为正")
    pct = limit_pct(code, is_st, date)
    num_up = round((1 + pct) * 100)      # 110 / 120 / 105 / 130
    num_dn = round((1 - pct) * 100)      # 90 / 80 / 95 / 70
    high = (2 * c * num_up + 100) // 200
    low = (2 * c * num_dn + 100) // 200
    return int(low), int(high)

MIN_ORDER_SHARES = {"主板": 100, "创业板": 100, "科创板": 200}
LOT_INCREMENT = {"主板": 100, "创业板": 100, "科创板": 1}
MAX_ORDER_SHARES = {
    "主板": {"limit": 1_000_000, "market": 1_000_000},
    "创业板": {"limit": 300_000, "market": 150_000},
    "科创板": {"limit": 100_000, "market": 50_000},
}
UNVERIFIED_QTY_BOARDS = ("北交所",)     # 原文未核，一律不判


def min_order_shares(code):
    """买入的最小申报数量（股）；未核的板块返回 None。"""
    return MIN_ORDER_SHARES.get(board(code))


def lot_increment(code):
    """最小申报数量之上的递增单位（股）；未核的板块返回 None。"""
    return LOT_INCREMENT.get(board(code))


def max_order_shares(code, order_type="limit"):
    """单笔申报数量上限（股）；未核的板块返回 None。"""
    if order_type not in ("limit", "market"):
        raise ValueError("order_type 只能是 'limit' 或 'market'")
    t = MAX_ORDER_SHARES.get(board(code))
    return None if t is None else t[order_type]


def order_shares_ok(code, shares, side="buy", order_type="limit"):
    """这个股数能不能报进交易所。

    返回 (ok, 原因)，**ok 是三态**：
      True  —— 合规
      False —— 违规，原因在第二项
      None  —— 该板块的规则我没核到原文，不判（目前只有北交所）

    None 不是 True。审计器把它单独统计、单独提示，不当成"通过"。

    卖出侧比买入宽：持仓里不足一手的零股**必须**一次性卖出，所以
    "卖 37 股"是合法的、"买 37 股"不是。代价是**卖出侧只查上限、
    不查整手** —— 持仓 5000 股卖 150 股（违规）这种查不出来，
    因为审计器看不到持仓数量。这是刻意取舍，不是遗漏。
    """
    if side not in ("buy", "sell"):
        raise ValueError("side 只能是 'buy' 或 'sell'")
    if shares is None or shares != shares:          # None / NaN / pd.NA
        return None, "申报数量缺失，不判"
    try:
        x = float(shares)
    except (TypeError, ValueError):
        return None, f"申报数量不是数字（{shares!r}），不判"
    if x != x or x in (float("inf"), float("-inf")):
        return None, "申报数量不是有限数，不判"
    if x <= 0:
        return False, "申报数量必须为正"
    n = round(x)
    # 容差：权重换算出来的股数常带浮点尾差（300*1.0000000000000002），
    # 严格 != int 会把它判成"非整数股"。真正的非整数（100.5）仍要拦。
    if abs(x - n) > 1e-6 * max(1.0, abs(x)):
        return False, "申报数量必须是整数股"
    shares = int(n)
    if board(code) in UNVERIFIED_QTY_BOARDS:
        return None, f"{board(code)}的申报数量规则未核到原文，不判"
    cap = max_order_shares(code, order_type)
    if cap is not None and shares > cap:
        return False, f"超过单笔申报上限 {cap:,} 股（{board(code)}，{order_type}）"
    if side == "sell":
        return True, ""                              # 零股必须一次性卖出
    lo = min_order_shares(code)
    if shares < lo:
        return False, f"低于最小申报数量 {lo} 股（{board(code)}）"
    step = lot_increment(code)
    if step > 1 and shares % step:
        return False, f"买入须为 {step} 股的整数倍（{board(code)}）"
    return True, ""


def price_on_tick(price, tick=TICK_SIZE_YUAN, rel_tol=1e-6):
    """申报价格是否落在最小变动单位上。

    **容差必须是相对的，而且要留到 1e-6 这一档**，两个原因：

    1. 合法价格自己就带累积误差。0.07*3 在 float 里是
       0.21000000000000002，8.8*1.15 是 10.12 差一点 —— 按复权因子
       还原出来的价格全是这种数，严格相等会把它们全判成违规。
    2. **float32**。很多面板（包括本项目自己的）价格列是 float32，
       10 元量级的表示误差约 2e-7。容差如果按 1e-9 这种绝对量级定，
       一份完全合法的 float32 价格表会被判成 100% 违规 ——
       这个坑是代码审查抓出来的，原来的实现就是这么写的。

    真正要拦的是分以下的价格（33.333333、4.755），它们离最近的分
    有 1e-3 量级的距离，与上面两种误差差着三个数量级。
    """
    if price is None or price != price:
        return False
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False
    if not (p > 0) or p in (float("inf"), float("-inf")):
        return False
    n = round(p / tick)
    return abs(p - n * tick) <= rel_tol * max(tick, abs(p))


# ---------- 风险警示板 / 退市整理期 ----------
#
# 出处（一手规则原文）：《上海证券交易所风险警示板股票交易管理办法》
#   （2013-01-01 实施，此后 2015-01-30 / 2017-06-28 / 2018-08-06 /
#     2020-05 / 2020-12 五次修订）
#   第七条 风险警示股票价格涨跌幅限制为 5%；退市整理期股票涨跌幅限制为 10%
#   第八条 退市整理期首个交易日无价格涨跌幅限制
#   第十四条 投资者当日通过竞价交易和大宗交易**累计**买入的单只
#           风险警示股票，数量不得超过 50 万股
#
# 这里有一个回测几乎必错的地方：**退市整理期的股票带 ST 标记，但涨跌幅是
# ±10% 不是 ±5%**。把"风险警示 → ±5%"一刀切，会把退市整理期那十几天
# 的涨跌停全判错。limit_pct() 覆盖不了这一条（它只看 is_st）。
#
# **下面的函数写了规则，但审计器没有调用它**，写明免得当成已修：判定需要
# "这一天是不是这只股票的退市整理期、是不是首日"，而免费面板只有股票的
# **当前**名称，用当前名称去标历史会把这只股票的整段历史全标错。
# 换句话说：规则是对的，数据不够，所以不判 —— 不是忘了接。
#
# 未核的部分，写清楚免得当成已核：
#   - 退市整理期的**长度**。手上的一手材料是 2022 年一份退市公告，写的是
#     "十五个交易日"；更早的规则版本是 30 个交易日，具体切换日期我没查到
#     原文，所以不在代码里写长度常量。
#   - 深交所是否有同样的 50 万股/日上限。上面这份是**上交所**的办法，
#     深交所的对应规定我没核，所以 st_daily_buy_cap() 只对 sh. 返回上限。

ST_DAILY_BUY_CAP_SHARES = 500_000     # 上交所：单账户单日买入单只风险警示股
ST_DAILY_BUY_CAP_FROM = "2013-01-01"
DELISTING_PERIOD_PCT = 0.10           # 退市整理期，首日除外


def st_daily_buy_cap(code, date):
    """单账户单日买入单只风险警示股票的股数上限；无上限或未核时返回 None。"""
    if not code.startswith("sh."):
        return None                    # 深交所对应规定未核，不假设
    if date < ST_DAILY_BUY_CAP_FROM:
        return None
    return ST_DAILY_BUY_CAP_SHARES


def delisting_period_limit_pct(is_first_day):
    """退市整理期的涨跌幅：首日无限制（None），其后 ±10%。"""
    return None if is_first_day else DELISTING_PERIOD_PCT


# ---------- 卖空：A 股卖不出手里没有的票 ----------
#
# 出处（一手）：《上海证券交易所融资融券交易实施细则（2023 年修订）》
#   第二条 融券交易是"借入证券并卖出"的行为 —— 先借到券，才能卖
#   第二十三条、第三十条 只能融券卖出交易所公布的**标的证券名单**内的证券，
#           会员向客户公布的名单不得超出本所范围
#   第十二条 **融券卖出的申报价格不得低于该证券的最新成交价**；
#           当天没有成交的，不得低于前收盘价（即"提价规则"）
#
# 对回测的意义，这一条被忽略的程度和涨跌停差不多：
#   1. **裸卖空在 A 股不存在。** 卖出一只手里没有的票，在 A 股是报不进去的
#      委托，不是"成本高一点"的交易。多空对冲、截面做空的回测如果直接对
#      因子低分组下空单，那半边收益整个是不存在的。
#   2. 走融券也有三道门：标的名单、券源（有没有票可借）、以及提价规则 ——
#      提价规则意味着**下跌途中你往往卖不出去**，恰恰是最想做空的时候。
#
# 券源和标的名单是逐日变化的数据，免费数据源拿不到，所以审计器只查第 1 条
# （手里没有的票不能卖），查得到就是硬伤；查不到的那两条写在 RULE_INVENTORY。

SHORT_UPTICK_RULE = True         # 融券卖出不得低于最新成交价


# ---------- 投资者适当性：板块权限 ----------
#
# 出处：上交所科创板投资者适当性材料（个人投资者：申请权限开通前 20 个交易日
# 证券账户及资金账户内资产**日均不低于 50 万元**，且**参与证券交易 24 个月
# 以上**）；北交所同为 50 万 + 24 个月；深交所创业板为 10 万 + 24 个月。
#
# 创业板那一档针对注册制改革后**新开通**权限的投资者，改革前已开通的不受影响 ——
# 具体生效日我没核到原文，所以这里不写日期常量，只写门槛。
#
# 为什么审计器要管这个：策略池里有科创板，就等于假设账户有科创板权限，
# 也就等于假设账户资产曾经 20 个交易日日均 50 万以上。用 10 万本金回测
# 一个含科创板的策略，前提本身不成立 —— 这不是成本问题，是权限问题。

BOARD_ACCESS_THRESHOLD_YUAN = {"主板": 0, "创业板": 100_000,
                               "科创板": 500_000, "北交所": 500_000}
BOARD_ACCESS_MONTHS = 24


def board_access_threshold(code):
    """交易该板块所需的账户资产门槛（元，20 个交易日日均）。"""
    return BOARD_ACCESS_THRESHOLD_YUAN[board(code)]


# ---------- 股息红利差别化个人所得税 ----------
#
# 出处（一手）：《财政部 国家税务总局 证监会关于上市公司股息红利差别化个人
# 所得税政策有关问题的通知》财税〔2015〕101 号，**2015-09-08 起**：
#   持股 1 个月以内（含）        股息红利全额计入应纳税所得额 → 实际税率 20%
#   持股 1 个月以上至 1 年（含）  减按 50% 计入               → 实际税率 10%
#   持股超过 1 年                暂免征收个人所得税           → 实际税率 0%
# 调整前（财税〔2012〕85 号，2013-01-01 起）超过 1 年的按 25% 计入 → 5%。
#
# **这一条是复权价回测里一个系统性的、谁都不算的高估。** 前/后复权价把分红
# 按 100% 还原进价格序列，等于假设股息免税；而月度换仓的策略实际只拿到 80%。
# A 股全市场股息率量级 2%，20% 的税就是每年 0.4% —— 和手续费同一个数量级，
# 但手续费人人都算，这个没人算。
#
# 2013-01-01 之前的档我没核到原文，所以 dividend_tax_rate() 对更早的日期
# 返回 None（不判），不编一个数填上去。

DIVIDEND_TAX_SCHEDULE = (
    # (生效日, 1个月以内, 1个月~1年, 超过1年)
    ("2015-09-08", 0.20, 0.10, 0.00),
    ("2013-01-01", 0.20, 0.10, 0.05),
)
ONE_MONTH_DAYS = 30
ONE_YEAR_DAYS = 365


def dividend_tax_rate(holding_days, date):
    """个人投资者股息红利的实际税率；date 早于 2013-01-01 返回 None（未核）。

    holding_days 用自然日。税务口径按「1 个月」、「1 年」表述，这里用 30 / 365
    近似 —— 边界上会差一两天，但策略的持仓期通常离边界很远，
    真正贴着边界做税务套利的不是回测该管的事。
    """
    if holding_days < 0:
        raise ValueError("持股天数不能为负")
    for eff, r_short, r_mid, r_long in DIVIDEND_TAX_SCHEDULE:
        if date >= eff:
            if holding_days <= ONE_MONTH_DAYS:
                return r_short
            if holding_days <= ONE_YEAR_DAYS:
                return r_mid
            return r_long
    return None


# ---------- 除权除息日：涨跌停的参考价不是前一天的收盘价 ----------
#
# 交易所算涨跌停价用的是**除权参考价**，不是原始的前收盘。数据商一般直接给一个
# `preclose` 字段，它已经做过除权调整（baostock 在 adjustflag=3 下就是这样）。
# 很多引擎图方便写成 `close.shift(1)`，在除权日就是错的。
#
# 实测的例子（baostock，2026-10 核过）：sh.600000 2017-05-25 除权，
#   preclose = 11.75（除权参考价），原始前收 = 15.47
#   用 preclose 算涨停价 ≈ 12.93（当天实际就封在 12.93）
#   用 close.shift(1) 算出来是 17.02 —— 封板判定完全反过来
# 送转日更夸张：原始价能跳 50% 以上。
#
# 这个函数做的事很小：给定数据商的 preclose 和前一行的 close，判断这一天是不是
# 除权除息/送转日。它放在规则模块里而不是下载脚本里，因为这是一条**规则**
# （参考价怎么取），而且这样它才有测试覆盖 —— 下载脚本没法在没有网的环境里测。

EX_RIGHTS_TOL = 0.005        # 半分；两者差到半分以上才算除权


def is_ex_rights(preclose, prev_close, tol=EX_RIGHTS_TOL):
    """这一天是否除权除息/送转。

    preclose：数据商给的、已做除权调整的昨收
    prev_close：前一个交易日的实际收盘价
    任一为缺失（如上市首日没有前一行）→ 返回 False，**不猜**。
    """
    if preclose is None or prev_close is None:
        return False
    if preclose != preclose or prev_close != prev_close:      # NaN
        return False
    try:
        return abs(float(preclose) - float(prev_close)) > tol
    except (TypeError, ValueError):
        return False

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
    # price 是可选列 —— 有它才能查复权口径，没有就跳过那一项，不猜
    for cand in ("price", "成交价", "deal_price", "fill_price"):
        if cand in d.columns:
            out["price"] = pd.to_numeric(d[cand], errors="coerce")
            break
    # qty 同理：有股数才查得了整手/上限，没有就跳过。
    # **不认 amount／金额**：那是成交额不是股数，混进来会让整手检查全错。
    # **不认 volume/vol/成交量**：A 股行情里这几个名字既可能是市场总成交量、
    # 也常以"手"为单位，认错单位会让整手检查 100% 误报。宁可不查。
    for cand in ("qty", "shares", "quantity", "股数", "成交股数", "委托数量"):
        if cand in d.columns:
            out["qty"] = pd.to_numeric(d[cand], errors="coerce")
            break
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

十八项检查。绝大多数对应本项目自己真犯过并修好的错 ——
这不是"我想到可能有坑"的清单，是"我掉进去过"的清单。

| 检查 | 查什么 | 出处 |
|---|---|---|
| limit_fill | 涨停当天算不算买得进、跌停当天算不算卖得出 | 结果 01 |
| open_limit_fill | 开盘一字板；与上一项**互补，不能相加** | 结果 01 |
| limit_rule | 涨跌幅是否按**生效日期**取（创业板 2020-08-24 等）| 报告 A7 |
| stamp_duty | 印花税是否分段（2023-08-28 减半、2008-09-19 前双边）| 报告 A11 |
| circuit | 2016-01-04 / 01-07 熔断日算不算可交易 | 报告 A11 |
| t1 | 当日买入当日卖出 | A 股 T+1 |
| suspension | 停牌日成交 | |
| held_through_suspension | 持仓穿越停牌期 | 实测复牌日均值 +3.34% |
| price_convention | 成交价是复权价还是原始价 | |
| cost_floor | 成本假设是否低于监管费用地板、有没有佣金起点 | |
| order_size | 整手、单笔申报上限（需 qty 列）| |
| tick_size | 成交价是否落在 0.01 元上（需 price 列）| |
| st_buy_cap | 风险警示股单日买入 50 万股上限（需 qty 列）| |
| naked_short | 卖出手里没有的票 —— **A 股裸卖空不存在** | |
| board_permission | 科创板/北交所需 50 万、创业板 10 万的账户权限门槛 | |
| dividend_tax | 复权价按免税算分红，个人实际要交 20%/10% | |
| ex_rights | 成交落在除权日（**仅未复权回测适用**）| |
| survivorship | 退市股票在不在样本里 | 面板含 241 只已退市 |

哪些规则查了、哪些没查、没查的原因，逐条写在发布包的 RULE_INVENTORY.md。

输入：成交记录 CSV，至少要有 date, code, side（buy/sell）三列；
      price（成交价）与 qty（股数）可选，缺哪列就跳过对应检查并明说"没查"。
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


def check_open_limit_fill(trades, panel):
    """按**开盘价**成交时的可成交性 —— 与 `limit_fill`（按收盘）互补。

    很多引擎的口径是"信号次日开盘成交"。开盘一字板同样买不进：集合竞价
    以涨停价成交，买单排队，轮不到你。跌停开盘则卖不出。

    **本工具无法从成交记录判断对方用的是开盘价还是收盘价**，所以两项都报，
    让使用者按自己的口径取用。写明这一点，免得两个数被当成重复计数。
    """
    out = []
    need = {"open_limit_up", "open_limit_down"}
    if not need <= set(panel.columns):
        return out
    m = panel.set_index(["date", "code"])
    bad_buy = bad_sell = 0
    ex = []
    for t in trades.itertuples():
        key = (t.date, t.code)
        if key not in m.index:
            continue
        row = m.loc[key]
        if t.side == "buy" and bool(row.get("open_limit_up", False)):
            bad_buy += 1
            if len(ex) < 5:
                ex.append(f"{t.date.date()} {t.code} 买入，但当日**开盘**即涨停")
        if t.side == "sell" and bool(row.get("open_limit_down", False)):
            bad_sell += 1
            if len(ex) < 5:
                ex.append(f"{t.date.date()} {t.code} 卖出，但当日**开盘**即跌停")
    n = bad_buy + bad_sell
    if n:
        pct = n / max(len(trades), 1)
        out.append(_f("严重" if pct > 0.01 else "中等", "open_limit_fill",
                      f"{n} 笔成交当日**开盘即封板**（占 {pct:.2%}）—— "
                      "若你的引擎按开盘价成交，这些成交不存在",
                      "；".join(ex) +
                      "／本项与 limit_fill（按收盘价）**互补，不要相加**："
                      "工具判断不出你用的是开盘还是收盘口径，两项都报，"
                      "按自己的口径取用。"))
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


def check_t1(trades):
    """T+1：当日买入的股票**当日不能卖**。

    这是 A 股与美股最基本的差别之一，也是最容易漏的 —— 通用回测框架
    （backtrader 等）默认 T+0，拿来跑 A 股时不会有任何提示。
    影响方向明确：T+0 允许日内来回，回测收益**高估**。

    只查最硬的那条（同日同票既买又卖）。更细的"卖出量超过昨日可用量"
    需要股数，多数成交记录里没有，所以不在这里查 —— 宁可漏报也不误报。
    """
    out = []
    g = trades.groupby(["date", "code"])["side"].agg(set)
    both = g[g.apply(lambda x: {"buy", "sell"} <= x)]
    if len(both):
        ex = "；".join(f"{d.date()} {c}" for d, c in list(both.index)[:5])
        out.append(_f("严重", "t1",
                      f"{len(both)} 个「同一天、同一只票、既买又卖」—— "
                      "A 股 T+1，当日买入不能当日卖出，回测收益被高估", ex))
    return out


def check_suspension(trades, panel):
    """在**停牌日**成交 —— 那天根本没有交易。"""
    out = []
    if "tradable" not in panel.columns:
        return out
    m = panel.set_index(["date", "code"])["tradable"]
    bad, ex = 0, []
    for t in trades.itertuples():
        v = m.get((t.date, t.code))
        if v is not None and not bool(v):
            bad += 1
            if len(ex) < 5:
                ex.append(f"{t.date.date()} {t.code}")
    if bad:
        out.append(_f("严重", "suspension",
                      f"{bad} 笔成交发生在停牌日（占 {bad / max(len(trades), 1):.2%}）",
                      "；".join(ex)))
    return out


def check_price_convention(trades, panel):
    """成交价是**原始价**还是**复权价**。需要 price 列，没有就跳过（不猜）。

    做法：每只票算 `成交价 / 当日原始收盘价` 随时间的变化。

    * 恒等于 1 → 原始价，正确
    * 随时间跳变 → **复权价**

    **这里只能判到"是复权价"为止，判不出是前复权还是后复权。**
    第一版写的是"多半来自前复权"，那是过度声称 —— 任何复权序列相对原始价
    的比值都会在分红日跳变，两种复权在单份成交记录上**不可分辨**。
    要分辨必须比较**两个不同时间下载的快照**：前复权会改写历史，后复权不会。
    方法写在 detail 里，留给使用者自己做。

    为什么"是复权价"本身就值得报：按复权价算涨停价、算每手股数、
    做任何绝对价筛选（如"只买 10 元以下"）都会错 —— 那些规则作用在
    **真实报价**上，而复权价不是任何一天真实的报价。
    """
    out = []
    if "price" not in trades.columns or "close_raw" not in panel.columns:
        return out
    m = panel.set_index(["date", "code"])["close_raw"]
    ratios = {}
    for t in trades.itertuples():
        raw = m.get((t.date, t.code))
        px = getattr(t, "price", None)
        if raw is None or not raw or raw <= 0 or px is None or px <= 0:
            continue
        ratios.setdefault(t.code, []).append(px / float(raw))
    multi = {c: v for c, v in ratios.items() if len(v) >= 3}
    if not multi:
        return out
    import statistics as st
    unadj, adj, worst = 0, 0, None
    for c, v in multi.items():
        spread = (max(v) - min(v)) / max(st.mean(v), 1e-12)
        if all(abs(x - 1.0) < 0.005 for x in v):
            unadj += 1
        else:
            adj += 1
            if worst is None or spread > worst[1]:
                worst = (c, spread)
    n = len(multi)
    if adj:
        out.append(_f("中等", "price_convention",
                      f"{adj}/{n} 只股票的成交价**不是当日原始收盘价** —— 用的是复权价",
                      f"比值波动最大的是 {worst[0]}（{worst[1]:.1%}）。"
                      "复权价不是任何一天真实的报价，所以涨停价、每手股数、"
                      "绝对价筛选都要改用原始价算。"
                      "／**这一条的后果比「价格不对」大得多**：涨跌停价这个量"
                      "只在未复权价上有定义（交易所按除权参考价 ±幅度、"
                      "四舍五入到分）。拿复权价去算涨跌停，等于在一个不存在的"
                      "价格刻度上做可成交性判定 —— 而误差大小取决于每只票的"
                      "复权因子，没有统一的量级。整手（100 股的整数倍）和"
                      "最小变动价位（0.01 元）同理。"
                      "／本项无法判断是前复权还是后复权：两者在单份成交记录上"
                      "不可分辨。要分辨就隔一段时间重新下载一次同一段历史，"
                      "**前复权的历史值会变，后复权不会** —— 而历史会变的回测"
                      "无法复现，这比算错更麻烦。"))
    return out


def check_held_through_suspension(trades, panel):
    """**持仓穿越停牌** —— 比"在停牌日成交"常见得多，量级也更大。

    `check_suspension` 查的是成交日本身停牌（罕见，多数引擎会跳过）。
    这里查的是更常见的那种：**买入之后停牌，停牌期间账面照常算收益**。

    为什么这是问题：停牌往往伴随重大事项，复牌当天经常大幅跳空，而回测里
    停牌那段通常按最后价格平着走。

    **方向不预设。** 我第一版在这里写死了"利空停牌尤其，风险被删掉、收益被
    留下"，然后跑真实数据：复牌首日收益均值 **+3.34%**、中位数 +5.01% ——
    是正的。原因大概是 A 股停牌相当一部分是重大资产重组，复牌常连板。
    所以"高估收益"这个方向在那个样本上不成立，写死方向是错的。

    **确定成立的是另一件事，与方向无关**：停牌那段你**既不能卖也不能止损**。
    回测里任何止损、风控、调仓规则在那个窗口都是虚构的，而跳空的全部幅度
    （无论正负）你都必须照单全收。所以这里只报实测的分布，让数据说方向。

    做法：按成交记录还原每只票的持有区间（买 → 持有，卖 → 空仓），
    数区间内 `tradable == False` 的天数；若面板带收盘价，再算**复牌首日**
    的收益分布 —— 那才是被抹掉的东西的大小。
    """
    out = []
    if "tradable" not in panel.columns:
        return out
    pan = panel.sort_values(["code", "date"])
    by_code = {c: g for c, g in pan.groupby("code", observed=True)}
    has_px = "close_raw" in panel.columns

    episodes, total_days, gaps = 0, 0, []
    affected = set()
    for code, g in trades.sort_values("date").groupby("code", observed=True):
        p = by_code.get(code)
        if p is None or p.empty:
            continue
        held_from = None
        spans = []
        for t in g.itertuples():
            if t.side == "buy" and held_from is None:
                held_from = t.date
            elif t.side == "sell" and held_from is not None:
                spans.append((held_from, t.date))
                held_from = None
        if held_from is not None:                 # 还没卖出的，算到样本末
            spans.append((held_from, p["date"].max()))
        for a, b in spans:
            w = p[(p["date"] > a) & (p["date"] < b)]
            if w.empty:
                continue
            tr = w["tradable"].to_numpy(dtype=bool)
            if tr.all():
                continue
            affected.add(code)
            total_days += int((~tr).sum())
            # 停牌段的边界：False 转 True 的那一天就是复牌首日
            idx = w.index.to_numpy()
            for i in range(1, len(tr)):
                if tr[i] and not tr[i - 1]:
                    episodes += 1
                    if has_px and i >= 1:
                        prev = w.iloc[:i]
                        last_px = prev.loc[prev["tradable"].to_numpy(dtype=bool),
                                           "close_raw"]
                        if len(last_px):
                            px0 = float(last_px.iloc[-1])
                            px1 = float(w.iloc[i]["close_raw"])
                            if px0 > 0:
                                gaps.append(px1 / px0 - 1)
    if not affected:
        return out
    detail = (f"涉及 {len(affected)} 只股票，停牌日合计 {total_days} 个交易日，"
              f"复牌 {episodes} 次。")
    if gaps:
        import statistics as st
        mu, lo, hi = st.mean(gaps), min(gaps), max(gaps)
        detail += (f"／**复牌首日收益**：均值 {mu:+.2%}，"
                   f"中位数 {st.median(gaps):+.2%}，"
                   f"区间 [{lo:+.2%}, {hi:+.2%}]。"
                   f"这段跳空在回测里多半被抹成了平的 —— 按本样本，"
                   f"抹掉的方向是"
                   + ("**高估**" if mu < 0 else "**低估**") +
                   f"收益约 {abs(mu):.2%}／次；"
                   "但无论正负，停牌期间你既不能卖也不能止损，"
                   "回测里那段时间的止损与风控规则都是虚构的。")
    else:
        detail += "（面板没有收盘价列，算不出复牌首日跳空的大小。）"
    out.append(_f("严重", "held_through_suspension",
                  f"{episodes} 段持仓穿越了停牌期 —— 那段时间账面在算收益，"
                  "而实际既不能卖也不能止损", detail))
    return out


def check_cost_floor(trades, assumed_bps=None, assumed_min_commission=None):
    """成本地板：监管类费用（过户费 + 经手费 + 证管费 + 印花税）不可避免。

    多数回测只算佣金和印花税，漏掉过户费/经手费/证管费 —— 三项双边合计约
    0.0064%，一轮买卖约 1.3bp。单看不大，高换手策略上会累积。

    另一项更狠的是**佣金起点 5 元**：10,000 元的单子按万 2.5 只有 2.5 元，
    实际收 5 元，费率翻倍；5,000 元的单子是名义的 4 倍。
    回测用固定 bps 时，资金越小、单笔越小，低估得越厉害。
    """
    out = []
    if not len(trades):
        return out
    d0, d1 = trades["date"].min(), trades["date"].max()
    lo = regulatory_cost_rate(str(d0.date()), "buy")
    hi = regulatory_cost_rate(str(d1.date()), "sell")
    detail = (f"监管类费用地板（不含佣金）：买入约 {lo:.4%}、"
              f"卖出约 {hi:.4%}／构成：过户费 0.001% 双边、"
              "经手费 0.0341‰ 双边、证管费 0.02‰ 双边、印花税按日期分段。"
              "来源为券商官方费用公示，**只覆盖当前档，历史沿革未核**。")
    if assumed_bps is not None:
        floor_bps = (lo + hi) * 1e4
        if assumed_bps < floor_bps:
            out.append(_f("中等", "cost_floor",
                          f"你假设的一轮买卖成本 {assumed_bps:.2f}bp "
                          f"低于监管类费用地板 {floor_bps:.2f}bp（还没算佣金）",
                          detail))
    if assumed_min_commission is not None and assumed_min_commission <= 0:
        out.append(_f("中等", "cost_floor",
                      f"你的成本模型没有**佣金起点**（券商普遍 "
                      f"{MIN_COMMISSION_YUAN:.0f} 元/笔）",
                      "小额交易上这一项能让实际费率翻倍甚至更多："
                      "10,000 元按万 2.5 名义 2.5 元、实收 5 元；"
                      "5,000 元的单子实际费率是名义的 4 倍。"
                      "资金越小、单笔越小，回测低估得越厉害。"))
    return out


def check_order_size(trades, order_type="limit"):
    """整手约束与单笔申报上限。只在成交记录带**股数**时能查。

    出处：上交所交易规则 3.3.8/3.3.9、深交所交易规则 3.3.8/3.3.10、
    科创板交易特别规定第二十条、深交所创业板交易特别规定 2.8。
    条款号都在 ashare_rules 里，沪深主板各引各自的条款。

    为什么这条重要：按权重回测的策略，落到实盘要取整。一只 300 元的股票
    一手就是 3 万元，小资金账户上"买 0.4% 仓位"根本报不进去 —— 回测里
    那笔成交是凭空来的。反过来，大资金会撞上单笔申报上限，一笔变多笔、
    冲击成本上升，回测同样看不见。

    三个刻意的边界，免得被当成查全了：

    1. **默认按限价单的上限判**（order_type='limit'）。市价单的上限更低
       （创业板 15 万、科创板 5 万），但成交记录里通常看不出委托类型。
       限价档是两者中更宽的一档，所以默认口径**只会漏报、不会误报**。
       知道自己跑的是市价单，传 order_type='market'。
    2. **卖出侧只查上限、不查整手**：零股必须一次性卖出，"卖 37 股"合法。
       代价是"持仓 5000 股卖 150 股"这种违规查不出来 —— 审计器看不到持仓。
    3. **北交所不判**：申报数量规则没核到原文，单独计数并提示，不算通过。
    """
    out = []
    if not len(trades):
        return out
    if "qty" not in trades.columns:
        out.append(_f("轻微", "order_size",
                      "成交记录里没有股数列，整手约束与单笔上限**没查**",
                      "按权重输出的回测普遍如此。想查这一项，"
                      "在成交明细里加一列 qty（股数）再跑一次。"
                      "取整这件事在小资金上很致命：一只 300 元的股票"
                      "一手 3 万元，小账户上小额仓位根本报不进去。"))
        return out
    q = trades[["code", "side", "qty"]].dropna(subset=["qty"])
    if not len(q):
        out.append(_f("轻微", "order_size", "股数列全为空值，这一项没查"))
        return out
    bad, unjudged = {}, {}
    for code, side, n in q.itertuples(index=False):
        sd = str(side).strip().lower()
        if sd not in ("buy", "sell"):
            unjudged.setdefault(f"买卖方向无法识别（{side!r}）", []).append((code, n))
            continue
        ok, why = order_shares_ok(code, n, sd, order_type)
        if ok is None:
            unjudged.setdefault(why, []).append((code, n))
        elif not ok:
            bad.setdefault(why, []).append((code, sd, n))
    frac = sum(len(v) for v in bad.values()) / len(q)
    for why, rows in sorted(bad.items(), key=lambda kv: -len(kv[1])):
        code, sd, n = rows[0]
        out.append(_f("中等" if len(rows) / len(q) > 0.01 else "轻微",
                      "order_size",
                      f"{len(rows)} 笔成交：{why}",
                      f"例：{code} {sd} {float(n):,.0f} 股。"
                      f"整体 {frac:.2%} 的成交报不进交易所"
                      f"（按 {order_type} 口径）。"))
    for why, rows in sorted(unjudged.items(), key=lambda kv: -len(kv[1])):
        out.append(_f("轻微", "order_size",
                      f"{len(rows)} 笔成交**没判**：{why}",
                      f"例：{rows[0][0]}。没判不等于合规 —— "
                      "规则核不到原文的板块，这里不猜。"))
    return out


def _as_bool(sr):
    """把面板里的 is_st 稳健地变成 bool。

    直接 .astype(bool) 会把字符串 "False" 当成 True（非空字符串为真）。
    这是代码审查抓出来的：一份 is_st 存成字符串的面板会让 ST 检查大面积误报。

    **分支一律按类型语义判断，不看 dtype 的名字。** 第一版写的是
    `sr.dtype == object or str(sr.dtype).startswith("string")`，在 pandas 2.x
    上对，在 pandas 3.0 上错 —— 3.0 把默认字符串 dtype 换成了 `str`
    （PDEP-14），名字不以 "string" 开头，于是字符串列落进数值分支、
    `astype(float)` 碰上 "False" 直接抛异常。
    dtype 的**名字**是会变的，`is_bool_dtype` / `is_numeric_dtype` 这层语义不会。
    """
    if pd.api.types.is_bool_dtype(sr):
        # 含 pd.NA 的 nullable boolean：先填 False 再转，直接 astype(bool) 会炸
        return sr.fillna(False).astype(bool) if sr.isna().any() else sr.astype(bool)
    if pd.api.types.is_numeric_dtype(sr):
        return sr.fillna(0).astype(float).ne(0)
    # 其余（字符串、object、混杂）一律按字面量映射
    m = {"true": True, "1": True, "1.0": True, "y": True, "yes": True,
         "是": True, "t": True,
         "false": False, "0": False, "0.0": False, "n": False, "no": False,
         "否": False, "f": False, "": False, "nan": False, "none": False}
    return sr.map(lambda x: False if (x is None or x != x)
                  else m.get(str(x).strip().lower(), bool(x))).astype(bool)


def check_st_buy_cap(trades, panel):
    """风险警示股票：单账户单日买入单只不超过 50 万股。

    出处：上交所《风险警示板股票交易管理办法》第十四条（2013-01-01 起）。
    深交所的对应规定我没核，所以这里只查沪市代码 —— 少查是保守，
    照着沪市往深市套才是编规则。

    这条和整手一样只在有股数时能查，但它比整手更容易吃到：ST 股价低，
    50 万股在很多 ST 上只有两三百万元 —— 一个几千万的账户想在 ST 上
    建 5% 仓位，一天报不进去。回测里那笔成交是不存在的。
    """
    out = []
    if not len(trades) or "qty" not in trades.columns:
        return out
    if "is_st" not in panel.columns:
        return out
    st = panel.loc[_as_bool(panel["is_st"]), ["date", "code"]].copy()
    if not len(st):
        return out
    st["date"] = pd.to_datetime(st["date"]).dt.normalize()
    # 面板可能有重复行（拼接过的、带多层索引的）。不去重，merge 会把
    # 一笔合法成交复制成两笔再求和，凭空造出一个"超限"。
    st = st.drop_duplicates()
    b = trades[(trades["side"].astype(str).str.strip().str.lower() == "buy")
               & trades["qty"].notna()].copy()
    if not len(b):
        return out
    b["qty"] = pd.to_numeric(b["qty"], errors="coerce")
    b = b[b["qty"].notna()]
    if not len(b):
        return out
    # 成交记录常带时分秒，面板是午夜 —— 不归一化，merge 命中 0 行，
    # 这条检查会在真实输入上静默失效（代码审查抓出来的）。
    b["date"] = pd.to_datetime(b["date"]).dt.normalize()
    # 同一天同一只的买入要**累加**再比上限 —— 规则写的是"累计买入"，
    # 逐笔比会漏掉拆单。
    agg = (b.merge(st, on=["date", "code"], how="inner")
             .groupby(["date", "code"], as_index=False)["qty"].sum())
    if not len(agg):
        return out
    hits = []
    for dt, code, q in agg.itertuples(index=False):
        cap = st_daily_buy_cap(code, str(pd.Timestamp(dt).date()))
        if cap is not None and q > cap:
            hits.append((dt, code, q, cap))
    if not hits:
        return out
    dt, code, q, cap = max(hits, key=lambda x: x[2])
    out.append(_f("中等", "st_buy_cap",
                  f"{len(hits)} 个交易日的 ST 买入超过单日 50 万股上限",
                  f"最大的一笔：{code} {pd.Timestamp(dt).date()} 买入 "
                  f"{q:,.0f} 股，上限 {cap:,} 股。"
                  "规则按「当日累计」算，拆单不管用。"
                  "只查了沪市代码 —— 深交所的对应规定未核，不假设。"))
    return out


def check_tick_size(trades):
    """申报价格最小变动单位 0.01 元。只在成交记录带价格时能查。

    出处：上交所交易规则 3.3.11（A 股 0.01 元）。

    回测里常见的是"按当日均价成交"或"按 VWAP 成交"，这类价格几乎都不落在
    分上。成交价不在最小变动单位上，说明那个价格在市场上并不存在。
    它本身不一定是错（均价是合理近似），但**它是在告诉你回测用的不是
    真实可成交价**，滑点假设要从这里重新算。
    """
    out = []
    if not len(trades) or "price" not in trades.columns:
        return out
    p = trades["price"].dropna()
    p = p[p > 0]
    if not len(p):
        return out
    off = p[~p.map(price_on_tick)]
    if not len(off):
        return out
    frac = len(off) / len(p)
    out.append(_f("中等" if frac > 0.5 else "轻微", "tick_size",
                  f"{len(off)} 笔（{frac:.1%}）成交价不落在 0.01 元的"
                  "最小变动单位上",
                  f"例：{off.iloc[0]:.6f}。这种价格在市场上不存在，"
                  "通常意味着你成交在均价/VWAP 上而不是可申报价上 —— "
                  "不一定是错，但滑点假设得按这个重新算。"))
    return out


def check_naked_short(trades):
    """A 股卖不出手里没有的票。

    出处：上交所融资融券交易实施细则第二条（融券是"借入证券并卖出"）、
    第二十三条/第三十条（只能卖标的名单内的券）、第十二条（融券卖出申报价
    不得低于最新成交价，即提价规则）。

    这一条被忽略的程度和涨跌停差不多：**裸卖空在 A 股不存在**。卖出一只
    手里没有的票，是报不进去的委托，不是"成本高一点"的交易。多空对冲、
    截面做空的回测如果直接对因子低分组下空单，那半边收益整个是不存在的。

    查法：沿时间轴累计每只票的仓位，出现"卖出后仓位为负"就是不可能的成交。
    有 qty 列时按股数算，没有就按"这只票之前有没有买过"算。

    **一个必须说清的替代解释**：成交记录如果从策略中途截起，起点之前的持仓
    看不见，那么开头几笔卖出会被误判。所以这里把"最早的卖出发生在任何买入
    之前"和"中途卖穿"分开报，前者标轻微并写明这个可能。
    """
    out = []
    if not len(trades):
        return out
    d = trades.sort_values("date", kind="mergesort")
    has_q = "qty" in d.columns
    pos, first_buy = {}, {}
    naked_mid, naked_head = [], []
    for row in d.itertuples(index=False):
        code = row.code
        side = str(row.side).strip().lower()
        if side not in ("buy", "sell"):
            continue
        q = 1.0
        if has_q:
            try:
                q = abs(float(row.qty))
            except (TypeError, ValueError):
                q = 1.0
            if q != q or q in (float("inf"), float("-inf")):
                q = 1.0
        if side == "buy":
            pos[code] = pos.get(code, 0.0) + q
            first_buy.setdefault(code, row.date)
        else:
            have = pos.get(code, 0.0)
            if have <= 0:
                (naked_mid if code in first_buy else naked_head).append(
                    (row.date, code, q, have))
            pos[code] = have - q
    n_sell = int((d["side"].astype(str).str.strip().str.lower() == "sell").sum())
    if naked_mid:
        dt, code, q, have = naked_mid[0]
        out.append(_f("严重", "naked_short",
                      f"{len(naked_mid)} 笔卖出发生在仓位已经清零之后 —— "
                      "A 股卖不出手里没有的票",
                      f"例：{code} {pd.Timestamp(dt).date()} 卖出时账上仓位 "
                      f"{have:g}。裸卖空在 A 股是报不进去的委托，"
                      "不是成本高一点的交易。走融券还有三道门：标的名单、券源、"
                      "以及提价规则（申报价不得低于最新成交价）—— "
                      "提价规则意味着下跌途中往往卖不出去，"
                      "恰恰是最想做空的时候。"))
    if naked_head:
        dt, code, q, have = naked_head[0]
        out.append(_f("轻微", "naked_short",
                      f"{len(naked_head)} 笔卖出在这份记录里找不到对应的买入",
                      f"例：{code} {pd.Timestamp(dt).date()}。"
                      f"占全部卖出的 {len(naked_head) / max(n_sell, 1):.1%}。"
                      "两种可能：成交记录从策略中途截起、起点前的持仓看不见"
                      "（那就没问题）；或者策略真的在下空单（那半边收益"
                      "在 A 股不存在）。自己确认是哪一种。"))
    return out


def check_board_permission(trades, capital_yuan=None):
    """板块权限：交易科创板/北交所，等于假设账户资产曾达 50 万。

    出处：上交所科创板投资者适当性（个人：申请权限开通前 20 个交易日证券
    账户及资金账户内资产**日均不低于 50 万元**，且**参与证券交易 24 个月
    以上**）；北交所同为 50 万 + 24 个月；深交所创业板 10 万 + 24 个月。

    为什么这算制度坑：用 10 万本金回测一个含科创板的策略，前提本身不成立 ——
    不是成本问题，是权限问题。传 capital_yuan 才判，不传只列出涉及的板块。
    """
    out = []
    if not len(trades):
        return out
    boards = {}
    for code in trades["code"].unique():
        boards.setdefault(board(code), []).append(code)
    need = {b: board_access_threshold(codes[0]) for b, codes in boards.items()}
    need = {b: v for b, v in need.items() if v > 0}
    if not need:
        return out
    worst = max(need.values())
    listing = "、".join(f"{b}（{need[b]:,} 元）"
                       for b in sorted(need, key=lambda x: -need[x]))
    if capital_yuan is None:
        out.append(_f("轻微", "board_permission",
                      f"策略池涉及有权限门槛的板块：{listing}",
                      "门槛是「申请权限开通前 20 个交易日日均资产」，"
                      f"另需参与证券交易 {BOARD_ACCESS_MONTHS} 个月以上。"
                      "传 --capital 让我核一下你的本金够不够开这些权限。"))
        return out
    if capital_yuan < worst:
        out.append(_f("中等", "board_permission",
                      f"本金 {capital_yuan:,.0f} 元低于交易这些板块所需的"
                      f"权限门槛 {worst:,} 元",
                      f"涉及：{listing}。门槛是「申请权限开通前 20 个交易日"
                      "日均资产」，不是「现在有多少钱」，另需参与证券交易 "
                      f"{BOARD_ACCESS_MONTHS} 个月以上。"
                      "这不是成本问题，是这个账户根本下不了这些单。"))
    return out


def median_holding_days(trades):
    """从成交记录推每笔持仓的自然日天数，返回中位数；推不出来返回 None。

    按代码先进先出配对买卖。只用配得上对的那些 —— 期末还拿着的持仓
    不知道什么时候卖，不猜。
    """
    if not len(trades):
        return None
    d = trades.sort_values("date", kind="mergesort")
    open_lots, spans = {}, []
    for row in d.itertuples(index=False):
        side = str(row.side).strip().lower()
        if side == "buy":
            open_lots.setdefault(row.code, []).append(row.date)
        elif side == "sell":
            lots = open_lots.get(row.code)
            if lots:
                spans.append((pd.Timestamp(row.date)
                              - pd.Timestamp(lots.pop(0))).days)
    if not spans:
        return None
    spans.sort()
    n = len(spans)
    return float(spans[n // 2] if n % 2 else (spans[n // 2 - 1] + spans[n // 2]) / 2)


def check_dividend_tax(trades, dividend_yield=None):
    """复权价把分红按 100% 还原，但个人投资者的股息红利是**要交税**的。

    出处：财税〔2015〕101 号（2015-09-08 起）—— 持股 1 个月以内 20%、
    1 个月至 1 年 10%、超过 1 年免征。

    为什么这条值得单独查：前/后复权价序列等于假设股息免税，而短持仓的策略
    实际只拿到 80%。A 股股息率量级 2%，20% 的税就是每年 0.4% ——
    和手续费同一个数量级，但手续费人人都算，这个没人算。

    **这是上界，不是点估计**：年化高估 ≈ 股息率 × 税率 只在「策略全年在场、
    分红都落在持仓窗口内」时成立。分红大多落在持仓之外就更小。
    工具按上界说话，并把这句话一起输出 —— 不把上界当成实际损失。
    """
    out = []
    if not len(trades):
        return out
    hold = median_holding_days(trades)
    if hold is None:
        return out
    d0 = str(pd.Timestamp(trades["date"].min()).date())
    rate = dividend_tax_rate(hold, d0)
    if rate is None:
        out.append(_f("轻微", "dividend_tax",
                      "样本早于 2013-01-01，股息红利税的当时档位我没核到原文，"
                      "这一项**没判**"))
        return out
    if rate == 0:
        return out
    bracket = "1 个月以内" if hold <= ONE_MONTH_DAYS else "1 个月至 1 年"
    detail = (f"中位持仓 {hold:.0f} 天 → 落在「{bracket}」档，实际税率 "
              f"{rate:.0%}（财税〔2015〕101 号）。"
              "复权价序列把分红按 100% 还原进价格，等于假设股息免税。")
    if dividend_yield is None:
        out.append(_f("轻微", "dividend_tax",
                      f"中位持仓 {hold:.0f} 天，股息红利实际税率 {rate:.0%} —— "
                      "复权价回测默认按免税算",
                      detail + " 传 --dividend-yield（如 0.02）"
                      "让我把年化高估的上界算出来。"))
        return out
    if dividend_yield < 0:
        raise ValueError("股息率不能为负")
    drag = dividend_yield * rate
    out.append(_f("中等" if drag >= 0.002 else "轻微", "dividend_tax",
                  f"股息红利税让年化收益最多高估 {drag:.2%}"
                  f"（股息率 {dividend_yield:.2%} × 税率 {rate:.0%}）",
                  detail + " **这是上界不是点估计**：只在策略全年在场、"
                  "分红都落在持仓窗口内时成立，分红多落在持仓之外就更小。"
                  "和手续费同一个数量级，但手续费人人都算，这个没人算。"))
    return out


def check_ex_rights(trades, panel):
    """成交落在**除权除息日**上。这一条**只在未复权价的回测里**才是问题。

    交易所算涨跌停价用的是**除权参考价**。在**未复权**序列上，很多引擎图方便写
    `close.shift(1)` 当参考价，那在除权日就是错的 —— 实测例：sh.600000 2017-05-25，
    数据商给的 preclose = 11.75（当天实际封在涨停价 12.93），而前一日收盘是 15.47，
    用它算出的"涨停价"是 17.02，封板判定完全反过来。送转日原始价能跳 50% 以上。

    **但在复权序列上，`close.shift(1)` 本来就等于除权参考价，不存在这个问题。**
    这不是巧合，是复权因子的定义决定的：复权序列要让
    `adj(t)/adj(t-1)` 等于真实总收益，推出
    `除权参考价(t) × af(t) == 收盘(t-1) × af(t-1)`，
    也就是"复权空间里的前一日收盘"恰好就是"复权后的除权参考价"。
    实数核过：15.47/11.75 这个例子两边差 2.3e-07，而且复权空间里那天**判对了**涨停。

    所以这条检查做的事只有一件：**把落在除权日上的成交点出来，并说清这个条件**。
    它判不出使用者的引擎工作在哪个价格空间 —— 成交记录里看不到。猜一个答案然后
    报"你错了"，比不报更坏；而不说清"复权序列上没这个问题"，就会让一半的使用者
    白跑一趟。
    """
    out = []
    if not len(trades) or "ex_rights" not in panel.columns:
        return out
    xr = panel.loc[_as_bool(panel["ex_rights"]), ["date", "code"]].copy()
    if not len(xr):
        return out
    xr["date"] = pd.to_datetime(xr["date"]).dt.normalize()
    xr = xr.drop_duplicates()
    t = trades[["date", "code"]].copy()
    t["date"] = pd.to_datetime(t["date"]).dt.normalize()
    hit = t.merge(xr, on=["date", "code"], how="inner")
    if not len(hit):
        return out
    frac = len(hit) / len(trades)
    out.append(_f("中等" if frac > 0.01 else "轻微", "ex_rights",
                  f"{len(hit)} 笔成交（{frac:.2%}）落在除权除息日上 —— "
                  "**只有用未复权价回测时这才是问题**",
                  f"例：{hit['code'].iloc[0]} "
                  f"{pd.Timestamp(hit['date'].iloc[0]).date()}。"
                  "先确认你的价格序列："
                  "／**复权序列**：`close.shift(1)` 本来就等于除权参考价"
                  "（复权因子的定义就保证这件事），**这条不适用，可以跳过**。"
                  "／**未复权序列**：参考价必须用数据商给的 preclose，"
                  "用 `close.shift(1)` 在这些天上会算出离谱的阈值"
                  "（实测：真实涨停价 12.93，用 shift 算出 17.02），"
                  "该判封板的没判、不该判的判了；送转日原始价跳 50% 以上。"
                  "／**本检查判不出你在哪个空间 —— 成交记录里看不到，所以不猜。**"))
    return out


def check_survivorship(trades, panel):
    """样本里一只退市股都没有 → 几乎一定是幸存者偏差。"""
    out = []
    # 面板自己标了"不是全市场"时，这一项**没法查**，要和"该有退市股却没有"分开说。
    # 不分开的后果很具体：用只含自己交易过的股票的窄面板跑一遍，
    # 看到"未发现幸存者偏差"就以为过了 —— 而那是本工具最该避免的静默降级。
    if "panel_is_full_universe" in panel.columns:
        full = _as_bool(panel["panel_is_full_universe"])
        if len(full) and not bool(full.iloc[0]):
            out.append(_f("轻微", "survivorship",
                          "这份参考面板只含部分股票，幸存者偏差**没查**",
                          "它要比对全市场的退市股，窄面板里压根没有可比的对象。"
                          "想查这一项，用全市场面板重建一次"
                          "（`make_reference_panel.py` 去掉 --from-trades）。"
                          "**没查不等于通过** —— 幸存者偏差是单向高估收益的，"
                          "而且在短线策略上尤其大。"))
            return out
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


def expected_max_sharpe(n_trials, sd, mean=0.0):
    """N 次独立试验下，**零技能**假设中最大 Sharpe 的期望。

        E[max] ≈ μ + σ·[(1-γ)Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e))]

    γ 是 Euler–Mascheroni 常数。这是极值分布的标准近似（Bailey & López de
    Prado 在 Deflated Sharpe Ratio 里用的同一个式子）。

    它回答的问题是：**如果我完全没有技能，光靠试 N 次，能挑出多高的 Sharpe？**
    只有超过这个数的部分才可能是技能。

    这一项**不是 A 股特有**的 —— 但它是本工具其余七项加起来都比不上的那一个。
    一份回测哪怕制度规则全对，只要作者试了 200 个策略只报最好的那个，
    报出来的数字就仍然主要是选择的产物。
    """
    from statistics import NormalDist
    if n_trials < 2:
        raise ValueError("试验次数至少 2 次")
    if sd <= 0:
        raise ValueError("试验间 Sharpe 的标准差必须为正")
    g, nd, e = 0.5772156649015329, NormalDist(), 2.718281828459045
    return mean + sd * ((1 - g) * nd.inv_cdf(1 - 1 / n_trials)
                        + g * nd.inv_cdf(1 - 1 / (n_trials * e)))


def report_trials(n_trials, observed, sd, mean):
    emax = expected_max_sharpe(n_trials, sd, mean)
    print(f"试验 {n_trials} 次，试验间 Sharpe 均值 {mean:.3f}、标准差 {sd:.3f}\n")
    print(f"  **零技能**假设下，最大 Sharpe 的期望：{emax:.2f}")
    if observed is not None:
        gap = observed - emax
        print(f"  你报告的 Sharpe：{observed:.2f}   差额：{gap:+.2f}")
        if gap <= 0:
            print("\n  → 报告的 Sharpe **没有超过**零技能下的期望最大值。"
                  "\n    这个数字可以完全由试验次数解释，不构成技能的证据。")
        elif gap < 0.5 * sd:
            print("\n  → 只高出不到半个试验间标准差。**大部分**可以由试验次数解释。")
        else:
            print("\n  → 高出零技能期望，但这不等于显著 —— 还要看样本长度、"
                  "\n    收益的偏度与峰度。完整的 Deflated Sharpe Ratio 需要这些。")
    print("\n  试验次数的代价：")
    for n in (5, 10, 20, 50, 100, 200, 500):
        print(f"    {n:>4d} 次 → {expected_max_sharpe(n, sd, mean):.2f}")
    print("\n  两条边界：")
    print("  1. 本式假设 N 次试验**相互独立**。若你的策略是同一想法的变体"
          "（换窗口、换参数），\n     实际独立试验数远小于 N，真实期望最大值更低 —— "
          "所以这里算的是**上界**。")
    print("  2. 试验次数要算**全部**跑过的，不只是留下的那些。"
          "\n     调参试过的、跑完看一眼就删的，全都算。")


def run(trades, panel, assumed_limit_pct=None, assumed_stamp_bps=None,
        assumed_cost_bps=None, assumed_min_commission=None,
        order_type="limit", capital_yuan=None,
        dividend_yield=None):
    f = []
    f += check_limit_fill(trades, panel)
    f += check_open_limit_fill(trades, panel)
    f += check_limit_rule(trades, assumed_limit_pct)
    f += check_stamp_duty(trades, assumed_stamp_bps)
    f += check_circuit_breaker(trades)
    f += check_t1(trades)
    f += check_suspension(trades, panel)
    f += check_price_convention(trades, panel)
    f += check_held_through_suspension(trades, panel)
    f += check_cost_floor(trades, assumed_cost_bps, assumed_min_commission)
    f += check_order_size(trades, order_type)
    f += check_tick_size(trades)
    f += check_st_buy_cap(trades, panel)
    f += check_naked_short(trades)
    f += check_board_permission(trades, capital_yuan)
    f += check_dividend_tax(trades, dividend_yield)
    f += check_ex_rights(trades, panel)
    f += check_survivorship(trades, panel)
    return sorted(f, key=lambda x: SEV.index(x["severity"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="A 股回测的制度规则审计")
    ap.add_argument("trades", nargs="?",
                    help="成交记录 CSV：date, code, side[, price]")
    ap.add_argument("--trials", type=int, default=None,
                    help="多重检验模式：一共跑过多少次回测/策略")
    ap.add_argument("--observed-sharpe", type=float, default=None,
                    help="你最终报告的那个 Sharpe")
    ap.add_argument("--trial-sharpes", default=None,
                    help="逗号分隔的各次试验 Sharpe，用来估均值与标准差")
    ap.add_argument("--trial-sharpe-sd", type=float, default=None,
                    help="试验间 Sharpe 的标准差（没有 --trial-sharpes 时用）")
    ap.add_argument("--trial-sharpe-mean", type=float, default=0.0)
    ap.add_argument("--assumed-cost-bps", type=float, default=None,
                    help="你的成本模型里一轮买卖合计多少 bp")
    ap.add_argument("--assumed-min-commission", type=float, default=None,
                    help="你的成本模型里的佣金起点（元）；传 0 表示没有")
    ap.add_argument("--panel", default="data/panel.parquet",
                    help="对照面板 parquet，需含 date/code/limit_up/limit_down/is_st 列")
    ap.add_argument("--assumed-limit-pct", type=float, default=None,
                    help="被审回测假设的涨跌幅，如 0.10")
    ap.add_argument("--assumed-stamp-bps", type=float, default=None,
                    help="被审回测假设的印花税，如 5")
    ap.add_argument("--framework", choices=sorted(FRAMEWORK_PROFILES),
                    help="直接套用某框架的默认假设")
    ap.add_argument("--dividend-yield", type=float, default=None,
                    help="组合的年化股息率（如 0.02），用来算股息红利税"
                         "带来的年化高估上界")
    ap.add_argument("--capital", type=float, default=None,
                    help="回测本金（元），用来核板块权限门槛（科创板/北交所"
                         "50 万、创业板 10 万）")
    ap.add_argument("--order-type", choices=("limit", "market"), default="limit",
                    help="委托类型，决定按哪一档单笔上限判（默认 limit，"
                         "是更宽的一档，只会漏报不会误报）")
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

    if a.trials is not None:
        if a.trial_sharpes:
            import statistics as _st
            v = [float(x) for x in a.trial_sharpes.split(",") if x.strip()]
            if len(v) < 2:
                raise SystemExit("--trial-sharpes 至少给 2 个值")
            mean, sd = _st.mean(v), _st.stdev(v)
            print(f"（标准差来自你给的 {len(v)} 个试验 Sharpe"
                  f"{'，注意这是全部 %d 次里的一部分' % a.trials if len(v) < a.trials else ''}）\n")
        elif a.trial_sharpe_sd:
            mean, sd = a.trial_sharpe_mean, a.trial_sharpe_sd
        else:
            raise SystemExit("需要 --trial-sharpes 或 --trial-sharpe-sd 之一"
                             " —— 没有试验间的离散程度就算不出期望最大值")
        report_trials(a.trials, a.observed_sharpe, sd, mean)
        return
    if not a.trades:
        raise SystemExit("要么给成交记录 CSV，要么用 --trials 走多重检验模式")

    if a.positions:
        tr = positions_to_trades(a.trades, a.date_col, a.code_col, a.qty_col)
        print(f"由持仓表推出 {len(tr)} 笔成交（只取 0↔非0 的跃迁）\n")
    else:
        tr = read_trades(a.trades, a.date_col, a.code_col, a.side_col)
    # **每加一项用到新面板列的检查，这里必须跟着加。** 漏掉的后果是静默的：
    # 列被丢掉 → 那项检查当成"面板没这一列"直接跳过 → 输出里一个字都看不到。
    # ex_rights 和 panel_is_full_universe 就这么漏过一轮（加了检查、没加到这里，
    # 于是 CLI 路径上从未触发），是端到端跑窄面板时才发现的。
    # test_cli_loads_every_panel_column_the_checks_use 现在守住这件事。
    want = ["date", "code", "limit_up", "limit_down", "is_st", "tradable",
            "close_raw", "open_limit_up", "open_limit_down",
            "ex_rights", "panel_is_full_universe"]
    import pyarrow.parquet as _pq
    have = set(_pq.ParquetFile(a.panel).schema_arrow.names)
    pan = pd.read_parquet(a.panel, columns=[c for c in want if c in have])
    fs = run(tr, pan, a.assumed_limit_pct, a.assumed_stamp_bps,
             a.assumed_cost_bps, a.assumed_min_commission, a.order_type,
             a.capital, a.dividend_yield)
    print(f"审计 {len(tr)} 笔成交，{tr['code'].nunique()} 只股票，"
          f"{tr['date'].min().date()} ~ {tr['date'].max().date()}\n")
    if not fs:
        print("未发现问题。（注意：未发现不等于没有 —— 本工具只查\n      RULE_INVENTORY.md 里标了 ✅/◐ 的那些项，标 ○ 和 ✗ 的一概没查）")
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
