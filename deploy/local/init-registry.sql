CREATE TABLE IF NOT EXISTS applications (
    app_id VARCHAR(128) PRIMARY KEY,
    display_name VARCHAR(256),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS application_releases (
    app_id VARCHAR(128) NOT NULL REFERENCES applications(app_id),
    version VARCHAR(64) NOT NULL,
    object_key VARCHAR(1024) NOT NULL UNIQUE,
    sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    -- status value domain matches docs/REGISTRY_LOGICAL_SCHEMA.md §3.
    status VARCHAR(32) NOT NULL CHECK (status IN ('published', 'yanked')),
    dependency_fingerprint CHAR(64),
    platform_constraint VARCHAR(256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_id, version)
);

CREATE TABLE IF NOT EXISTS application_channels (
    app_id VARCHAR(128) NOT NULL,
    channel VARCHAR(32) NOT NULL,
    version VARCHAR(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_id, channel),
    FOREIGN KEY (app_id, version) REFERENCES application_releases(app_id, version)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor VARCHAR(256) NOT NULL,
    action VARCHAR(128) NOT NULL,
    target VARCHAR(512) NOT NULL,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

