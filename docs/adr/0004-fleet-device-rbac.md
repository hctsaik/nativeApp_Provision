# ADR 0004: Fleet and device RBAC boundary

Status: Accepted (2026-07-12)

Fleet build/promote/yank/rollout operations require the central Fleet administrator role. Device install/update/rollback/reconcile/GC require local administrator permission. General users may view and launch enabled installed applications. Central credentials are never exposed to an embedded iframe; the service owns authentication and audit.

Until production identity federation is configured, Fleet remains restricted to trusted internal networks. UI visibility is not authorization; every mutation endpoint must enforce its role.

