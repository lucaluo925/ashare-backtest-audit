"""审计案例 01 的复审计算 —— 案例文里的每个数都由这个脚本生成。

数据来源：`case01_published_summary.csv`，逐字抄自被审项目**自己发布**的
`backtest_data/results_v2/summary_v2.json`（28 套策略的收益/回撤/胜率/笔数/
平均持仓）。没有用它的行情数据，也没有跑它的回测 —— 那个仓库不随附行情。

跑法：
    python3 -B patterns/case01_recheck.py

为什么要有这个脚本：案例文里写了"零技能下期望最大 +105.66%"这种数。
**手写的数字会在复制粘贴里漂移，而且没人能复核。** 这里让它可重跑。
"""
import csv
import os
import statistics as st
import sys
from statistics import NormalDist

HERE = os.path.dirname(os.path.abspath(__file__))
# 两种目录布局都要能跑：
#   私有仓库 —— 这个文件在 patterns/，规则在 ../src/ashare_rules.py
#   公开仓库 —— 这个文件和单文件版 ashare_audit_standalone.py 并排
# 不做这件事，发布出去的脚本在公开仓库里 import 就会失败，
# 而"案例里的数可重跑"这句话也就成了空话。
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)
try:
    from ashare_rules import (  # noqa: E402
        HANDLING_FEE_RATE,
        REGULATORY_FEE_RATE,
        TRANSFER_FEE_RATE,
        dividend_tax_rate,
        stamp_duty_bps_on,
    )
except ImportError:  # pragma: no cover - 公开仓库布局
    from ashare_audit_standalone import (  # noqa: E402
        HANDLING_FEE_RATE,
        REGULATORY_FEE_RATE,
        TRANSFER_FEE_RATE,
        dividend_tax_rate,
        stamp_duty_bps_on,
    )

CSV_PATH = os.path.join(HERE, "case01_published_summary.csv")

# 被审项目 backtest_engine_v2.py 第 20–22 行
THEIR_COMMISSION = 0.0003      # 佣金万 3
THEIR_STAMP = 0.001            # 印花税千 1（卖出）
# 滑点千 1 不参与对比：滑点是假设，不是费率表
BACKTEST_START = "2025-09-01"  # 它 README 写的回测期起点


def load():
    with open(CSV_PATH, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def expected_max(mean, sd, n):
    """零技能、独立、正态假设下 n 次试验的期望最大值。

    与 ashare_audit.expected_max_sharpe 同一个式子，只是作用在收益率上而非
    Sharpe 上 —— 式子对任何近似正态的统计量都成立，但**正态这个前提在年度
    收益上不成立**（右偏），所以结论只能当量级看。
    """
    if n < 2:
        raise ValueError("试验次数至少 2 次")
    if sd <= 0:
        raise ValueError("标准差必须为正")
    g, nd, e = 0.5772156649015329, NormalDist(), 2.718281828459045
    return mean + sd * ((1 - g) * nd.inv_cdf(1 - 1 / n)
                        + g * nd.inv_cdf(1 - 1 / (n * e)))


def main():
    rows = load()
    rs = [float(r["total_return"]) for r in rows]
    n = len(rs)
    mu, sd = st.mean(rs), st.stdev(rs)
    best = max(rows, key=lambda r: float(r["total_return"]))
    em = expected_max(mu, sd, n)

    print("=== 多重检验 ===")
    print(f"策略数 N = {n}；收益率 均值 {mu:+.2f}%  标准差 {sd:.2f}")
    print(f"实际最大 {max(rs):+.2f}%（{best['strategy']} {best['name']}）"
          f" = 均值 + {(max(rs) - mu) / sd:.2f} 个标准差")
    print(f"零技能期望最大 E[max] = {em:+.2f}% = 均值 + {(em - mu) / sd:.2f} 个标准差")
    print(f"盈利 {sum(1 for r in rs if r > 0)} 套，亏损 {sum(1 for r in rs if r <= 0)} 套")
    print("  前提：独立 + 正态 + 零技能。28 套高度相关 → 有效 N < 28 → 真实 E[max]")
    print("  更低（此算法高估选择效应）；而年度收益右偏 → 正态 null 低估期望最大值")
    print("  （此算法低估选择效应）。两头都偏，所以只当量级看。")

    print("\n=== 成本（只比制度性费率，不含滑点）===")
    theirs = THEIR_COMMISSION * 2 + THEIR_STAMP
    real_stamp = stamp_duty_bps_on(BACKTEST_START) / 1e4
    other = (TRANSFER_FEE_RATE + HANDLING_FEE_RATE + REGULATORY_FEE_RATE) * 2
    real = THEIR_COMMISSION * 2 + real_stamp + other
    print(f"它的一轮买卖      {theirs * 1e4:6.2f} bp（印花税按 {THEIR_STAMP * 1e4:.0f}bp）")
    print(f"同佣金率下真实    {real * 1e4:6.2f} bp（印花税 {real_stamp * 1e4:.0f}bp"
          f" + 过户/经手/证管双边 {other * 1e4:.2f}bp）")
    gap = theirs - real
    print(f"净差：它{'多扣' if gap > 0 else '少扣'} {abs(gap) * 1e4:.2f} bp/轮")
    nt = int(best["trades"])
    print(f"冠军 {nt} 笔，线性累计{'多扣' if gap > 0 else '少扣'} "
          f"{abs(gap) * nt * 100:.2f} 个百分点（未复利）")

    print("\n=== 股息红利税（它用 adjust='qfq' 前复权）===")
    holds = [float(r["avg_hold"]) for r in rows]
    rate = dividend_tax_rate(max(holds), BACKTEST_START)
    print(f"平均持仓 {min(holds):.1f} ~ {max(holds):.1f} 天 —— 最长的那套也只有 "
          f"{max(holds):.1f} 天")
    print(f"全部落在同一档，个人实际税率 {rate:.0%}")
    for y in (0.015, 0.02, 0.025):
        print(f"  股息率 {y:.1%} → 年化高估上界 {y * rate:.2%}")

    print("\n=== 冠军的分季度 ===")
    print("Q1 +20.70% / Q2 +87.83% / Q3 +2.47%（来自它发布的 periods 字段）")
    print("收益高度集中在一个季度 —— 与「赢家是那一次好运的抽样」一致，但不构成证明。")


if __name__ == "__main__":
    main()
