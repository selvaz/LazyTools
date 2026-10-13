# Esempio di nota di credito prodotta dall'agente

**Emittente:** Walmart Inc. (CIK 0000104169)
**Periodo:** 2024-02-01 → 2025-01-31 · **Filing:** 0000104169-25-000021
**Ontologia:** corporate · **Segnali aperti:** nessuno
**Cutoff informativo:** 2025-06-01 · **Libreria:** comune + settore retail
**Modello:** deepseek-v4-flash

Nessuna cifra è stata fornita dal modello: ogni valore è stato letto dal codice
dai prospetti renderizzati che la SEC pubblica col filing.

---

## La nota

Now I'll assess Walmart's credit. The ontology is corporate, no open signals, so the corporate vocabulary applies. Let me work through leverage, coverage, liquidity, and durability.

**Key figures from the base:**
- house_operating_ebitda (derived): $42.3bn — operating income + operating D&A
- house_adjusted_debt (derived): $60.1bn — reported debt plus both lease liabilities
- house_net_debt (derived): $51.1bn — adjusted debt less cash
- Interest expense (reported): $2.2bn charged; cash interest $2.7bn
- CFO (reported): $36.4bn; house_ffo $33.7bn
- focf $12.7bn; dcf $1.5bn
- Maturities: y1 $2.6bn through y5 $2.0bn, thereafter $22.9bn

**Lease capitalisation issue:** Walmart is US GAAP. Operating rent stays inside operating profit, so EBITDA is gross-of-rent while adjusted debt already counts the rent. The base gives no rent/EBITDAR figure, so the true lease-adjusted leverage is understated. This is a critical caveat.

Let me compute the leverage and coverage figures.

Adjusted debt / house EBITDA = 60.1/42.3 = ~1.42x
Net debt / house EBITDA = 51.1/42.3 = ~1.21x

But these understate because of the rental burden in the denominator.

Fixed-charge coverage: no rent figure available, so I can only give interest coverage. Cash interest coverage: house_ffo / cash interest = 33.7/2.7 = ~12.5x. Charged interest coverage: 42.3/2.2 ≈ 19x.

Let me write the assessment.

Let me note the same-convention comparison: Walmart peer Target would be comparable, but I don't have those figures.

Durability/operating risk: comps, occupancy cost, inventory turns not in base. Available signals from the base: nothing on comps. Debt maturity shape is good (spread, no wall).

Let me compose concise assessment.

**Lease-adjusted leverage** — house_adjusted_debt is reported debt plus both lease liabilities: $60.1bn. Against house EBITDA $42.3bn = 1.42x. Net basis (after $9.0bn cash) 1.21x. But both understate: US GAAP keeps rent in the denominator. I'd need EBITDAR to do this right; without it, leverage is understated. Since I have no threshold, I note my view would change above ~3x adjusted leverage, and that level is mine.

**Coverage** — cash interest $2.7bn, FFO $33.7bn → ~12x cash interest coverage on cash basis. Fixed-charge coverage can't be computed (no rent given), which is the bigger constraint for a retailer. Coverage slower to matter here.

**Liquidity** — January year-end follows peak season, so cash $9.0bn is at its high point. Maturities are well spread: $2.6bn y1, $3.5bn y2, $1.7bn y3, $3.3bn y4, $2.0bn y5, then $22.9bn thereafter. No committed facility disclosed, no undrawn availability. Readily available cash covers y1 and y2 maturity. But seasonality: peak working capital need is before the holiday quarter, and the base can't show the intra-year peak. Liquidity assessment is point-in-time.

**Durability** — operating risk block: comps, occupancy cost, inventory turns all unavailable from this base. CFO $36.4bn vs EBITDA $42.3bn — reasonable conversion. DCF only $1.5bn after dividends $6.7bn and buybacks $4.5bn — so net of shareholder distributions, retained cash before acquisitions is thin relative to the $22.9bn long-end maturity and ongoing capex needs. FOCF $12.7bn.

Let me finalize.

The conclusion: a strong credit. Leverage low even understated, coverage strong on cash interest, liquidity adequate but benignly. The main caveats: lease-adjusted leverage understated (no EBITDAR), peak-season timing, thin DCF, and the $22.9bn back-end maturity relying on refinancing over the 5-year horizon.

Let me write it.## Credit assessment — Walmart Inc. (5-year senior unsecured facility)

**Perimeter note.** The base's `perimeter_status` is unavailable; this is consolidated leverage. Netting cash against debt assumes consolidated cash can reach the debt, which consolidation does not establish. I treat net figures as upper bounds, not the position.

### Leverage — low, and structurally understated
- **Adjusted debt** (derived: reported financial debt $39.1bn plus both lease liabilities, $14.3bn operating + $6.7bn finance) is **$60.1bn**; against **house EBITDA** $42.3bn → **1.4x**. Net of the $9.0bn cash, **1.2x**.
- This understates the true burden. Walmart reports US GAAP, so operating rent stays inside operating income while adjusted debt already counts the leases. Comparing lease-inclusive debt to gross-of-rent EBITDA needs **EBITDAR**, which this base does not provide. The correct lease-adjusted leverage is higher than 1.4x, and I cannot say by how much.
- No threshold table was supplied. The level at which my view would change is roughly **3x on a properly EBITDAR-adjusted basis** — that is my judgement, not a published standard. Even with the absent rent adjustment, leverage this far from that level is low.
- **Maturity shape is the counterpart to the low level:** the ladder is benign — $2.6bn (y1), $3.5bn (y2), $1.7bn (y3), $3.3bn (y4), $2.0bn (y5) — with **$22.9bn in "thereafter."** No near wall; the refinancing test is the back-end concentration, which sits beyond the facility horizon.

### Coverage — strong on cash interest; the rent test is the missing constraint
- **House FFO** (derived) of $33.7bn covers **cash interest** $2.7bn at roughly **12x**. The gap between cash interest ($2.7bn) and interest charged ($2.2bn) reflects lease and other cash interest; cash-basis coverage is the right test here and is comfortable.
- What I cannot compute is the binding retail constraint: **fixed-charge coverage** including rent. Rent data is absent from the base. For a retailer, interest-only coverage understates the burden because losing stores to unpaid rent is as destructive as missing a bond payment. This is the single largest unknown in the credit.

### Liquidity — adequate but point-in-time and peak-biased
- The January year-end **follows the holiday peak**: cash is at its seasonal high, inventory at its low. Read as the normal position, $9.0bn cash overstates liquidity.
- Ready cash covers the y1 ($2.6bn) and roughly the y2 ($3.5bn) maturities on its own. No committed facility or undrawn availability is disclosed (both `unavailable`), so uncommitted lines cannot be counted and the true refinancing headroom is unknown. The intra-year working-capital peak cannot be shown from annual statements; this is a point-in-time assessment.
- Liquidity here is not the concern. A retailer fails when it cannot fund inventory before the peak season, and the disclosed CFO trajectory does not flag that risk.

### Durability and cash generation
- CFO of $36.4bn against EBITDA of $42.3bn is an efficient conversion — earnings are becoming cash. **FOCF** (derived: CFO less capex $23.8bn) of **$12.7bn** is the honest capacity to repay debt from operations.
- Net of shareholder distributions it thins sharply: **DCF** (derived, after dividends $6.7bn and buybacks $4.5bn) is only **$1.5bn** before acquisitions. The shareholder payout (~$11bn/yr) materially exceeds annual capex-funded growth in retained cash. That is not a solvency problem — leverage is low — but it means the $22.9bn back-end maturity and any future deleveraging will compete with a large, entrenched payout.
- The operating-risk signals this base cannot test: comparable-store sales, occupancy cost against sales, and inventory turns are all absent. Walmart's scale dulls these, but the direction of comps — whether it is growing by winning or by adding square footage — is not established here.

### Conclusion
Walmart is a strong credit for a five-year senior unsecured facility: leverage is low (and nearer to low even after the unavailable rent adjustment), cash-interest coverage is multi-double-digit, the five-year maturity ladder is flat and manageable, and cash conversion is efficient. The facility is low-risk.

**Drivers:** (1) lease-adjusted leverage under 3x; (2) cash interest covered comfortably from FFO; (3) no material maturity inside five years.

**What would change it:** a shift in comparable-store sales that makes recent growth look capital-bought rather than organic; or a path to lease-adjusted leverage above ~3x on an EBITDAR basis (my level, not a sector standard).

**Downside mechanism:** not a liquidity or coverage event — both are too strong. The realistic path is slower than currently, a sales deceleration that worsens the cash-conversion ratio while the $11bn/yr payout stays sticky, leaving less to fund the $22.9bn back-end refinancing.

**What I could not establish:** the rent figure (so true lease-adjusted leverage and fixed-charge coverage are unknown and understated), and — because none was disclosed — committed-facility availability, without which refinancing headroom is an assumption. Both are the respect in which this is a strong credit with a larger unknown than the headline ratios suggest.

---

## La base normalizzata da cui ha lavorato

38 elementi utilizzabili su 45 popolati.

| elemento | valore (USD) | stato | provenienza / motivo del blocco |
|---|---:|---|---|
| `amortisation_intangibles` | — | unavailable | not present in the filing's statements: No separate line for amortisat |
| `capex_intangibles` | — | unavailable | not present in the filing's statements: No line for cash paid for inta |
| `capex_ppe` | 23,783,000,000 | reported | Consolidated Statements of Cash Flows: 'Payments for property and equi |
| `cash_acquisitions` | 1,896,000,000 | reported | Consolidated Statements of Cash Flows: 'Payments for business acquisit |
| `cash_and_equivalents` | 9,037,000,000 | reported | Consolidated Balance Sheets: 'Cash and cash equivalents' |
| `cash_divestiture_proceeds` | 432,000,000 | reported | Consolidated Statements of Cash Flows: 'Proceeds from the disposal of  |
| `cash_interest_paid` | 2,739,000,000 | reported | Consolidated Statements of Cash Flows: 'Interest paid' |
| `cash_plus_restricted` | — | unavailable | not present in the filing's statements: Only cash and cash equivalents |
| `cash_taxes_paid` | 5,884,000,000 | reported | Consolidated Statements of Cash Flows: 'Income taxes paid' |
| `cfo` | 36,443,000,000 | reported | Consolidated Statements of Cash Flows: 'Net cash provided by operating |
| `committed_facility` | — | unavailable | not present in the filing's statements: No committed facility size is  |
| `current_long_term_debt` | 2,598,000,000 | reported | Consolidated Balance Sheets: 'Long-term debt due within one year' |
| `dcf` | 1,478,000,000 | derived | focf - dividends_paid - share_repurchases |
| `debt_maturity_thereafter` | 22,914,000,000 | reported | Short-term Borrowings and Long-term Debt - Schedule of Debt Maturities |
| `debt_maturity_y1` | 2,598,000,000 | reported | Short-term Borrowings and Long-term Debt - Schedule of Debt Maturities |
| `debt_maturity_y2` | 3,451,000,000 | reported | Short-term Borrowings and Long-term Debt - Schedule of Debt Maturities |
| `debt_maturity_y3` | 1,741,000,000 | reported | Short-term Borrowings and Long-term Debt - Schedule of Debt Maturities |
| `debt_maturity_y4` | 3,340,000,000 | reported | Short-term Borrowings and Long-term Debt - Schedule of Debt Maturities |
| `debt_maturity_y5` | 1,955,000,000 | reported | Short-term Borrowings and Long-term Debt - Schedule of Debt Maturities |
| `dividends_paid` | 6,688,000,000 | reported | Consolidated Statements of Cash Flows: 'Dividends paid' (presented as  |
| `finance_lease_current` | 800,000,000 | reported | Consolidated Balance Sheets: 'Finance lease obligations due within one |
| `finance_lease_noncurrent` | 5,923,000,000 | reported | Consolidated Balance Sheets: 'Long-term finance lease obligations' |
| `finance_lease_total` | 6,723,000,000 | derived | finance_lease_current + finance_lease_noncurrent |
| `focf` | 12,660,000,000 | derived | cfo - house_capex |
| `gross_interest_expense` | 2,249,000,000 | reported | Consolidated Statements of Income: 'Debt' |
| `house_adjusted_debt` | 60,114,000,000 | derived | reported debt plus both lease liabilities (house convention: leases ca |
| `house_capex` | 23,783,000,000 | derived | capex_ppe |
| `house_ffo` | 33,698,000,000 | derived | house_operating_ebitda - cash_interest_paid - cash_taxes_paid |
| `house_net_debt` | 51,077,000,000 | derived | house_adjusted_debt - readily_available_cash |
| `house_operating_ebitda` | 42,321,000,000 | derived | operating_income + operating_da_total |
| `impairment` | — | unavailable | not present in the filing's statements: No impairment line is presente |
| `long_term_debt_noncurrent` | 33,401,000,000 | reported | Consolidated Balance Sheets: 'Long-term debt' |
| `operating_da_total` | 12,973,000,000 | reported | Consolidated Statements of Cash Flows: 'Depreciation and amortization' |
| `operating_income` | 29,348,000,000 | reported | Consolidated Statements of Income: 'Operating income' |
| `operating_lease_current` | 1,499,000,000 | reported | Consolidated Balance Sheets: 'Operating lease obligations due within o |
| `operating_lease_noncurrent` | 12,825,000,000 | reported | Consolidated Balance Sheets: 'Long-term operating lease obligations' |
| `operating_lease_total` | 14,324,000,000 | derived | operating_lease_current + operating_lease_noncurrent |
| `readily_available_cash` | 9,037,000,000 | derived | cash_and_equivalents |
| `reported_financial_debt` | 39,067,000,000 | derived | short_term_borrowings + current_long_term_debt + long_term_debt_noncur |
| `restricted_cash` | — | unavailable | not present in the filing's statements: No restricted cash line is pre |
| `revenue` | 680,985,000,000 | reported | Consolidated Statements of Income: 'Total revenues' |
| `share_repurchases` | 4,494,000,000 | reported | Consolidated Statements of Cash Flows: 'Purchase of Company stock' (pr |
| `short_term_borrowings` | 3,068,000,000 | reported | Consolidated Balance Sheets: 'Short-term borrowings' |
| `undrawn_availability` | — | unavailable | not present in the filing's statements: No undrawn availability amount |
| `working_capital_movement` | -1,106,000,000 | reported | Consolidated Statements of Cash Flows: 'Receivables, net' |
