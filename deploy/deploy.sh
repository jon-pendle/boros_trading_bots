#!/bin/bash
# Deploy boros_trade_bot to EC2
# Usage: ./deploy/deploy.sh [test|prod|both]

set -e

TARGET=${1:-prod}
REMOTE="agent-1-ec2"
REMOTE_DIR="~/boros_trade_bot"

echo "=== Deploying boros_trade_bot ($TARGET) to $REMOTE ==="

# Sync code (excludes secrets and local state)
rsync -avz --delete \
    --exclude='.env*' \
    --exclude='*.json' \
    --exclude='logs/' \
    --exclude='__pycache__/' \
    --exclude='.git/' \
    --exclude='.claude/' \
    --exclude='tests/' \
    -e ssh \
    /root/boros_trade_bot/ \
    "$REMOTE:$REMOTE_DIR/"

# Ensure deploy script is executable
ssh "$REMOTE" "chmod +x $REMOTE_DIR/deploy/check_bot.sh"

# Build and start
case "$TARGET" in
    prod)
        ssh "$REMOTE" "cd $REMOTE_DIR && docker compose up -d --build prod"
        ;;
    test)
        ssh "$REMOTE" "cd $REMOTE_DIR && docker compose --profile test up -d --build test"
        ;;
    both)
        ssh "$REMOTE" "cd $REMOTE_DIR && docker compose --profile test up -d --build"
        ;;
esac

# Install cron monitor (idempotent)
ssh "$REMOTE" "
    CRON_CMD='*/5 * * * * $REMOTE_DIR/deploy/check_bot.sh'
    (crontab -l 2>/dev/null | grep -v check_bot.sh; echo \"\$CRON_CMD\") | crontab -
    echo 'Cron installed:'
    crontab -l | grep check_bot
"

echo "=== Deploy complete ==="
ssh "$REMOTE" "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
