# Pharmaceuticals and healthcare — sector library

Sits on top of the common credit library.

A pharmaceutical issuer's cash flows are strong, stable and dated. A patent
expires on a known day, and revenue that was highly profitable becomes revenue
that is barely profitable almost at once. Nothing in a set of financial
statements shows that date, so the common layer's ratios describe a business
whose most important feature is invisible to them.

The second peculiarity is that the largest risks arrive as contingencies rather
than as liabilities: litigation, regulatory action, and reimbursement decisions
made by governments and insurers who are not parties to any contract with the
issuer.

## block: sector/healthcare/applies
kind: router
summary: When to use this library, and which of the sector's very different halves.
requires: route/applicability
---
Use this library for an issuer whose revenue depends on a medicine, a medical
device or the delivery of care: originator pharmaceuticals, generics, biotech
with marketed products, medical devices, hospital and clinic operators, and
managed care.

The sector splits into halves that behave differently, so say which one you are
in before applying anything else:

- **Product issuers** (pharma, biotech, devices) carry patent, pipeline and
  litigation risk. Margins are high, capital intensity is moderate, and the
  cliff is the defining feature.
- **Provider issuers** (hospitals, clinics, care operators) carry payor mix,
  labour cost and occupancy risk. Margins are thin, operating leverage is high,
  and they are much closer to a leased-estate operator than to a manufacturer —
  read `sector/retail/seasonality` and `sector/retail/leases` alongside this if
  the estate is leased.

Do **not** use this library for a health insurer whose balance sheet carries
underwriting reserves. If the classification gate raised `insurance_operations`
or the ontology is `insurer`, the corporate vocabulary does not apply.

Which blocks a question needs:

- Any question about durability, or any forecast beyond the current period →
  `sector/healthcare/exclusivity`. It is the sector's defining risk.
- Earnings quality, margin or acquisition questions →
  `sector/healthcare/rd_and_deals`.
- Anything touching legal or regulatory exposure →
  `sector/healthcare/contingencies`.
- Any question that needs a level → `sector/healthcare/levels`.

## block: sector/healthcare/exclusivity
kind: workflow
summary: The dated cliff that financial statements do not show.
requires: core/evidence, process/cash_generation
---
For a product issuer, exclusivity is the credit. A protected medicine earns a
margin that would be impossible in a competitive market; when protection ends,
generic entry typically removes most of that revenue within a year, and the cost
base does not fall with it.

- **Say explicitly that our base cannot show this.** Patent expiry dates,
  product-level revenue and pipeline stage are not financial-statement items. If
  the filing's narrative carries them, that is where they are; if you have not
  read it, the honest statement is that the single most important variable is
  unobserved. Do not infer it from margins.
- **Concentration is the amplifier.** An issuer whose largest product is a large
  share of revenue has a cliff; one with a broad portfolio has an erosion. The
  same leverage ratio means different things in the two cases, and the
  difference is larger than most ratio differences you will see.
- **Current cash flow is a poor guide to future cash flow here**, in a specific
  and predictable direction: it is too high whenever a major product is
  approaching expiry. A leverage ratio computed on pre-cliff EBITDA understates
  post-cliff leverage by however much the product contributed.
- **The pipeline is the offset, and it is a probability, not an asset.**
  Research spending is a claim on future revenue that may not arrive. Treat it
  as a cost the business cannot stop paying, not as an investment you can net
  against the cliff.
- **Provider issuers have no cliff but have a payor.** Their equivalent risk is a
  reimbursement rate set by a government programme or a large insurer, changed
  administratively, and applying to a large share of revenue at once.

## block: sector/healthcare/rd_and_deals
kind: process
summary: Research spending and acquired growth, and what they do to reported profit.
requires: process/earnings_quality, process/adjustments
---
- **Research and development is maintenance spending, not discretionary
  investment.** An issuer that cuts it improves this year's cash flow and
  shortens its own life. When free cash flow improves while research spending
  falls, say what has happened rather than reporting the improvement.
- **Acquisition is how the cliff is answered**, so this sector's issuers are
  serial acquirers and their reported figures carry the consequences: large
  intangible amortisation, purchase-accounting effects on margins, and revenue
  growth that is bought rather than grown. Where the base separates amortisation
  of intangibles, its size relative to earnings tells you how much of the
  business was acquired.
- **Be careful with adding back intangible amortisation.** It is genuinely
  non-cash, and adding it back is standard. But for an issuer that must keep
  acquiring to replace expiring products, the acquisitions are recurring, and an
  earnings measure that excludes their cost while including their revenue
  flatters a treadmill. Say which convention you used and what it omits.
- **Milestone and contingent consideration** are future cash obligations that
  may sit outside debt. If the base cannot show them, say the debt figure is
  incomplete in a known direction.
- **For providers, labour is the equivalent line.** Staff cost is most of the
  cost base, it is not deferrable, and agency staffing at a premium is the
  visible sign of a shortage that compresses margin quickly.

## block: sector/healthcare/contingencies
kind: process
summary: Liabilities that are real, large, and not on the balance sheet.
requires: process/perimeter, process/liquidity
---
This sector's largest exposures often appear as narrative before they appear as
numbers, and by the time they are numbers the outcome is settled.

- **Product liability and mass litigation can exceed the accrued provision by a
  wide margin**, and settlement is usually paid in cash over a defined schedule.
  A settlement schedule is debt in substance: fixed amounts, fixed dates, no
  discretion. If the base does not carry it, say the leverage figure excludes a
  known future obligation and name it.
- **Regulatory action is a revenue event, not a fine.** A manufacturing
  suspension or a withdrawn approval removes the revenue while leaving the cost
  base, and it happens on the regulator's timetable rather than the issuer's.
- **Pricing and reimbursement decisions** by governments and large payors change
  revenue administratively and across a whole portfolio at once. There is no
  contract to enforce and no notice period to plan around.
- **Say what you checked and what you could not.** Our base is a financial one;
  contingencies live in the notes. "The statements show no provision, and I have
  not read the contingencies note" is a truthful and useful sentence. "There are
  no material contingencies" is one you cannot support from the base.

## block: sector/healthcare/levels
kind: process
summary: Why a leverage level here needs a date attached to it.
requires: core/identity
---
This library ships **no threshold table**, and here the missing element is not
only the level but the date it applies to.

A leverage figure for a product issuer is a statement about a denominator with a
known expiry. The same 2.5x is comfortable for an issuer whose portfolio is
protected for a decade and stretched for one facing a cliff inside the tenor of
the facility being assessed. A threshold that does not distinguish those two is
not a threshold.

So:

- **If a house threshold table has been supplied**, use it, name it, and say
  which version.
- **If none has been supplied**, state the level at which your view would change,
  say it is your own judgement, and say **over what horizon** — because for this
  sector the horizon is the substance of the answer.

What is more informative than a level here:

- **Whether an exclusivity expiry falls inside the tenor** of the debt being
  assessed. If it does, that is the finding, whatever the current ratio says.
- **Revenue concentration in the largest products**, which decides whether the
  expiry is a cliff or an erosion.
- **Research spending as a share of revenue against its own history**, which
  shows whether the offset is being funded or quietly cut.
- **Disclosed settlement schedules**, which are dated obligations you can
  actually place against the maturity ladder.
