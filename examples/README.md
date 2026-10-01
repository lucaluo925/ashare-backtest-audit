# 示例

`sample_trades.csv` —— 一份典型的"涨停后买入、持有若干日卖出"的成交记录。

```bash
python3 ../make_reference_panel.py --start 2024-01-01 --out panel.parquet
python3 ../ashare_audit_standalone.py sample_trades.csv --panel panel.parquet --framework qlib
```

预期能看到 `limit_fill`（封板日成交）和 `survivorship`（样本里没有退市股）两项告警。

`sample_trades_with_qty.csv` —— 同样的格式，但**多了 `qty`（股数）和 `price` 两列**。
数量类规则（整手、单笔申报上限、ST 单日 50 万股）和最小变动价位只有拿到这两列
才查得动；缺列时工具会明说"没查"，不会猜。

```bash
python3 ../ashare_audit_standalone.py examples/sample_trades_with_qty.csv --panel panel.parquet
```

这一份是故意每条都踩一次：主板买 150 股（不是整手）、科创板买 137 股（低于 200 股
起点）、创业板买 40 万股（超过 30 万限价上限）、成交价 33.333333（不落在分上）、
以及一只北交所代码 —— 最后这条会输出"**没判**"，因为北交所的申报数量规则我
一条原文都没核到。卖出 37 股是**合法**的（零股必须一次性卖出），所以不报。

跑市价单的加 `--order-type market`：市价档上限更低（创业板 15 万、科创板 5 万），
默认的限价档是更宽的一档，只会漏报、不会误报。

加 `--capital 100000` 还会核板块权限门槛：科创板与北交所要求「申请权限开通前
20 个交易日日均资产 ≥ 50 万 + 参与证券交易 ≥ 24 个月」，创业板是 10 万 + 24 个月。
**这不是成本问题，是这个账户根本下不了这些单** —— 用 10 万本金回测一个含科创板
的策略，前提本身不成立。
