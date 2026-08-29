# Retail — sector library

Sits on top of the common credit library. Everything here either changes a
common instruction, adds a measure the common layer cannot state, or names an
operating risk that decides whether the financial profile is durable.

Retail is the sector that most tests the common/sector split, because it does
not merely add metrics — it **overrides** one of the common layer's conventions.
A retailer's stores are its business, and a lease convention that treats them as
an operating cost describes a different company from the one that exists.

## block: sector/retail/applies
kind: router
summary: When to use the retail library, and which of its blocks the question needs.
requires: route/applicability
---
Use this library for an issuer whose revenue comes from selling goods to
consumers through a physical or online estate it operates: general merchandise,
grocery, specialty, apparel, home improvement, e-commerce, restaurant chains and
convenience.

Do **not** use it for: a shopping-centre landlord (that is real estate — the
tenant is the customer and the metric is rent, not sales), a consumer-goods
manufacturer that sells THROUGH retailers, or a retailer whose finance arm is
material enough that the ontology gate raised `captive_finance` and you have not
settled it.

Which blocks a question needs:

- Any leverage or coverage question → `sector/retail/leases` first. It changes
  the figures, so reading them before it is reading the wrong ones.
- A liquidity question → `sector/retail/seasonality`, because a retailer's
  period-end balance sheet is its least representative one.
- A question about durability or the direction of travel →
  `sector/retail/operating_risk`.
- Any question that needs a level → `sector/retail/levels`, which will tell you
  that you do not have one unless someone supplied it.

## block: sector/retail/leases
kind: process
summary: Why lease capitalisation is mandatory here, and what it also changes.
requires: process/adjustments, process/leverage
---
**This overrides the common layer's neutrality on lease convention.** The common
block says to state whichever convention was used. For a retailer, only one is
defensible: leases are capitalised, and leverage that excludes them is not a
conservative view of the same company but a description of a different one.

The reason is not accounting preference. A retailer's store estate is the
operating asset. Whether it owns its stores or leases them changes the balance
sheet and changes nothing about the business, so a leverage measure that moves
between two otherwise identical retailers because one owns and one leases is
measuring the lease decision rather than the credit.

What follows, and each part is required:

- **Debt.** Adjusted debt includes both lease liabilities. If only one component
  is available the figure is a floor: say "at least", and do not divide by it.
- **Earnings must move with it.** A US GAAP retailer keeps operating rent inside
  operating profit, so adding lease debt to its unmodified EBITDA compares a
  gross-of-rent denominator with a debt figure that already counts the rent.
  Where the base supports it, use EBITDAR — earnings before rent — against
  adjusted debt. Where it does not, say the leverage is understated and by
  roughly what.
- **Coverage becomes fixed-charge coverage.** Rent is a fixed charge with the
  same consequence as interest: a retailer that cannot pay it loses the stores.
  Coverage on interest alone understates the burden, sometimes by more than the
  interest itself.

**Sale-leaseback deserves its own sentence.** It raises cash today and raises the
fixed charge forever. An issuer that has been funding itself this way looks like
it is deleveraging on a debt measure that excludes leases and is doing the
opposite on one that includes them. Check whether disposal proceeds in the base
coincide with a rising lease liability.

## block: sector/retail/seasonality
kind: process
summary: Why a retailer's period-end figures are its least representative.
requires: process/liquidity, process/earnings_quality
---
Most retailers earn a disproportionate share of the year's profit in one
quarter and hold their largest inventory just before it. A balance sheet dated
after that quarter shows the year's best cash and lowest inventory; one dated
before it shows the opposite. Neither is the business.

What to do about it:

- **Say which point in the cycle the balance-sheet date is.** A January year end
  follows the peak: cash is at its high, inventory at its low, payables largely
  settled. Reading that as the normal position overstates liquidity, sometimes
  by a wide margin.
- **Peak working-capital need is the liquidity question**, not the period-end
  position. The issuer must fund inventory before it sells it, and the facility
  that covers that gap matters more than the cash on the balance-sheet date.
  If the base cannot show the intra-year peak — and from annual statements it
  cannot — say the liquidity assessment is a point-in-time one.
- **A working-capital contribution to cash flow is suspect in this sector.**
  Retailers can generate cash by shrinking inventory, which is either good
  management or a business selling down its ability to trade. One period cannot
  tell you which; the direction of inventory against the direction of sales
  usually can.
- **Payables are financing.** Vendor terms are a large, cheap, and revocable
  source of funding. A lengthening payables cycle is borrowing from suppliers,
  and it reverses fastest exactly when the issuer can least afford it — which is
  when suppliers become concerned about the issuer.

## block: sector/retail/operating_risk
kind: workflow
summary: What makes a retailer's earnings durable or not, beyond the ratios.
requires: core/evidence
---
The financial profile answers whether the issuer can pay. This answers whether
next year's figures will look like this year's. In retail they often do not, and
the reasons are visible before the numbers move.

Read these where the filing supports them, and say plainly when it does not:

- **Comparable-store sales.** The only demand signal that separates a business
  growing because it is winning from one growing because it opened stores.
  Falling comps with rising total revenue is an issuer buying growth with
  capital spending.
- **Occupancy cost against sales.** Rent is fixed and sales are not. A retailer
  with high occupancy cost has high operating leverage in the direction that
  hurts: a modest sales decline moves straight to the operating line.
- **Inventory turns and markdown exposure.** Slowing turns mean either weakening
  demand or over-ordering, and both end in markdowns. Apparel and seasonal
  goods carry this risk hardest; grocery and convenience least.
- **Format and channel.** A store estate sized for a demand pattern that has
  moved is a fixed cost that cannot be reduced quickly, and lease terms decide
  how quickly. Long remaining lease terms are a liability when the format is
  wrong, whatever they do for continuity when it is right.
- **Concentration.** A supplier that cannot be replaced, a geography that is
  most of the estate, or a category that is most of the margin.

None of these is a credit metric. Each of them decides whether the credit
metrics you computed describe a stable business or a snapshot of one that is
changing.

## block: sector/retail/levels
kind: process
summary: Where the thresholds come from, and what to do when there are none.
requires: core/identity
---
This library ships **no threshold table**, and that is deliberate rather than
incomplete.

The levels at which retail leverage or fixed-charge coverage become concerning
are the property of the rating agencies and the lenders who publish them; they
are behind subscriptions, they differ between agencies, and they move with the
cycle. Reproducing remembered numbers here would give the analyst something that
looks authoritative and is neither current nor attributable.

So:

- **If a house threshold table has been supplied**, use it, name it, and say
  which version. A conclusion resting on a level should say which level.
- **If none has been supplied**, do not borrow one. State the level at which
  your view would change and say explicitly that the level is your own
  judgement, not a sector standard. "I would view this differently above about
  four times lease-adjusted leverage, though that level is mine and not a
  published threshold" is honest, checkable and useful. A remembered agency
  number is none of those.

What you may always do without a threshold, and what usually matters more:

- Compare the issuer against **itself over time**. Direction is a fact.
- Compare it against **named peers on the same convention**, if you have them
  and if their figures were adjusted the same way. A lease-adjusted leverage
  against an unadjusted one is not a comparison.
- Identify the level at which something **breaks** rather than the level at
  which something is rated: the covenant, the facility test, the maturity the
  issuer cannot cover. Those are thresholds you can establish from the
  documents, and they bind whatever an agency thinks.
