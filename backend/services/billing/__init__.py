"""Phase 8 monetization package (founder directive 2026-11-25).

Modules:
- catalogue: founder-priced plan/package definitions, the
  platform credit rate, and the idempotent catalogue sync.
- credits: the wallet + immutable ledger (all balance mutations).
- periods: benefit periods, entitlements and allowances.
- generation_billing: reservation/settlement hooks for the run
  ledger choke points.
- lifecycle: subscription period rollover/expiry job.
- providers: payment-provider interface + the Paddle adapter.
- webhooks: verified webhook inbox processing.
- costs: the §13 production-cost engine over pricing rules.
- retention: the §21 generated-video registry + sweeper.
- notifications: the in-app notification store.
"""
