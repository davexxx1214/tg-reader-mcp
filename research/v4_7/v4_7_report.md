# V4.7 bounded score-tilt allocation

Classification: **exploratory_historical_validation_not_blind**. The 2026 outcomes were already accessed in predecessor research, so this result is exploratory, not blind or confirmatory.

The V4.6 signal weights and Top 10 selection are unchanged. V4.7 changes only the allocation inside those ten names. Raw weights are proportional to composite score raised to a frozen power and then projected to 5%-20% per name, 35% per industry, and global score-weight monotonicity.

## Training winner (2021-2025)

- Candidate: `score_power_6`
- Score power: 6
- Dynamic tilt selected: **True**
- Selected / equal-weight cumulative return: +146.95% / +118.13%
- Return delta: +28.81%
- Selected annualized return / volatility: +20.57% / 19.68%
- Selected maximum drawdown: -16.94%

## 2026 historical test (six complete periods)

- Selected / equal-weight / SPY cumulative return: +22.19% / +18.13% / +9.23%
- Selected minus equal-weight: +4.06%
- Selected annualized return / volatility: +49.29% / 11.70%
- Selected maximum drawdown: -2.59%

## Integrity and constraints

- Same Top 10 as equal weight: **True**
- Factor lookahead violations: **0**
- Weight range observed: 6.28% to 20.00%
- Maximum industry weight: 35.00%
- Score-weight monotonicity violations: 0

## Verdict

- Training selected a dynamic tilt: **True**
- 2026 return exceeded equal weight: **True**
- Research verdict: **passed_exploratory_score_tilt_outperformance**
