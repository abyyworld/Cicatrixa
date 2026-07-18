#!/usr/bin/env bash
# Deploy the Cicatrixa platform to the server.
# Usage: SERVER=root@169.58.36.128 ./deploy.sh
set -euo pipefail
SERVER="${SERVER:-root@169.58.36.128}"
DEST=/root/cicatrixa-platform

rsync -az --delete --exclude .env "$(dirname "$0")/" "$SERVER:$DEST/"
ssh "$SERVER" "set -e; cd $DEST; \
  [ -f .env ] || cp .env.example .env; \
  docker compose build control; \
  docker compose up -d; \
  docker compose ps"
