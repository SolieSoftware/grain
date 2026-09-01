"""A conversational agent over a `Grain` facade.

The model's only channel into the engine is a validated `QuerySpec`. It never
sees SQL, the connection, or the DDL. See
`docs/plans/2026-08-28-query-agent-design.md`.
"""
