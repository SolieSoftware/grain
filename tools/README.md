# Evaluation tools

Not part of the test suite. These answer questions the suite cannot: whether the
engines are *right* (rather than merely in agreement), and what the symmetric
encoding costs at a scale chinook cannot measure.

Run with `GRAIN_DATABASE_URL` set and `PYTHONPATH=tools`.

| Tool | Question it answers |
|---|---|
| `oracle.py` | What IS the right answer? Computes it in pure Python from raw rows, sharing no SQL with either engine — so it cannot inherit their mistakes. |
| `sweep.py` | Do both engines match the oracle across every enumerated (root, path, group key, metric) combination? |
| `bench.py` | What does the encoding cost as data grows? Synthetic, via `generate_series` — no tables are created. |
| `stress.py` | How does the encoding behave at magnitudes and precisions where Looker's documented limits bite? |

`oracle.py` is the important one. A two-engine comparison can only find
disagreements; it cannot catch both engines being wrong the same way. The oracle
is what turns "they agree" into "they are right".
