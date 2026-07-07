# User Stories & Gherkin Acceptance Criteria
**Version:** v0.1 · **Author:** A2 SDLCRequirementsBAAgent · **Date:** 2026-07-08
**Convention (agreed with A6 QA):** every scenario is tagged with the `@FR-NN` requirement ID(s) it verifies, so QA can auto-link coverage into the RTM (`06_RTM.md`). Persona = single self-directed investor (the owner).

---

## US-1 — Query a ticker and see one explainable call
> As the investor, I want to look up any TW/US ticker and get one consolidated Buy/Sell call with reasoning, so I can decide quickly.

```gherkin
@FR-06 @FR-07 @FR-59 @NFR-02
Scenario: Query a covered ticker returns yesterday's snapshot fast
  Given a completed daily snapshot exists for "2330.TW"
  When I request the dashboard for "2330.TW"
  Then I receive the composite call, Bear/Base/Bull target, confidence and conflict flag
  And a non-empty per-lens breakdown is included
  And the response is served from the snapshot within 5 seconds

@FR-06 @FR-08
Scenario: Query a ticker outside coverage
  Given "ZZZZ" is not covered by the system
  When I request the dashboard for "ZZZZ"
  Then the API returns error code "SECTOR_NOT_COVERED"

@FR-07
Scenario: Reads never trigger live computation
  When I request the dashboard for any ticker
  Then the system reads only the last precomputed snapshot
  And no signal is recomputed on the request path
```

## US-2 — Four scoring lenses produce signals
> As the investor, I want each lens scored −2…+2 so I understand each dimension.

```gherkin
@FR-12 @FR-13 @FR-14 @FR-15 @FR-17
Scenario Outline: Each scoring module yields a bounded signal with metadata
  Given facts are available for module "<module>" on "2330.TW"
  When the daily batch computes signals
  Then module "<module>" produces a signal in the range -2 to +2
  And it persists signal, weight_assigned, weight_effective and status "ok"

  Examples:
    | module        |
    | technical     |
    | fundamental   |
    | chip          |
    | informational |

@FR-15
Scenario: News unavailable defaults to neutral
  Given GDELT news is unavailable for "2330.TW"
  When the informational module runs
  Then the informational signal defaults to neutral (0)
  And its status is recorded appropriately

@FR-16
Scenario: US chip signal labelled as quarterly positioning
  Given ticker "AAPL" uses SEC EDGAR 13F for chip/institutional data
  When the chip module runs
  Then the chip signal is labelled "quarterly positioning"
```

## US-3 — Weighted composite → 5-point call + target + confidence
> As the investor, I want one transparent composite score mapped to a clear call.

```gherkin
@FR-24 @FR-26
Scenario Outline: Composite score maps to the 5-point call
  Given the four module signals blend to composite score <score> with default weights 30/30/25/15
  When the recommendation engine maps the score
  Then the call is "<call>"

  Examples:
    | score  | call        |
    | -1.60  | Strong Sell |
    | -1.00  | Sell        |
    |  0.00  | Hold        |
    |  1.00  | Buy         |
    |  1.60  | Strong Buy  |
  # LOCKED (A3 OD-1 reconciled 2026-07-08): Strong/regular split at ±1.5 →
  # Strong Sell [-2.0,-1.5) · Sell [-1.5,-0.75) · Hold [-0.75,+0.75] · Buy (+0.75,+1.5] · Strong Buy (+1.5,+2.0].
  # The examples above are all consistent with ±1.5.

@FR-25 @FR-47
Scenario: Weights must sum to 100 percent
  When I submit module weights that do not sum to 100%
  Then the config update is rejected with a validation error

@FR-27
Scenario: Target price uses fixed +/-15% band
  Given the engine computes a Base target of 100 for "2330.TW"
  Then the Bear target is 85 and the Bull target is 115

@FR-28
Scenario Outline: Confidence derived from module agreement
  Given module agreement is <agreement> percent
  Then the confidence level is "<level>"

  Examples:
    | agreement | level  |
    | 80        | High   |
    | 60        | Medium |
    | 40        | Low    |

@FR-28
Scenario: Hold call uses Hold-consistent agreement across the whole Hold band
  Given the composite maps to "Hold" (composite in [-0.75, +0.75])
  Then agreement is the percent of surviving modules with absolute signal <= 0.75
  And a surviving module with signal exactly 0 counts as agreeing (Hold-consistent)

@FR-28
Scenario: Non-Hold call uses sign-match agreement
  Given the composite maps to a non-Hold call
  Then agreement is the percent of surviving modules whose signal sign matches the composite sign
  And a surviving module with signal exactly 0 does not count as agreeing

@FR-28
Scenario: Hold born of strong opposing signals scores LOW confidence
  Given the composite maps to "Hold" from strong opposing module signals (all |signal| > 0.75)
  Then agreement is 0 percent
  And the confidence level is "Low"

@FR-29
Scenario: Conflicting Signals flag on wide spread
  Given the signal spread (max - min over surviving scoring signals) is 2.5
  When the recommendation is produced
  Then the "Conflicting Signals" flag is raised

@FR-29
Scenario: Spread exactly 2.0 does not flag (strict boundary)
  Given the signal spread over surviving scoring signals is exactly 2.0
  When the recommendation is produced
  Then the "Conflicting Signals" flag is NOT raised

@FR-27 @FR-34
Scenario: Target degrades honestly when fundamental leg is missing
  Given the fundamental module is unavailable
  When the Base target price is computed from the technical leg only
  Then the target inherits Reduced-Confidence framing
  And it is never presented as a silently thinner full-confidence blend

@FR-27
Scenario Outline: Negative-EPS / invalid-PE guard falls back to technical-only target
  Given the fundamental scoring module is ok
  But <invalid_condition>
  When the Base target price is computed
  Then target_base equals latest_close (technical-only)
  And the row carries Reduced-Confidence framing
  And no negative PE-derived target is shown

  Examples:
    | invalid_condition          |
    | trailing_EPS is 0 or below |
    | peer_median_PE is null     |

@FR-30 @FR-31
Scenario: Every recommendation ships with reasoning and metadata
  When any recommendation is produced
  Then per_module_breakdown is non-empty for all four scoring modules
  And data_completeness and methodology_version are recorded
```

## US-4 — Missing-data degradation (never silent)
> As the investor, I want honest degradation so I never trust a hollow call.

```gherkin
@FR-34 @FR-36 @FR-37
Scenario: One scoring module down renormalises and reduces confidence
  Given the technical module is unavailable for "2330.TW"
  And the other three scoring modules are ok
  When the recommendation is produced
  Then weights are renormalised across the three surviving modules
  And weight_effective differs from weight_assigned
  And data_completeness is 0.75
  And the recommendation is marked "Reduced Confidence"
  And the technical module is flagged "unavailable" in the breakdown

@FR-35 @FR-37
Scenario: Two or more modules down suppresses the call
  Given at least two scoring modules are unavailable for "2330.TW"
  When the recommendation is requested
  Then no composite score, call, or target price is issued
  And composite_signal and target fields are null/absent
  And a suppressed_reason is recorded
  And the app shows "Analysis Only — Insufficient Data"
```

## US-5 — Dashboard/API contract robustness
```gherkin
@FR-57 @FR-58
Scenario: Single failed module returns 200 with flag
  Given exactly one module is unavailable
  When I read the dashboard
  Then the HTTP status is 200
  And all four module keys are present
  And the unavailable module carries status "unavailable"

@FR-58
Scenario: Total outage returns 503
  Given the database is unreachable
  When I read the dashboard
  Then the HTTP status is 503
```

## US-6 — Backtest honesty
```gherkin
@FR-40 @FR-41
Scenario: Benchmark-relative correctness
  Given a Buy call on a TW ticker over the horizon
  When the return exceeds the ^TWII benchmark by more than 2 percentage points
  Then the call is scored "correct"
  And Hold calls are excluded from scoring

@FR-42
Scenario: Insufficient history guard
  Given fewer than 12 months of history exist for a ticker
  When the backtest runs
  Then it shows "insufficient history" instead of a number

@FR-43 @NFR-13
Scenario: Partial-data days never blend into headline
  Given some days in the window are partial-data days
  When rolling accuracy is computed
  Then partial-data days are reported separately
  And the headline accuracy is computed from full-data days only
```

## US-7 — Configuration
```gherkin
@FR-45 @FR-46
Scenario: Change weights and horizon
  When I set module weights to 40/30/20/10 and horizon to 12 months
  Then the configuration is persisted
  And subsequent scoring uses the new weights and horizon
```

## US-8 — Decision log over immutable history
```gherkin
@FR-48 @FR-49 @FR-50 @FR-33 @NFR-12
Scenario: Log a decision without mutating the recommendation
  Given a recommendation exists for "2330.TW" on "2026-07-08"
  When I log decision "followed" with transaction_price 610 and a note
  Then a user_decision_log entry references that recommendation
  And the original recommendation row is unchanged

@FR-51 @NFR-05
Scenario: Transaction price stored encrypted at rest
  When I save a decision with a transaction_price
  Then the transaction_price column is encrypted at rest
```

## US-9 — Daily batch & pipeline status
```gherkin
@FR-52 @FR-53 @FR-54 @NFR-01 @NFR-04
Scenario: Batch isolates adapter failure and still ships a snapshot
  Given the yfinance adapter raises a timeout during the daily batch
  When the batch runs at ~03:00 Taiwan time
  Then the failing source is flagged data-unavailable
  And peer adapters continue
  And a versioned daily snapshot is written before 07:00 Taiwan time

@FR-55 @NFR-11
Scenario: Pipeline status is observable
  When I query "/pipeline/status"
  Then I see the last run date, per-source status and overall health

@FR-56
Scenario: Batch re-run is idempotent for a run date
  Given a snapshot already exists for run_date "2026-07-08"
  When the batch re-runs for "2026-07-08"
  Then the snapshot for that date is safely replaced without duplication
```

## US-10 — Auth (split strategy)
```gherkin
@FR-01 @FR-02 @FR-03
Scenario Outline: Auth-gated access with split token strategy
  Given an unauthenticated client of type "<client>"
  When it calls a protected route
  Then the response is 401
  And after authenticating with "<credential>" the request succeeds

  Examples:
    | client | credential      |
    | web    | session cookie  |
    | iOS    | JWT + refresh   |

@FR-05
Scenario: Expired credential requires re-auth
  Given an expired credential
  When a protected route is called
  Then the response is 401
```

## US-11 — Supply-chain discovery (not scored)
```gherkin
@FR-20 @FR-21 @FR-23
Scenario: Discovery names surfaced but excluded from score
  When I view "2330.TW"
  Then related investable names from the silicon-wafer graph are shown
  And the supply-chain lens does not affect the composite score

@FR-22 @NFR-15
Scenario: Curate the graph without a deploy
  When the wafer graph seed is edited
  Then the change takes effect without an application redeploy
```

## US-12 — Persistent disclaimer (compliance)
```gherkin
@FR-39 @NFR-18 @REG-01
Scenario: Not-financial-advice disclaimer always present
  When I view any recommendation surface
  Then a persistent "Not financial advice" disclaimer is visible
  And it cannot be permanently hidden

@FR-38 @REG-02
Scenario: Past-not-future caveat on projections
  When I view a backtest figure or a projected target
  Then a "past ≠ future" / methodology caveat is shown
```
