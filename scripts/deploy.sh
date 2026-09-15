#!/usr/bin/env bash
# Ship VECTRA_QUANT to the EC2 host and bring it up.
#   ./scripts/deploy.sh <elastic-ip>
#
# Builds the PWA locally (so the box needs no Node), rsyncs the repo minus
# secrets-adjacent junk, copies .env separately, then docker compose up.
set -euo pipefail

IP="${1:-}"
[ -z "$IP" ] && { echo "usage: $0 <elastic-ip> [--no-start]"; exit 1; }
NO_START=0
[ "${2:-}" = "--no-start" ] && NO_START=1

KEY_FILE="${KEY_FILE:-$HOME/.ssh/vectra_quant-key.pem}"
REMOTE="ec2-user@${IP}"
DIR=/opt/vectra_quant
SITE="${IP//./-}.sslip.io"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ssh_() { ssh -i "$KEY_FILE" -o StrictHostKeyChecking=accept-new "$REMOTE" "$@"; }

[ -f "$HERE/.env" ] || { echo "no .env at $HERE/.env — refusing to deploy without secrets"; exit 1; }

say "building PWA locally"
(cd "$HERE/pwa" && npm run build >/dev/null)

say "waiting for docker on the host"
for i in $(seq 1 40); do
  ssh_ 'command -v docker >/dev/null && docker info >/dev/null 2>&1' && break
  sleep 10
  [ "$i" = 40 ] && { echo "docker never came up — check cloud-init"; exit 1; }
done

say "syncing to $REMOTE:$DIR"
ssh_ "mkdir -p $DIR"
rsync -az --delete -e "ssh -i $KEY_FILE -o StrictHostKeyChecking=accept-new" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.ruff_cache' --exclude 'node_modules' \
  --exclude '/data' --exclude '.git' --exclude '.env' \
  "$HERE/" "$REMOTE:$DIR/"

say "copying .env (0600)"
scp -q -i "$KEY_FILE" "$HERE/.env" "$REMOTE:$DIR/.env"
ssh_ "chmod 600 $DIR/.env"

say "pinning SITE_ADDRESS=$SITE"
ssh_ "grep -q '^SITE_ADDRESS=' $DIR/.env \
      && sed -i 's|^SITE_ADDRESS=.*|SITE_ADDRESS=$SITE|' $DIR/.env \
      || echo 'SITE_ADDRESS=$SITE' >> $DIR/.env"
ssh_ "grep -q '^PWA_ORIGIN=' $DIR/.env \
      && sed -i 's|^PWA_ORIGIN=.*|PWA_ORIGIN=https://$SITE|' $DIR/.env \
      || echo 'PWA_ORIGIN=https://$SITE' >> $DIR/.env"

say "building and starting containers"
# The image runs as uid 10001; the bind-mounted data dir must be writable by it
# or SQLite fails with "unable to open database file".
ssh_ "cd $DIR && mkdir -p data && sudo chown -R 10001:10001 data"
if [ "$NO_START" = "1" ]; then
  say "building image only — backend will NOT be started"
  # --no-deps is essential: caddy depends_on backend, so a plain "up caddy"
  # starts the backend too, which is exactly what --no-start must prevent.
  # Was the backend already running? --no-start means "do not start it", NOT "stop it".
  # Stopping unconditionally killed a backend the trader had started themselves.
  WAS_UP=0
  if ssh_ "cd $DIR && docker compose ps --status running --services" 2>/dev/null \
      | grep -qx backend; then
    WAS_UP=1
  fi
  ssh_ "cd $DIR && docker compose build backend && docker compose up -d --no-deps caddy"
  if [ "$WAS_UP" = "0" ]; then
    ssh_ "cd $DIR && docker compose stop backend 2>/dev/null || true"
  fi
  ssh_ "cd $DIR && docker compose ps"
  echo
  if [ "$WAS_UP" = "1" ]; then
    echo "  Backend was ALREADY RUNNING and was left alone — it is on the PREVIOUS image."
    echo "  Restart it when you want this build live:"
  else
    echo "  Code and image are current. Start it deliberately with:"
  fi
  echo "    ssh -i $KEY_FILE $REMOTE 'cd $DIR && docker compose up -d backend'"
  exit 0
fi
ssh_ "cd $DIR && docker compose up -d --build"

say "waiting for health"
for i in $(seq 1 30); do
  if ssh_ "curl -fsS http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    say "backend healthy"
    break
  fi
  sleep 5
  [ "$i" = 30 ] && { ssh_ "cd $DIR && docker compose logs --tail=60 backend"; exit 1; }
done

ssh_ "cd $DIR && docker compose ps"

cat <<EOF

================================================================
  Deployed
================================================================
  Console : https://$SITE
  Health  : https://$SITE/health
  Logs    : ssh -i $KEY_FILE $REMOTE 'cd $DIR && docker compose logs -f backend'
  Restart : ssh -i $KEY_FILE $REMOTE 'cd $DIR && docker compose restart backend'

  On the phone: open the console in Safari -> Share -> Add to Home Screen,
  then System -> Enable push. iOS will not deliver push until it is installed.
================================================================
EOF
