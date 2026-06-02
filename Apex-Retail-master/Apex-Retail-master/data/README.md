# Dataset directory

Place the challenge ZIP contents here:

```
data/
├── clips/                   # CCTV clips (5 stores × 3 cams × 20 min)
├── store_layout.json        # zone definitions per store
├── pos_transactions.csv     # POS records (store_id, txn_id, ts, basket_inr)
├── sample_events.jsonl      # 200 example events (validation reference)
└── assertions.py            # 10 example test assertions
```

Everything except this README is **gitignored** — the challenge license forbids
redistribution of the footage and PoS data.
