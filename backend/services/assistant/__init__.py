"""The AI Product Assistant (Phase 6).

A tool-using operator inside Cinemora, built to the design package
in ~/workspace/cinemora-audit/assistant/ (04 architecture, 06 tool
registry, 08 security model), reconciled with the Phase 8 accounts
system that landed after the design was written: the "owner" every
tool scopes by is the authenticated principal's user id, resolved
by api_v1.require_user (session or the dual-auth operator token,
which resolves to the founder principal).

The model is an untrusted proposer. It speaks a strict JSON action
protocol; the registry validates every action's schema, class and
ownership; READ tools execute immediately, WRITE and CONSEQUENTIAL
tools only ever create pending actions that a separate
authenticated confirm call executes.
"""
