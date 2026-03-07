# V-Shape Strategy Configuration
import os

# Boros API
API_BASE_URL = "https://api.boros.finance/core"

# Strategy Parameters
ENTRY_THRESHOLD = -0.05   # Oracle Funding < -5% triggers entry
EXIT_THRESHOLD = 0.00     # Oracle Funding > 0% triggers exit
MIN_HOLD_HOURS = 12       # Minimum hold duration before exit allowed
PROLONGED_HOLD_HOURS = 48 # Hours before emitting prolonged hold warning

# Execution Config
POSITION_SIZE_USD = 1000  # Notional size per trade (USD)
MAX_POSITIONS = 5         # Safety cap on concurrent positions
DRY_RUN = True            # Simulation mode by default

# Target Markets: None = use all markets from API dynamically
TARGET_MARKETS = None

# Depth Constraints
MIN_DEPTH_MULTIPLIER = 1.0   # Minimum depth required as multiple of position size
DEPTH_USAGE_PCT = 0.30       # Max fraction of available depth to consume

# State
STATE_FILE = "v_shape_state.json"

# Tick Interval
TICK_INTERVAL_SECONDS = 60

# Alert (IFTTT Webhooks)
IFTTT_WEBHOOK_KEY = os.environ.get("IFTTT_WEBHOOK_KEY", "")
IFTTT_EVENT_NAME = os.environ.get("IFTTT_EVENT_NAME", "boros_event")
