---
title: "Local Kafka server for Hopskotch testing"
type: feat
date: 2026-03-13
---

# Local Kafka Server for Hopskotch Testing

## What We're Building

Add a local Kafka server to the Docker Compose and Kubernetes configurations for
testing the Hopskotch publisher without connecting to the production SCiMMA
Hopskotch service. The local server uses the `scimma/server` Docker image with
`--noSecurity` (no auth). It is disabled by default and enabled via a Docker
Compose profile (`local-kafka`) or Kubernetes replicas override.

## Why This Approach

- **Isolated testing**: Verify the notifier publishes correct payloads without
  requiring production Hopskotch credentials or network access.
- **Compose profiles**: Clean separation — `docker compose up` runs the normal
  stack; `docker compose --profile local-kafka up` adds the local Kafka server.
  No replicas hack needed.
- **Manual `.env` config**: When using the local Kafka, the user updates `.env`
  to point at it. Simple and explicit — no compose magic needed.

## Key Decisions

1. **Docker image**: `scimma/server:latest` with `--noSecurity` flag. This is
   the SCiMMA-provided dev/test image that bundles Kafka + Zookeeper. Not for
   production. Topics are created implicitly on first publish.

2. **Compose profile**: `local-kafka` profile. Disabled by default.
   Enable with `docker compose --profile local-kafka up`.

3. **Manual `.env` configuration**: When using local Kafka, the user updates
   `.env` to set:
   - `HOPSKOTCH_BROKER_URL=kafka://local-kafka:9092`
   - `HOPSKOTCH_TOPIC=crossmatch-test` (or any name — created on first publish)
   - `HOPSKOTCH_USERNAME=` (empty — triggers auth=False)
   - `HOPSKOTCH_PASSWORD=` (empty)

4. **Auth toggle in publisher code**: If `HOPSKOTCH_USERNAME` is empty, the
   publisher passes `auth=False` to `Stream()` instead of creating an `Auth`
   instance. No new env var needed — the existing empty defaults serve as the
   toggle.

5. **Kubernetes**: Add a local-kafka deployment with `replicas: 0` by default,
   overridable in values.yaml. User overrides `hopskotch.*` values to point at
   the local Kafka when enabling it, same as the Docker Compose `.env` approach.

6. **No consumer service**: Verify published messages via the hop CLI manually
   if needed. Expose port 9092 to the host so the CLI can reach local Kafka:
   `hop subscribe kafka://localhost:9092/topic --no-auth`.

7. **Networking**: The `scimma/server` wiki specifies `--hostname localhost`.
   In Docker Compose, the service name (`local-kafka`) serves as the DNS name
   on the internal network. The container's `hostname` should be set to match
   the service name so Kafka advertises the correct listener. If this doesn't
   work, use a network alias of `local-kafka` with `hostname: localhost` and
   test during implementation.

## scimma/server Details

- DockerHub: `scimma/server:latest`
- Source: https://github.com/scimma/scimma-server-container
- Wiki: https://github.com/scimma/hop-client/wiki
- `--noSecurity` flag disables all auth
- Takes ~20 seconds to start up
- Topics created implicitly on first publish
- Port 9092 for Kafka

## Open Questions

None — all questions resolved during brainstorming.
