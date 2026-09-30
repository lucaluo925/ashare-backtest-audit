# 示例

`sample_trades.csv` —— 一份典型的"涨停后买入、持有若干日卖出"的成交记录。

```bash
python3 ../make_reference_panel.py --start 2024-01-01 --out panel.parquet
python3 ../ashare_audit_standalone.py sample_trades.csv --panel panel.parquet --framework qlib
```

预期能看到 `limit_fill`（封板日成交）和 `survivorship`（样本里没有退市股）两项告警。
