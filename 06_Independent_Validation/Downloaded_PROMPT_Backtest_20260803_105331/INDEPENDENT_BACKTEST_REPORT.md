# TTE Project B — Downloaded PROMPT Independent Backtest

Decision: **PROJECT_B_PRELIMINARY_INDEPENDENT_VALIDATION_INSUFFICIENT_EVIDENCE**

## Evidence boundary

- Downloaded PROMPT actuals: 2026-07-01 to 2026-07-22
- Estates: 16; estate-day rows: 339
- Weighment rows: 121,541; actual green leaf: 982,360.00 kg
- These July records were scored only; they were not merged into training data.
- Missing estate-days were not created or converted to zero.
- Neural result = unchanged V2 P50 champion plus the hash-matched V4 absolute P90 reliability policy.
- Project A was not read, changed, or restarted.

## Preliminary independent results

| Model | Join coverage | P50 WAPE | P50 bias | P90 coverage | Lag-7 comparable WAPE | Lag-7 naive WAPE | Gate |
|---|---:|---:|---:|---:|---:|---:|---|
| PHASE5_XGBOOST | 76.70% | 23.42% | -11.61% | 97.69% | 20.19% | 21.06% | INSUFFICIENT_EVIDENCE |
| NEURAL_V2_P50_V4_P90_LOCKED_CHAMPION | 76.70% | 34.78% | -25.47% | 96.92% | 35.54% | 21.06% | INSUFFICIENT_EVIDENCE |

## Control decision

This is a short post-training confirmation window, not authorization for live use. Project B remains a challenger until a longer, complete period confirms stability and management signs off.

- Retraining: disabled
- Canonical-history merge: disabled
- RL workforce allocation: disabled
- Operational forecasts: not authorized
