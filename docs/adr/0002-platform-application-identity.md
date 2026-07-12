# ADR 0002: Platform application identity

Status: Accepted (2026-07-12)

`plugin.yaml.id`, nativeApp `tool_id`, and `.napp app_id` are the same immutable identifier. App-track identifiers start with `app-`. Display names never participate in joins. Installed/active/LKG state comes from the device agent; catalog metadata and RBAC come from nativeApp; channel/latest comes from the Control Plane.

An agent-managed active source under `CIM_APPS_ROOT` takes precedence over the vendored source. Unsetting the variable restores the vendor path. The engine runner remains the only launch authority; `.napp.entrypoint` is an optional launch hint.

