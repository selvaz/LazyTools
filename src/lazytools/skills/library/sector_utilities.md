# Regulated utilities — sector library

Sits on top of the common credit library.

A regulated utility is the sector where the common layer is most likely to be
confidently wrong. The common instructions read a company's earnings as the
result of what it sold; a utility's earnings are largely the result of what a
regulator allowed. Leverage that would be alarming in an industrial is normal
here and is *supposed* to be, because the asset base is long-lived and the
revenue is set by a formula rather than won in a market.

None of this makes a utility safe. It moves the risk somewhere the common layer
does not look: to the regulator, to the structure of the group, and to the gap
between money spent and money recovered.

## block: sector/utilities/applies
kind: router
summary: When to use the utilities library, and which blocks the question needs.
requires: route/applicability
---
Use this library for an issuer whose prices, allowed returns or revenue
requirement are set or approved by a public authority: electricity and gas
networks, integrated utilities, water, and regulated pipelines.

Check the base first. The classification gate raises `rate_regulated` when the
filing's own statements show regulatory assets or liabilities. If the ontology
is `utility` **or** that signal is raised, this library applies. If neither is
true and you are reaching for this library because of the issuer's name, stop:
an unregulated generator selling into a merchant power market is an industrial
with commodity exposure, and reading it as a utility gets the risk backwards.

Do **not** use it for: a merchant generator, an energy trader, a utility
holding company whose regulated subsidiaries are a minority of the group, or an
infrastructure fund holding utility stakes.

Which blocks a question needs:

- Any leverage question → `sector/utilities/rate_base` and
  `sector/utilities/structure`. Both change what the number means.
- Cash flow, capex or dividend questions → `sector/utilities/regulatory_lag`.
- Any question that needs a level → `sector/utilities/levels`.

## block: sector/utilities/rate_base
kind: process
summary: Why utility leverage is read against the asset base, not only earnings.
requires: process/leverage, process/adjustments
---
A utility's economics run on a regulated asset base: the authority permits a
return on capital invested and prudently incurred, so the company's earning
power is a function of the assets it is allowed to count, not of demand.

What this changes:

- **Debt against the asset base is a real measure here**, and in many
  jurisdictions it is the one the regulator itself sets a notional level for.
  If the base gives you total assets or property, plant and equipment, say what
  the debt is as a proportion — and say clearly that PP&E is not the regulatory
  asset base, only the closest thing our statements carry. The two differ by
  regulatory adjustments we cannot see.
- **High leverage is not by itself the finding.** A network business with a
  formula-set revenue and a thirty-year asset life supports a debt load that
  would be reckless in a cyclical manufacturer. Report the level and then say
  what actually threatens it: a disallowance, a re-set of the allowed return, or
  a capex programme that outruns the recovery.
- **Regulatory assets and liabilities are not ordinary receivables.** A
  regulatory asset is money spent that the authority has agreed can be recovered
  from future customers. It is real, but it is neither cash nor a claim on
  anyone who can be sued. If the base carries it, do not net it against debt and
  do not count it as liquidity.
- **Depreciation is a recovery mechanism, not just an accrual.** In a utility,
  depreciation approximates the return *of* capital the regulator allows.
  Earnings before it are a much weaker guide to capacity than usual, which makes
  cash-flow measures more informative than EBITDA here.

## block: sector/utilities/regulatory_lag
kind: process
summary: The gap between spending and recovery, which is where utilities fail.
requires: process/cash_generation, process/liquidity
---
Utilities rarely fail because demand disappeared. They fail because they spent
money the regulator had not yet agreed to give back, and had to fund the gap.

- **Negative free cash flow is normal and is not automatically a warning.** A
  utility in an investment cycle spends more than it earns for years by design,
  and funds it with debt and equity because the regulator will allow a return on
  it. Say the cycle is what you are looking at rather than reporting a cash
  shortfall as distress.
- **The question is whether the spending is being recovered, and how fast.** A
  widening gap between capex and operating cash flow across periods, with the
  allowed return unchanged, is the pattern that matters. One period cannot show
  it; if the base has only one, say so.
- **Fuel and commodity pass-through creates a working-capital shock, not an
  earnings shock.** When input prices spike, a utility pays now and recovers
  later through a tariff mechanism. Its margin may be untouched while its
  liquidity is severely tested. Read a large working-capital outflow against
  input prices before reading it as deterioration.
- **The dividend is the first thing to look at when the gap widens.** A utility
  funding both a capex programme and an unreduced dividend from debt is making a
  choice, and it is the choice that precedes most utility downgrades.

## block: sector/utilities/structure
kind: process
summary: Which entity in the group owes the debt, which is never incidental here.
requires: process/perimeter
---
This sector makes the common perimeter block load-bearing rather than routine.
Utility groups are deliberately structured, and consolidated figures blend
entities with genuinely different claims.

- **Holding-company debt is structurally subordinated to operating-company
  debt.** The regulated subsidiary owns the assets and earns the revenue; the
  holding company owns the subsidiary. Its creditors are paid after the
  subsidiary's, from dividends the subsidiary is permitted to pay.
- **The regulator can restrict those dividends**, and does so exactly when the
  subsidiary is weak — which is when the holding company most needs them.
  Consolidated coverage that assumes cash moves freely up the group assumes away
  the mechanism that makes holdco debt riskier.
- **Say which entity our figures describe.** If `perimeter_status` is
  unavailable — and from consolidated statements it usually is — say the
  analysis is group-level and that any netting of cash against debt is an upper
  bound. Do not present a consolidated ratio as if it were the borrower's.
- **A ring-fenced or securitised financing** (`non_recourse` in the signals)
  carries debt that is not a claim on the group and cash that is not available
  to it. Reading either into a group ratio is wrong in both directions.

## block: sector/utilities/levels
kind: process
summary: Where utility thresholds come from, and what to do without them.
requires: core/identity
---
This library ships **no threshold table**, for the same reason as every other
sector library here, and with a sector-specific twist worth stating.

Utility thresholds are unusually jurisdiction-dependent: the level of leverage a
regulator tolerates, and the coverage it expects, are set by the regulatory
framework rather than by the sector. A level carried over from one country's
network regime to another's is not conservative, it is unfounded.

So:

- **If a house threshold table has been supplied**, use it, name it, say which
  version, and say which jurisdiction it was calibrated on.
- **If none has been supplied**, state the level at which your view would change
  and say it is your own judgement. Do not import a remembered agency number,
  and do not import a level from a different regulatory regime.

What binds regardless of any threshold, and is usually more informative:

- The **regulatory determination itself** — the allowed return, the period it
  runs for, and when it is next reset. That date is a real trigger.
- The **covenant or licence condition**, where the filing discloses one. A
  licence that requires an investment-grade rating is a threshold with a
  mechanism behind it.
- The issuer against **its own history** across the capex cycle.
