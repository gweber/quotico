#!/usr/bin/env bash
set -euo pipefail

# Quotico.de deploy script
# Run on the server: ./deploy.sh

ROOT="/var/www/quotico.de"
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "=== Quotico.de Deploy ==="

# 1. Pull latest code
echo "[1/5] Pulling latest code..."
cd "$REPO"
git pull --ff-only

# 2. Build frontend
echo "[2/5] Building frontend..."
cd "$REPO/frontend"
pnpm install --frozen-lockfile
pnpm build

# 3. Deploy frontend files
echo "[3/5] Deploying frontend to $ROOT/web..."
rm -rf "$ROOT/web"
cp -r "$REPO/frontend/dist" "$ROOT/web"
chown -R www-data:www-data "$ROOT/web"

# 4. Update backend dependencies
echo "[4/5] Updating backend dependencies..."
cd "$REPO/backend"
.venv/bin/pip install -q -r requirements.txt

# 5. Restart services
echo "[5/5] Restarting services..."
sudo systemctl restart quotico
sudo systemctl reload nginx

echo "=== Deploy complete ==="
