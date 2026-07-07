# Foundation Decisions

## Decided

- Employee source: Slack workspace sync.
- Manager source: admin CSV mapping.
- Employee interaction: DM-first free-flow messages, optional `/leave`.
- Leave/document rules: placeholder JSON policy until final company rules exist.
- Approval workflow: manager first, then HR only when policy requires it.
- Durable waiting: Agentspan adapter later; local workflow adapter now.
- Documents/images: storage adapter now; S3 adapter later.

## Not Final Yet

- Exact leave allocations.
- Exact document rules.
- Whether weekends/public holidays count.
- Whether half-days are allowed.
- Whether negative balances are allowed per leave type.
- Production deployment provider.

