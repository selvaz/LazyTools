# Software and technology — sector library

Sits on top of the common credit library.

This sector inverts the usual problem. Most credits are assessed on whether
earnings cover debt; a large software issuer often holds more cash than debt,
and the leverage question answers itself. What does not answer itself is
whether the reported profit is a real one, because the two largest costs in a
software business — the people who write the code and the cost of keeping
customers — are recognised in ways that let profit and cash diverge widely and
for years.

The risk here is rarely the balance sheet. It is the durability of a revenue
base that renews, and the distance between the profit as presented and the
profit after the costs the business cannot stop paying.

## block: sector/software/applies
kind: router
summary: When to use the software library, and which blocks the question needs.
requires: route/applicability
---
Use this library for an issuer whose revenue comes from licensing, subscribing
or hosting software, or from services delivered over one: enterprise software,
SaaS, platforms, internet services and the software half of a mixed technology
issuer.

Do **not** use it for: a hardware manufacturer with a software attachment (the
credit runs on inventory, component supply and a product cycle — that is
industrial), a semiconductor issuer (capital intensity and the fabrication cycle
dominate), or a payments or lending business that calls itself technology while
carrying credit risk on its balance sheet. If the classification gate raised
`captive_finance` or `banking_operations`, settle that before using this library.

Which blocks a question needs:

- Any earnings or margin question → `sector/software/earnings` first. It changes
  what the reported profit means.
- Any question about durability, growth or the direction of travel →
  `sector/software/revenue_base`.
- Leverage, liquidity or a debt-funded transaction →
  `sector/software/balance_sheet`.
- Any question that needs a level → `sector/software/levels`.

## block: sector/software/earnings
kind: process
summary: Why software profit and software cash diverge, and in which direction.
requires: process/earnings_quality, process/adjustments
---
Three mechanisms move reported profit away from economic profit in this sector,
and they do not all point the same way. Say which ones you could test and which
the base could not show.

- **Share-based compensation is a real cost.** It is a large fraction of
  engineering pay, it is added back to cash flow because it consumes no cash,
  and the company then buys back stock with cash to offset the dilution. An
  issuer whose buybacks approximately equal its share-based compensation is
  paying that cost in cash after all, through a different line. Where the base
  carries buybacks, compare them with the scale of the workforce cost before
  treating cash flow as free.
- **Capitalised development moves cost from the income statement to the balance
  sheet.** Software spend that is capitalised raises profit today and creates
  amortisation later. Two otherwise identical issuers with different
  capitalisation policies report different margins. If the base separates
  amortisation of intangibles, that figure is a partial view of what was
  capitalised previously; if it does not, say the policy is unobserved.
- **Deferred revenue is a liability that is good news.** Cash collected before
  the service is delivered sits as a liability and funds the business
  interest-free. A growing deferred balance is a leading indicator of revenue; a
  shrinking one, with revenue still rising, means the business is recognising
  what it already collected. Do not net it against anything, and do not read it
  as debt.

The compound effect: reported operating margin in this sector is a weaker guide
than usual, and cash conversion is a stronger one — provided the add-backs have
been read rather than accepted.

## block: sector/software/revenue_base
kind: workflow
summary: Whether next year's revenue is already largely committed, or not.
requires: core/evidence
---
A subscription business is a contract book. Its credit quality comes from
renewal, and renewal is not visible in an income statement.

Read these where the filing supports them, and say plainly when it does not:

- **Recurring versus one-time revenue.** A licence sale and a subscription look
  the same on the revenue line and behave completely differently in a downturn.
  An issuer transitioning from licence to subscription reports falling revenue
  while improving, which is the single most misread pattern in this sector.
- **Remaining performance obligations or backlog.** Where disclosed, this is the
  closest thing to committed future revenue, and it is far more informative than
  a growth rate. Our base does not carry it; the filing's own narrative may.
- **Customer concentration**, and whether the largest contracts renew on the
  same date. Concentration matters more when the contracts are long and few.
- **Switching cost.** Software embedded in a customer's own operations renews
  almost regardless of satisfaction; software that is a discretionary tool does
  not. This is the difference between a stable revenue base and a subscription
  list.
- **Whether growth is bought.** Serial acquisition can produce a revenue line
  that grows while the underlying products do not. Where the base shows
  acquisition spending against revenue growth, say what share of the growth the
  acquisitions could explain.

None of these is a credit metric. Together they decide whether the coverage you
computed describes next year as well as this one.

## block: sector/software/balance_sheet
kind: process
summary: A net cash issuer, and why that does not end the analysis.
requires: process/leverage, process/liquidity
---
Many issuers in this sector hold more cash and investments than debt. When they
do, the leverage question is answered and the useful questions are elsewhere.

- **Say plainly when the issuer is net cash**, and stop presenting leverage
  ratios as if they were binding. A net-debt-to-EBITDA of less than zero is not
  a finding, it is an artefact.
- **But the cash is not always reachable.** Cash held offshore, or held in
  long-dated investments rather than deposits, is not the same as cash available
  to repay debt next year. If the base separates restricted or non-current
  balances, use only what is genuinely available; if it cannot, say the netting
  is an upper bound.
- **The real leverage risk is an event, not a trend.** These issuers move from
  net cash to materially levered in one transaction — a large acquisition or a
  debt-funded buyback. Read the financial policy: an issuer returning
  substantially all of its free cash flow has no accumulated buffer, whatever
  its current net position says.
- **Capex understates the reinvestment need.** The maintenance spending of a
  software business is largely engineering payroll inside operating expenses,
  not capex. Free cash flow after a small capex number therefore overstates
  discretionary capacity, and the overstatement grows with headcount.

## block: sector/software/levels
kind: process
summary: Where thresholds come from here, and why the usual ones fit badly.
requires: core/identity
---
This library ships **no threshold table**, and in this sector the absence is
more than a sourcing problem.

Leverage thresholds calibrated on asset-backed businesses fit software badly in
both directions. There is little to recover: the assets are contracts and
people, and people leave. So a level that looks comfortable can be supported by
almost nothing in a wind-up, while a highly levered software issuer with a
renewing contract book can service debt through a downturn that would end a
manufacturer.

So:

- **If a house threshold table has been supplied**, use it, name it, and say
  which version.
- **If none has been supplied**, state the level at which your view would change
  and say it is your own judgement. Do not import a remembered agency number.

What is more informative than any leverage level here:

- **Cash conversion after the add-backs have been read** — particularly whether
  buybacks are absorbing the share-based compensation that was added back.
- **The direction of the recurring revenue base**, which decides whether today's
  coverage describes next year.
- **The financial policy**, because the transition from net cash to levered
  happens in one step and is a decision, not a trend.
