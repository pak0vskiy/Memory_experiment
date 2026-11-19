#!/usr/bin/env bash
# One-shot setup on the VPS. Run from the repo checkout, as root:
#   sudo bash deploy/setup-vps.sh
# Safe to run again: it keeps an existing .env and data/, and only rebuilds/restarts the container.
#
# What it does
#   1. Shows what's running (Docker containers, ports, Tailscale serve/funnel).
#   2. Offers to remove the old "memory-service" side project (containers + its data volume).
#   3. Creates .env with a random ADMIN_KEY if there isn't one.
#   4. Builds and starts the app on 127.0.0.1:8090 and waits until it's healthy.
#   5. Publishes it with Tailscale Funnel (port 443 if free, else 8443).
#   6. Installs a nightly database backup (cron, keeps 30 days).
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
PORT=8090
YES="${YES:-}"          # YES=1 answers every question with "yes"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ask()  { [ -n "$YES" ] && return 0; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash deploy/setup-vps.sh"
command -v docker >/dev/null || die "Docker is not installed (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || die "the 'docker compose' plugin is missing (apt install docker-compose-plugin)"

# ---------------------------------------------------------------- 1. look around
say "What is running now"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' || true
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  if ! docker ps --format '{{.Names}}' | grep -qx memory-study; then
    ss -tlnp | grep ":$PORT " || true
    die "port $PORT is used by something else. Free it or change the port in docker-compose.yml."
  fi
fi
HAVE_TS=""
if command -v tailscale >/dev/null; then
  HAVE_TS=1
  echo; echo "Tailscale serve/funnel config:"; tailscale serve status 2>&1 || true
else
  echo "(tailscale is not installed: skipping Funnel. See README, section 'Deploy'.)"
fi

# ---------------------------------------------------------------- 2. old side project
say "Old side project (memory-service)"
OLD=$(docker ps -a --filter "label=com.docker.compose.service=memory-service" --format '{{.Names}}' || true)
if [ -n "$OLD" ]; then
  echo "Found containers: $OLD"
  PROJECTS=$(docker ps -a --filter "label=com.docker.compose.service=memory-service" \
             --format '{{.Label "com.docker.compose.project"}}' | sort -u)
  VOLS=""
  for p in $PROJECTS; do VOLS="$VOLS $(docker volume ls -q --filter "label=com.docker.compose.project=$p")"; done
  echo "Its Docker volumes (its data will be DELETED):${VOLS:- none}"
  if ask "Remove the old memory-service containers, volumes and image?"; then
    IMAGES=$(docker ps -a --filter "label=com.docker.compose.service=memory-service" --format '{{.Image}}' | sort -u)
    docker rm -f $OLD
    for v in $VOLS; do docker volume rm "$v" || true; done
    for p in $PROJECTS; do docker network rm "${p}_default" 2>/dev/null || true; done
    for i in $IMAGES; do docker rmi "$i" 2>/dev/null || true; done
    echo "Removed."
  else
    echo "Kept. (It doesn't conflict with this app unless it uses port $PORT.)"
  fi
else
  echo "No memory-service containers found. If the old project runs some other way (systemd, pm2, another"
  echo "container name), stop it by hand; this script only needs port $PORT to be free."
fi

# ---------------------------------------------------------------- 3. .env
say "Configuration (.env)"
if [ ! -f .env ]; then
  KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  sed "s/^ADMIN_KEY=.*/ADMIN_KEY=$KEY/" .env.example > .env
  chmod 600 .env
  echo "Created .env with a new random ADMIN_KEY."
else
  echo ".env exists, keeping it."
  grep -qE '^ADMIN_KEY=(change-me.*)?$' .env && echo "WARNING: ADMIN_KEY in .env is still a placeholder: the admin page stays disabled."
fi
grep -E '^(READ_SEC|DAY2_GAP_SEC|DAY9_GAP_SEC)=' .env | sed 's/^/  /'

# ---------------------------------------------------------------- 4. build + start
say "Build and start"
mkdir -p data
docker compose up -d --build
echo -n "Waiting for the app"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then echo " ok"; break; fi
  echo -n "."; sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || { docker compose logs --tail 50; die "app did not become healthy"; }
docker compose logs --tail 20 | grep -iE 'warn|error' || true

# ---------------------------------------------------------------- 5. Tailscale Funnel
if [ -n "$HAVE_TS" ]; then
  say "Public HTTPS address (Tailscale Funnel)"
  STATUS=$(tailscale serve status 2>&1 || true)
  if echo "$STATUS" | grep -q "127.0.0.1:$PORT"; then
    echo "Funnel already points at this app."
  elif echo "$STATUS" | grep -qiE 'no serve config|^$'; then
    tailscale funnel --bg "$PORT"
  else
    echo "Port 443 on Tailscale is already used by:"; echo "$STATUS"
    if ask "Is that the old side project? Reset the Tailscale serve/funnel config and use 443 for this app?"; then
      tailscale serve reset
      tailscale funnel --bg "$PORT"
    else
      echo "Keeping it; using port 8443 for this app instead."
      tailscale funnel --bg --https=8443 "$PORT"
    fi
  fi
  echo; tailscale funnel status || true
  echo "If the command printed a link to enable Funnel, open it once and run this script again."
  echo "New *.ts.net names can take ~10 minutes to work. Test from a phone with Wi-Fi off."
fi

# ---------------------------------------------------------------- 6. backups
say "Nightly backups"
CRON=/etc/cron.d/memory-study-backup
if [ ! -f "$CRON" ]; then
  if ask "Install a nightly backup at 04:17 to $APP_DIR/data/backups (keeps 30 days)?"; then
    echo "17 4 * * * root bash $APP_DIR/deploy/backup.sh >> /var/log/memory-study-backup.log 2>&1" > "$CRON"
    chmod 644 "$CRON"
    echo "Installed $CRON"
  fi
else
  echo "$CRON already installed."
fi
bash deploy/backup.sh

say "Done"
echo "Admin key (also in $APP_DIR/.env):"
grep '^ADMIN_KEY=' .env | cut -d= -f2-
echo "Participant page: the Funnel URL above. Admin page: same URL + /admin"
