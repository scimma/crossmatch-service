---
topic: Auto-create Kafka topic on local-kafka startup
date: 2026-03-14
status: complete
---

# Auto-Create Kafka Topic on Local Kafka Startup

## Context

The `local-kafka` service (scimma/server) loses all topics on container
restart because no volume is mounted for Kafka data. After every restart,
the `crossmatch-test` topic must be manually recreated with `kafka-topics
--create` before the Hopskotch publisher can send notifications. This is
tedious and easy to forget.

## What We're Building

A Docker Compose init service (`local-kafka-init`) that automatically
creates the Kafka topic after the `local-kafka` container is healthy. The
init service uses the same `scimma/server:latest` image (which includes the
`kafka-topics` CLI), runs the create command once, and exits.

## Why This Approach

### Chosen: Init service in Docker Compose

**Over entrypoint wrapper script:** An init service keeps the local-kafka
container unmodified — no custom entrypoint, no background process
management, no wrapper script to maintain. Docker Compose handles the
startup ordering via `depends_on: condition: service_healthy`.

**Over Kafka auto.create.topics.enable:** The scimma/server image doesn't
expose easy Kafka broker config overrides, and we already observed that
implicit topic creation on first publish doesn't work with this image.

## Key Decisions

1. **Init service pattern** — a separate one-shot service under the
   `local-kafka` Compose profile with `restart: "no"`. Runs after
   `local-kafka` health check passes, creates the topic, and exits.

2. **Topic name from `${HOPSKOTCH_TOPIC}`** — reads the topic name from
   the same `.env` variable the celery-worker uses, so they stay in sync.
   One place to configure the topic name. The Compose definition should
   provide a default (e.g., `${HOPSKOTCH_TOPIC:-crossmatch-test}`) so the
   init service works even if the user hasn't uncommented the local Kafka
   values in `.env`.

3. **`--if-not-exists` flag** — makes the create command idempotent. Safe
   to run on restart even if the topic somehow already exists.

4. **Same image as local-kafka** — `scimma/server:latest` already contains
   `/usr/bin/kafka-topics`. No extra image to pull.

## Open Questions

None — all decisions resolved during brainstorming.
