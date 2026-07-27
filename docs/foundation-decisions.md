# Foundation Decisions

## Decided

- Employee source: Slack workspace sync.
- Manager source: admin CSV mapping.
- Employee interaction: FastEmbed intent routing, Slack slash commands, forms, and approval buttons.
- Leave/document rules: placeholder JSON policy until final company rules exist.
- Approval workflow: manager first, then HR only when policy requires it.
- Durable waiting: AgentSpan human-approval workflows.
- Documents/images: storage adapter now; S3 adapter later.

## Not Final Yet

- Exact leave allocations.
- Exact document rules.
- Whether weekends/public holidays count.
- Whether half-days are allowed.
- Whether negative balances are allowed per leave type.
- Production deployment provider.
