# ADR 0003: Base track and application track

Status: Accepted (2026-07-12)

A tool enters the `.napp` track only when its id starts with `app-` and it has an independent dependency set, data migration/schema, or staged-rollout requirement. Modules and sheets stay on the base provision plus signed module hotfix track.

Both tracks are supported stable outcomes. Existing provision and `fleet_publish` behavior is not deprecated by this decision.

