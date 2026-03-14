---
title: "Auto-create Kafka topic on local-kafka startup"
type: feat
status: completed
date: 2026-03-14
origin: docs/brainstorms/2026-03-14-auto-create-kafka-topic-brainstorm.md
---

# Auto-Create Kafka Topic on Local Kafka Startup

## Overview

Add a `local-kafka-init` service to Docker Compose that automatically
creates the Kafka topic after the `local-kafka` container is healthy.
Eliminates the need to manually run `kafka-topics --create` after every
container restart.

## Proposed Solution

### 1. Add `local-kafka-init` service to `docker/docker-compose.yaml`

Add after the existing `local-kafka` service:

```yaml
# docker/docker-compose.yaml
  local-kafka-init:
    image: scimma/server:latest
    profiles:
      - local-kafka
    depends_on:
      local-kafka:
        condition: service_healthy
    networks:
      - internal
    entrypoint: /usr/bin/kafka-topics
    command:
      - --bootstrap-server
      - local-kafka:9092
      - --create
      - --if-not-exists
      - --topic
      - ${HOPSKOTCH_TOPIC:-crossmatch-test}
      - --partitions
      - "1"
      - --replication-factor
      - "1"
    restart: "no"
```

Key points (see brainstorm decisions #1-#4):
- Same `local-kafka` profile — only starts with `--profile local-kafka`
- `depends_on: condition: service_healthy` — waits for local-kafka health
  check to pass (~30s) before running
- `--if-not-exists` — idempotent, safe on restart
- `${HOPSKOTCH_TOPIC:-crossmatch-test}` — reads from `.env` with fallback
  default so it works even if user hasn't set `HOPSKOTCH_TOPIC`
- Same `scimma/server:latest` image — no extra pull
- `restart: "no"` — runs once and exits
- Uses `entrypoint` + `command` split to avoid shell quoting issues with
  the `kafka-topics` binary path

File: `docker/docker-compose.yaml` (after `local-kafka` service, ~line 263)

## Acceptance Criteria

- [x] `local-kafka-init` service added to `docker-compose.yaml`
- [x] Service uses `local-kafka` profile (only starts with `--profile local-kafka`)
- [x] Service waits for `local-kafka` healthy before running
- [x] Topic name reads from `${HOPSKOTCH_TOPIC:-crossmatch-test}`
- [x] `--if-not-exists` flag used for idempotency
- [ ] `docker compose --profile local-kafka up` creates the topic automatically
- [ ] Service exits cleanly after topic creation

## References

- **Origin brainstorm:** `docs/brainstorms/2026-03-14-auto-create-kafka-topic-brainstorm.md`
  - Key decisions: init service pattern, topic from `${HOPSKOTCH_TOPIC}` with
    default, `--if-not-exists`, same scimma/server image
- Docker Compose: `docker/docker-compose.yaml:245-263` (local-kafka service)
- `.env.example`: `docker/.env.example:49-56` (Hopskotch section)
