"""Central config — everything overridable via environment variables."""
import os

# LLM (OpenAI) — override with HEALER_MODEL if you have access to a newer model
MODEL = os.getenv("HEALER_MODEL", "gpt-5.1")

# Target app
TARGET_CONTAINER = os.getenv("TARGET_CONTAINER", "app-stable")
CANARY_CONTAINER = os.getenv("CANARY_CONTAINER", "app-canary")
HEALTH_URL = os.getenv("HEALTH_URL", "http://app-stable:8000/health")
CANARY_URL = os.getenv("CANARY_URL", "http://app-canary:8000")

# Source paths
# APP_SRC     = the app source as seen inside the healer container
# HOST_APP_SRC = the same directory's absolute path on the Docker host —
#                needed for -v bind mounts on ephemeral test containers,
#                because those mounts are resolved by the host daemon.
APP_SRC = os.getenv("APP_SRC", "/workspace/app")
HOST_APP_SRC = os.getenv("HOST_APP_SRC", APP_SRC)

# Docker / Traefik
APP_IMAGE = os.getenv("APP_IMAGE", "orderservice:stable")
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "healnet")
TRAEFIK_DYNAMIC = os.getenv("TRAEFIK_DYNAMIC", "/traefik-dynamic/dynamic.yml")

# Canary policy
CANARY_WEIGHT = int(os.getenv("CANARY_WEIGHT", "20"))
CANARY_WATCH_SEC = int(os.getenv("CANARY_WATCH_SEC", "45"))
CANARY_MAX_ERRORS = int(os.getenv("CANARY_MAX_ERRORS", "2"))

# Fixer policy
MAX_FIX_ATTEMPTS = int(os.getenv("MAX_FIX_ATTEMPTS", "4"))
MAX_REPRO_ATTEMPTS = int(os.getenv("MAX_REPRO_ATTEMPTS", "3"))

# Human gate: "off" = fully autonomous, "dashboard" = one-click approve in the
# healer UI, "telegram" = one-tap approve via bot (requires token + chat id)
APPROVAL_MODE = os.getenv("APPROVAL_MODE", "off").lower()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
APPROVAL_TIMEOUT_SEC = int(os.getenv("APPROVAL_TIMEOUT_SEC", "300"))
