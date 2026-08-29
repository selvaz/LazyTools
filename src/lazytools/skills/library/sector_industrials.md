# Industrials and capital goods — sector library

Sits on top of the common credit library.

Two things distinguish this sector, and the second one breaks arithmetic rather
than judgement.

The first is the cycle: these issuers sell equipment whose purchase a customer
can defer, so revenue falls faster than the economy and recovers later, and a
ratio computed at one point in the cycle says little about the level the issuer
lives at.

The second is the captive finance arm. Many equipment makers lend to their own
customers, and the consolidated statements then merge a manufacturer with a
lender. Every leverage ratio computed on those combined figures is meaningless —
not imprecise, meaningless — because a finance book's debt funds earning assets
and an industrial's debt does not.

## block: sector/industrials/applies
kind: router
summary: When to use this library, and the check that must come first.
requires: route/applicability
---
Use this library for an issuer that manufactures durable goods sold to
businesses or consumers: machinery, capital equipment, aerospace and defence
suppliers, vehicles, building products, electrical equipment and diversified
industrials.

**Check the classification signals before anything else.** If the gate raised
`captive_finance`, go to `sector/industrials/captive_finance` first and do not
report any leverage figure until you have. This is not a refinement; a
consolidated debt figure that blends a finance book is wrong by a factor, not by
a margin. The gate reads the filing's own report index, so a raised signal means
the filer itself presents finance-segment statements.

Do **not** use this library for: a pure defence prime whose revenue is a
government programme book (the risk is programme and appropriation risk, closer
to a contractor), a distributor that holds inventory but manufactures nothing,
or an issuer whose finance arm is the larger business.

Which blocks a question needs:

- **Any** leverage or coverage question, when `captive_finance` is raised →
  `sector/industrials/captive_finance`, first and without exception.
- Margin, earnings or through-cycle questions → `sector/industrials/cycle`.
- Cash flow, working capital or backlog → `sector/industrials/working_capital`.
- Any question that needs a level → `sector/industrials/levels`.

## block: sector/industrials/captive_finance
kind: process
summary: Why a consolidated ratio is meaningless when a finance arm is inside it.
requires: process/perimeter, process/leverage
---
A captive finance company borrows in order to lend. Its debt is matched by
receivables that generate the income servicing that debt, and a healthy finance
book is *supposed* to carry leverage that would be alarming in a factory.
Consolidating the two produces a debt figure that is the sum of two unlike
things and an EBITDA that omits the interest expense which is the finance arm's
principal cost.

What this requires of you:

- **Say the consolidated ratio is not usable, and say why.** Do not compute it
  with a caveat attached; the number will be quoted and the caveat will not.
  Our base is built from consolidated statements, so unless it carries
  segment-level figures, industrial-only leverage cannot be established from it.
  That is a finding, not a failure — state it as the binding limitation.
- **Name what would resolve it**: the segment disclosure separating industrial
  from financial-services debt, EBITDA and receivables. Most such filers publish
  it, usually in a supplementary schedule rather than the face statements.
- **The direction of the error is knowable even when the size is not.**
  Including finance debt against industrial earnings overstates leverage;
  including finance earnings against industrial debt understates it. Say which
  way the available figure errs.
- **Do not net the finance arm's receivables against the group's debt.** They
  are the finance arm's assets, pledged in substance to its own funding, and
  they are not available to repay the manufacturer's creditors.
- **Watch the funding channel, not just the size.** A captive that funds itself
  in commercial paper or securitisation is exposed to a market that closes
  quickly, and it closes at the moment the parent's customers are already
  deferring purchases. That correlation is the actual risk in this structure.

If the gate also raised `non_recourse` or `consolidated_vie`, the same
discipline applies: debt that is not a claim on the group, and assets that are
not available to it, must not enter a group ratio in either direction.

## block: sector/industrials/cycle
kind: workflow
summary: Reading a cyclical issuer at one point in its cycle.
requires: process/earnings_quality, core/evidence
---
A single period's margin from a cyclical manufacturer is a point on a curve, and
the curve has a wide amplitude. The common layer's ratios are all computed at
that point.

- **Say where in the cycle you believe the period sits, and on what evidence.**
  If the base holds one period only, say the level cannot be placed and that the
  ratios are a snapshot. This is more honest and more useful than a confident
  ratio.
- **Operating leverage is the mechanism.** These issuers carry substantial fixed
  manufacturing cost, so a revenue decline reaches the operating line amplified.
  A modest-looking sales sensitivity translates into a large earnings
  sensitivity, and the leverage ratio moves through the denominator.
- **Peak-cycle earnings make leverage look low exactly when it is most likely to
  rise.** An issuer that levered up on peak EBITDA carries the debt into the
  trough with a smaller denominator. Where the base allows a comparison against
  the issuer's own earlier periods, do it; direction is a fact and level is a
  judgement.
- **Backlog and order intake are the forward signal**, and they turn before
  revenue does. Our base does not carry them; the filing's narrative may. Say
  whether you could read them.
- **Aftermarket and service revenue is the stabiliser.** Parts and servicing
  continue when equipment sales stop, and an issuer with a large installed base
  has a floor that a pure equipment seller does not. Where the filing separates
  it, that share is one of the most informative numbers in this sector.

## block: sector/industrials/working_capital
kind: process
summary: Where the cash goes in a recovery, and where it comes from in a downturn.
requires: process/cash_generation, process/liquidity
---
Industrial working capital moves against the cycle in a way that flatters the
bad years and punishes the good ones.

- **A downturn releases cash.** Inventory is run down and receivables are
  collected while new production slows, so operating cash flow can hold up or
  even improve while the business deteriorates. Do not read that release as
  resilience; it is a one-time liquidation of working capital that cannot repeat.
- **A recovery consumes cash.** Rebuilding inventory and funding new receivables
  absorbs cash precisely when orders return. An issuer emerging from a trough
  with thin liquidity can be solvent, profitable and unable to fund its own
  recovery.
- **Read the working-capital contribution against the revenue direction.** A
  large positive contribution with falling revenue is the downturn pattern; a
  large negative one with rising revenue is the recovery pattern. Both are
  normal, and both are misread as their opposite.
- **Customer advances behave like deferred revenue**, not like debt. On long-lead
  equipment they fund production interest-free — and they reverse if the order
  is cancelled.
- **Capex is deferrable, and that is the liquidity buffer.** Unlike a utility,
  an industrial can cut investment sharply for a period without an immediate
  operating consequence. Say so when assessing whether the issuer can absorb a
  shock, and say what it costs later.

## block: sector/industrials/levels
kind: process
summary: Why a level means little here without a point in the cycle.
requires: core/identity
---
This library ships **no threshold table**, and in this sector a threshold
without a cycle position is close to meaningless anyway.

The same leverage figure describes a comfortable credit at the bottom of a cycle
and a stretched one at the top, because the denominator is about to move in
opposite directions in the two cases. A level quoted without saying which of the
two you believe you are looking at conveys almost nothing.

So:

- **If a house threshold table has been supplied**, use it, name it, say which
  version, and say whether it is calibrated on peak, trough or mid-cycle
  earnings. That last point decides whether it applies.
- **If none has been supplied**, state the level at which your view would change,
  say it is your own judgement, and say which point in the cycle you are
  assuming.

What is more informative than a level here:

- **Mid-cycle or trough earnings**, where the issuer's own history supports an
  estimate — leverage against a trough denominator is the test that matters.
- **The fixed-cost base**, which determines how far earnings fall for a given
  fall in revenue.
- **Committed liquidity against the working-capital swing** the issuer would
  need to fund on the way back up.
- And where a captive finance arm is present, none of the above means anything
  until the industrial figures have been separated.
