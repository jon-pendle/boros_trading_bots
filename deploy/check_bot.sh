#!/bin/bash
# Bot health monitor — runs via cron every 5 minutes
# Checks heartbeat freshness and container status
# Sends IFTTT alert if bot is down, auto-restarts container

BOT_DIR="$HOME/boros_trade_bot"
CONTAINER="boros-prod"
PAUSE_FILE="$BOT_DIR/.pause_monitor"
MAX_AGE=300  # 5 minutes

# Maintenance mode: skip all checks when pause file exists
if [ -f "$PAUSE_FILE" ]; then
    exit 0
fi

HEARTBEAT=$(docker exec $CONTAINER cat /app/logs/heartbeat.json 2>/dev/null)

# Load IFTTT key from prod env
IFTTT_KEY=$(grep IFTTT_WEBHOOK_KEY "$BOT_DIR/.env.prod" 2>/dev/null | cut -d= -f2)
IFTTT_EVENT="boros_alert"

send_alert() {
    local msg="$1"
    if [ -n "$IFTTT_KEY" ]; then
        curl -s -o /dev/null -X POST \
            "https://maker.ifttt.com/trigger/$IFTTT_EVENT/with/key/$IFTTT_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"value1\": \"$msg\"}"
    fi
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') ALERT: $msg" >> "$BOT_DIR/monitor.log"
}

# Check 1: Is container running?
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    send_alert "Container $CONTAINER not running. Restarting..."
    cd "$BOT_DIR" && docker compose up -d prod
    exit 1
fi

# Check 2: Is heartbeat fresh?
if [ -z "$HEARTBEAT" ]; then
    send_alert "No heartbeat from $CONTAINER"
    exit 1
fi

LAST_TICK=$(echo "$HEARTBEAT" | python3 -c "
import sys, json
from datetime import datetime, timezone
data = json.load(sys.stdin)
dt = datetime.fromisoformat(data['last_tick'])
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
age = (datetime.now(timezone.utc) - dt).total_seconds()
print(int(age))
" 2>/dev/null)

if [ -z "$LAST_TICK" ]; then
    send_alert "Cannot parse heartbeat from $CONTAINER"
    exit 1
fi

if [ "$LAST_TICK" -gt "$MAX_AGE" ]; then
    send_alert "Bot stale: last tick ${LAST_TICK}s ago. Restarting..."
    cd "$BOT_DIR" && docker compose restart prod
    exit 1
fi

# All good — silent
exit 0
