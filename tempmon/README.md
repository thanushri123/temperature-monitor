# Real-Time Temperature Monitoring & Alert System

Edge devices report °C once per minute. Each reading is validated, stored (14-day retention),
compared with a threshold (`temp >= threshold` ⇒ alert) and surfaced on a Streamlit dashboard.
**All user-facing times are IST (UTC+05:30); storage is UTC epoch seconds.**

## Layout
```
app.py                  Streamlit dashboard
tempmon/                config · validation · store (SQLite) · processor (threshold+alerts)
                        notifier · simulator · runtime · timeutil (IST)
scripts/run_simulator.py  headless device simulator      scripts/ingest_file.py  CSV/JSONL replay
data/sample_readings.csv  sample test data               tests/                  41 automated tests
```

## Setup
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run
```bash
streamlit run app.py            # dashboard (auto-starts a 12-device simulator + 6 h history)
python scripts/run_simulator.py --devices 50 --interval 60 --backfill-hours 24   # optional, headless
python scripts/ingest_file.py data/sample_readings.csv                            # replay sample data
```

## Configure (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `TEMPMON_THRESHOLD_C` | 40 | alert when temperature ≥ this |
| `TEMPMON_RETENTION_DAYS` | 14 | retention window |
| `TEMPMON_RENOTIFY_MIN` | 15 | reminder interval during a sustained breach |
| `TEMPMON_OFFLINE_SEC` | 180 | no reading for this long ⇒ device OFFLINE |
| `TEMPMON_MIN_VALID_C` / `MAX_VALID_C` | -50 / 150 | plausible sensor range |
| `TEMPMON_WEBHOOK_URL` | – | optional Slack/Teams/PagerDuty-style webhook |
| `TEMPMON_DB` | data/tempmon.db | SQLite path |

## Test
```bash
python -m pytest -v          # or: python -m unittest discover -s tests -t .
```
Covers: normal readings, below/at/above threshold, alert open → reminder → resolve, per-device
independence, invalid/missing/non-numeric/out-of-range readings, bad timestamps, duplicates,
out-of-order data, notifier failure, offline detection, 14-day retention (boundary, open alerts,
history queries), sample CSV end-to-end, seeded simulator, 50-device volume run, IST conversion.

## Alert semantics
* Every breaching reading is flagged in storage. First breach opens an alert and notifies at once.
* Sustained breach → one reminder per `RENOTIFY_MIN` (no alert storm). Back below threshold → RESOLVED.
* Invalid readings are quarantined in an audit table and never alert (and never resolve an alert).
* Late/out-of-order readings are stored but don't flip live state; duplicates are ignored (idempotent).
