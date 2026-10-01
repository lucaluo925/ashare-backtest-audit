"""案例 02 的度量脚本：`.round(2)` 算涨跌停价会错多少、错在哪一侧。

被审对象在 `data/quality.py` 里这样算涨跌停价：

    df["limit_up"] = (prev_close * (1 + limit_pct)).round(2)

`.round(2)` 是**银行家舍入**（四舍六入五成双），交易所是**四舍五入**。
这个脚本回答三个问题，全部用真实面板算，不猜：

  1. 两种口径差 1 分的行有多少？
  2. 差在哪一侧？（这决定后果是"少赚"还是"少赔"）
  3. 多少个**真实封跌停**的日子因此没被判出来、于是回测里照样卖出？

跑法（需要一份带 date/code/close_raw 的日线面板）：
    python3 -B patterns/case02_rounding.py --panel data/panel.parquet

公开仓库里没有随附面板 —— 用 `make_reference_panel.py` 从 baostock 自建一份
即可复核（它免费、含退市股）。
"""
import argparse
import os

import numpy as np
import pandas as pd


def exact_half_up(cents, num, den):
    """交易所口径：前收（整数分）× num/den，四舍五入到分。

    用整数算术，不碰浮点 —— 这里要的是"数学上正确的那个值"作为基准，
    而被审代码算出的是浮点 round 的结果，两者正是本案例要比的东西。
    """
    x = cents * num
    q, r = np.divmod(x, den)
    return q + (2 * r >= den)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "panel.parquet"))
    a = ap.parse_args()

    p = pd.read_parquet(a.panel, columns=["date", "code", "close_raw", "open_raw"])
    p = p.sort_values(["code", "date"])
    p["prev"] = p.groupby("code", observed=True)["close_raw"].shift(1)
    n0 = len(p)
    # close_raw 也要去 NaN：留着它，整数转换会产出垃圾值，统计全错
    p = p.dropna(subset=["prev", "close_raw", "open_raw"]).reset_index(drop=True)
    prev_f = p["prev"].astype("float64").to_numpy()
    close_c = np.rint(p["close_raw"].astype("float64").to_numpy() * 100).astype(np.int64)
    cents = np.rint(prev_f * 100).astype(np.int64)
    print(f"面板 {n0:,} 行，去掉缺价后 {len(p):,} 行，"
          f"{p['code'].nunique()} 只，{p['date'].min().date()} ~ {p['date'].max().date()}")

    print("\n### 一、两种舍入口径差多少，差在哪一侧")
    for num, den, label in ((11, 10, "涨停价 ×1.1"), (9, 10, "跌停价 ×0.9"),
                            (12, 10, "涨停价 ×1.2（创业板/科创板）"),
                            (8, 10, "跌停价 ×0.8（创业板/科创板）")):
        hu = exact_half_up(cents, num, den)
        actual = np.rint(np.round(prev_f * (num / den), 2) * 100).astype(np.int64)
        d = actual != hu
        print(f"  {label}：不同 {int(d.sum()):,} 行（{d.mean():.4%}）"
              f"，偏低 {int((actual < hu).sum()):,} / 偏高 {int((actual > hu).sum()):,}")

    print("\n### 二、后果：真实封跌停却没被判出来（回测照样卖出）")
    hu_dn = exact_half_up(cents, 9, 10)
    act_dn = np.rint(np.round(prev_f * 0.9, 2) * 100).astype(np.int64)
    real_down = close_c == hu_dn
    missed = real_down & (close_c > act_dn)
    blocked_wrongly = (close_c == act_dn) & (close_c < hu_dn)
    print(f"  真实收在跌停价上：{int(real_down.sum()):,} 行")
    print(f"  **没被判成跌停、于是回测里照样卖出**：{int(missed.sum()):,} 行"
          f"（占真实跌停 {missed.sum() / max(real_down.sum(), 1):.2%}）")
    print(f"  反方向（把没跌停的判成跌停、于是卖不出）：{int(blocked_wrongly.sum()):,} 行")
    print("  → 这一侧**不保守**：策略在一个本来逃不掉的跌停日逃掉了，"
          "收益被高估、回撤被低估。")

    print("\n### 三、为什么 ±20% 的板块不会踩这个坑")
    print("  前收以分为单位（k 分）。×1.1 = 11k/10 分，第三位小数是 (11k mod 10)/10，")
    print("  当 k 以 5 结尾时正好 .5 —— 落在舍入的分界上。")
    print("  ×1.2 = 12k/10，(12k mod 10) ∈ {0,2,4,6,8}，**永远不会是 5**。")
    print("  所以这是 ±10%（以及 ST 的 ±5%）独有的坑，和板块宽度直接相关。")

    print("\n### 四、单一 limit_pct=0.10 用在 ±20% 板块上的影响")
    code = p["code"].astype(str)
    eff = ((code.str.startswith("sz.3") & (p["date"] >= "2020-08-24"))
           | (code.str.startswith("sh.688") & (p["date"] >= "2019-07-22")))
    up10 = (p["prev"] * 1.10).round(2)
    dn10 = (p["prev"] * 0.90).round(2)
    fake_up = eff & (p["open_raw"] >= up10) & (p["open_raw"] < p["prev"] * 1.20)
    fake_dn = eff & (p["open_raw"] <= dn10) & (p["open_raw"] > p["prev"] * 0.80)
    print(f"  ±20% 规则已生效的行：{int(eff.sum()):,}（占全样本 {eff.mean():.2%}）")
    print(f"  开盘涨幅在 (10%,20%) 被误判成涨停、买不进：{int(fake_up.sum()):,} 行"
          f"（{fake_up.sum() / max(eff.sum(), 1):.3%}）")
    print(f"  开盘跌幅在 (10%,20%) 被误判成跌停、卖不出：{int(fake_dn.sum()):,} 行"
          f"（{fake_dn.sum() / max(eff.sum(), 1):.3%}）")
    print("  → 这两侧都**保守**（少成交），量级也小。这条是真缺口，但不要夸大。")


if __name__ == "__main__":
    main()
