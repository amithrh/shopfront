"""Domain layer: availability, pricing, approval rules, idempotency and the commitment services.

Depends on `profile` (read-only views) and `mcp_tasks` (owner decisions drive tasks). Never imports
`tools` or `app`; those depend on it through the ports in `domain.ports`.
"""
