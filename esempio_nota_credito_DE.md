# Deere & Co — nota di credito (libreria industrials)

**Segnali aperti:** ['captive_finance', 'consolidated_vie', 'banking_operations'] · **Filing:** 0001104659-25-122321
**Periodo:** 2024-11-02 → 2025-11-01

---

I need to assess Deere & Co for a five-year senior unsecured facility. Let me work through the signals and instructions. The base raises `captive_finance`, `consolidated_vie`, and `banking_operations`. Let me note the critical blocking issues in the base before I write anything.

Key findings from the base:

1. **`captive_finance` signal is raised** — Deere's finance arm (John Deere Financial) is inside the consolidation. Per `sector/industrials/captive_finance`, I cannot report a consolidated leverage ratio as usable. The base is built from consolidated statements and carries no segment-level separation.

2. **`reported_financial_debt` is a `lower_bound`** — one debt component was not found, so adjusted debt and net debt are both `unavailable`. Even the consolidated leverage cannot be computed.

3. **`committed_facility` and `undrawn_availability` are `unavailable`** — no committed line disclosed in the statements. Liquidity cannot be established.

4. **`perimeter_status` is `unavailable`** — cannot tell when debt was raised relative to earnings across an acquisition.

Let me build what I *can* say without violating the evidence rules.

Usable elements:
- House operating EBITDA = 7,334 (derived, from operating income + operating D&A) — usable but it is house convention, no agency thresholds apply.
- Cash interest paid = 3,080 (reported)
- Cash taxes paid = 1,647 (reported)
- CFO = 7,459 (reported)
- House capex = 4,228 (derived, capex_ppe 1,360 + capex_intangibles 2,868). Note the cap-ex figure includes Cost of equipment on operating leases acquired (dealer-flooring/fleet assets) — this is a finance-arm lease programme, so the capital spend notion is a mixed figure here too.
- FOCF = 3,231 (derived, CFO - house capex)
- DCF = 373 (derived, FOCF - dividends 1,720 - buybacks 1,138)
- Cash 8,276 (reported); restricted 257.

What I can compute cleanly:
- Interest coverage on cash interest: house EBITDA 7,334 / cash interest 3,080 ≈ 2.4x.
- House FFO (EBITDA - cash interest - cash tax) = 2,607 (derived).
- FOCF 3,231, DCF 373.

Leverage: cannot compute. Both adjusted debt and net debt are `unavailable`. The consolidated financial debt is a lower_bound (≥50.14bn per the two reported components, but one component missing so it's a floor). Even as a floor, this is consolidated and includes the captive finance book. I must not present a consolidated leverage ratio.

Let me write the assessment being concrete and brief.

Let me structure the assessment covering: leverage, coverage, liquidity, durability, and the conclusion driver/trigger form.

---

Let me draft.

**Leverage.** I must state that I cannot supply a leverage figure. Reported financial debt is a floor (≥$50.1bn from current maturities 6.6bn + noncurrent 43.5bn, but one component missing), and even that floor is the consolidated figure blending the captive finance arm. Per captive_finance discipline, a consolidated debt-to-industrial-earnings ratio is not usable — it errs in the knowable direction of overstating leverage against the industrial book. What would resolve it: segment disclosure separating industrial vs financial-services debt and EBITDA (Deere typically publishes this in a supplementary schedule, not the face statements). No industrial-only leverage can be stated from this base.

**Coverage.** Comment on affordability. House EBITDA 7,334 against cash interest 3,080 gives ≈2.4x cash-interest coverage. But note EBIT/interest would be lower. Cover on cash interest answers whether money left: ~2.4x. Also note coverage is silent on principal. Note house EBITDA includes finance-arm earnings and omits the finance arm's interest as its principal cost — so this is a consolidated coverage, muted by the captive.

Actually let me reconsider: The coverage using house_operating_ebitda / cash_interest_paid. House EBITDA = operating_income (6,257) + operating D&A (1,077) = 7,334. EBITDA/interest = 7334/3080 = 2.38x. Cash interest covers. House FFO = 2,607.

I should note the captive caveat applies to coverage too — finance-arm interest is embedded in expense while consolidated EBITDA excludes it partly; the coverage is a consolidated number.

**Liquidity.** Cannot establish committed facility or undrawn availability (both unavailable). Cash 8,276 readily available. Current debt maturities within a year: short-term securitization borrowings 6,596 (reported) — that's the captive's own securitisation funding, which as a funding channel for the captive is exposed to market closure exactly when equipment demand weakens. Cannot build a source/uses picture without the committed facility. No covenant headroom as no covenant definitions or net debt.

**Durability.** Cycle position: single period, cannot place on cycle. Working capital movement (receivables) 1,084 inside CFO — positive contribution helping CFO; without revenue direction cannot classify as downturn release or growth funding. The captive correlation risk (funding closes when customers defer) is the real durability concern. Aftermarket/service stabiliser not read from this base. Fixed-cost manufacturing gives operating leverage: revenue sensitivity amplifies on the operating line.

Also note: house_capex includes "Cost of equipment on operating leases acquired" (2,868) which is the finance arm's lease-fleet acquisition, folded into capex. So FOCF/DCF figures mix industrial capex with finance-arm fleet investment — another reason the consolidated cash figures are not cleanly the industrial cash generation.

Let me be concrete and brief. I'll structure it.

Let me draft the conclusion in the required form. I should present what is usable, what is blocked, and the triggers.

Let me consider giving a coverage figure with the caption quality noted. EBITDA 7,334 / cash interest 3,080 = 2.38x.

Also can give DCF 373 as residual after dividends and buybacks spread across maturity... but with debt floor ~50bn, residual of 373 is thin relative to a 50bn debt stock.

Actually the more honest read: FOCF 3,231 is the operating capacity after capex. DCF 373 is what's left after paying out 2,858 to shareholders (dividends 1,720 + buybacks 1,138). The debt stock (~50bn floor, consolidated) is far larger than annual FOCF. That's a durability point — but again, consolidated with finance arm.

Let me write the assessment.This base cannot carry the assessment most of it was asked to provide. Rather than pad around the blocks, I'll give you what is usable, what is not, and what would change it.

## Leverage — not computable from this base

I cannot supply a leverage figure. Two independent reasons, either one fatal:

- **The captive finance arm is inside the consolidation.** `captive_finance` is raised, and the base is built from consolidated statements with no segment separation. A consolidated debt-to-industrial-earnings ratio blends two businesses with different economics and errs in a knowable direction — overstating leverage against the industrial book (finance debt against industrial earnings). Per the captive discipline I will not quote it with a caveat attached; it is not a usable metric.
- **The debt figure itself is blocked.** `reported_financial_debt` is a `lower_bound` — one component was not found, so at least $50.1bn (current securitisation borrowings 6,596 + long-term borrowings 43,544), but a floor cannot enter a ratio. Adjusted debt, net debt, and house net debt are all therefore `unavailable`.

What would resolve it: segment disclosure separating industrial from financial-services debt and EBITDA. Deere typically publishes this in a supplementary schedule rather than the face statements; it is not in this base.

## Coverage — usable, but a consolidated number

Cash-interest coverage is the one metric that computes cleanly: house operating EBITDA 7,334 (derived, from operating income plus operating D&A) against cash interest paid 3,080 gives **~2.4x**. It answers whether the money left — it does.

But this is coverage of the *consolidated* group, muted by the captive (the finance arm's interest sits in expense while consolidated EBITDA excludes it). And coverage is silent on principal: ~2.4x interest comfort says nothing about the ~$50bn debt stock, which is the maturity-ladder question this base cannot answer (the ladder is not present).

## Cash generation — mixed perimeter

CFO 7,459 less house capital spend 4,228 gives FOCF 3,231; less dividends 1,720 and buybacks 1,138 gives DCF 373. Two caveats that matter for a durable credit:

- `house_capex` (4,228) includes 2,868 of "Cost of equipment on operating leases acquired" — the finance arm's lease-fleet investment, folded into capital spend. So both FOCF and DCF mix industrial capex with captive-fleet outlay. Not cleanly industrial cash generation.
- The whole debt stock (~$50bn floor) sits against a DCF of 373 after distributions. Even conceding the captive distorts the denominators, the residual after shareholder cash-outs is thin relative to the stock that must be refinanced.

The working-capital contribution of 1,084 (receivables, reported) sits inside CFO, but with a single period and no revenue direction I cannot classify it as downturn liquidation or recovery-funded growth. Untested either way.

## Liquidity — cannot be established

No committed facility and no undrawn availability are disclosed in these statements (both `unavailable`). Cash is 8,276 (reported), restricted 257. Without the committed line I cannot build a source/uses picture or compute covenant headroom — the contractual definitions are absent, so headroom is unknown, not approximated.

The one structural liquidity point the base does support: the 6,596 of *short-term securitisation borrowings* is the captive's own funding channel, and that channel closes precisely when equipment demand weakens and customers defer purchases. That correlation — not the size of the debt — is the actual risk in this structure.

## Durability

I cannot place this period on Deere's cycle from a single period, so the ratios are a snapshot. Fixed manufacturing cost gives operating leverage: revenue sensitivity reaches the operating line amplified, so the leverage denominator is the most likely thing to move. The captive's funding-market exposure and the working-capital swing on a recovery (which absorbs cash even as orders return) are the mechanisms to watch. Service-and-aftermarket revenue would be a stabiliser, but this base does not separate it.

## Conclusion

**View:** On the evidence provided, I cannot express a view on Deere as a five-year unsecured credit — the two loading questions (industrial-only leverage, and liquidity) are both blocked by the absence of finance-segment separation and committed-facility disclosure.

**Drivers it would rest on, once available:** (1) industrial-only debt/EBITDA excluding the captive, (2) committed facility and maturity ladder against the captive's securitisation exposure, (3) where the period sits in the cycle to interpret any leverage level.

**What would change it (testable):**
- Segment disclosure separating industrial vs. financial-services debt and EBITDA — without it, no leverage view is possible.
- A committed-facility size and maturity schedule disclosed — without them, no liquidity view is possible.
- A second/third period to establish the working-capital and cycle direction.

**Downside mechanism:** equipment demand softens → the captive's securitisation funding tightens at the same moment → the group's consolidated cash picture deteriorates precisely as industrial revenues fall through operating leverage → refinancing the ~$50bn stock, much of it finance-arm funding, becomes the constraint. What Deere would do: draw committed lines, cut share-cash-outs, defer capex (it is deferrable, and it is the liquidity buffer — at a later cost).

**What I could not establish:** industrial-only leverage and liquidity. Those two blocks, not any missing detail, are the finding of this assessment.
