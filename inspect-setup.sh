#!/bin/bash
set -x
docker --version || { echo NO_DOCKER; exit 1; }
docker network create cinemora-net 2>/dev/null || true
docker rm -f cinemora-db cinemora-redis cinemora-app 2>/dev/null || true
docker run -d --name cinemora-db --network cinemora-net -e POSTGRES_DB=cinemora -e POSTGRES_USER=cinemora -e POSTGRES_PASSWORD=inspect_pw_2026 postgres:16
docker run -d --name cinemora-redis --network cinemora-net redis:7-alpine
for i in $(seq 1 30); do docker exec cinemora-db pg_isready -U cinemora -d cinemora && break; sleep 2; done
export DATABASE_URL=postgresql://cinemora:inspect_pw_2026@cinemora-db:5432/cinemora
docker run --rm --network cinemora-net -e DATABASE_URL ghcr.io/mythr00/cinemora:latest sh -c "cd /app && alembic upgrade head" || echo MIGRATION_FAILED
docker run -d --name cinemora-app --network cinemora-net -p 8000:8000 -e PORT=8000 -e DATABASE_URL -e REDIS_URL=redis://cinemora-redis:6379/0 -e RIGHTS_POLICY=allow_all -e CINEMORA_FOUNDER_EMAIL=mirthdraught@gmail.com -e CINEMORA_CORS_ORIGINS=https://cinemora-chi.vercel.app -e CINEMORA_COOKIE_SECURE=1 -e CINEMORA_PUBLIC_BASE_URL=https://cinemora-chi.vercel.app ghcr.io/mythr00/cinemora:latest
sleep 8
curl -s http://localhost:8000/health && echo HEALTH_OK || docker logs cinemora-app --tail 60
