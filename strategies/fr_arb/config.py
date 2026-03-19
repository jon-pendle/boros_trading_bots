# FR Arbitrage Configuration — MarginalPnL Exit + Scale-In
# Aligned with backtest marginal_pnl_exit (Sharpe 5.17, $28.7k PnL)
import os

# Boros API
API_BASE_URL = "https://api.boros.finance/core"

# Strategy Parameters (from backtest optimal tuning)
ENTRY_SPREAD_THRESHOLD = float(os.environ.get("ENTRY_SPREAD_THRESHOLD", "0.042"))
EXIT_SPREAD_THRESHOLD = float(os.environ.get("EXIT_SPREAD_THRESHOLD", "0.038"))
MIN_HOLD_HOURS = 1.0
MAX_HOLD_HOURS = float('inf')    # No forced exit — exit only on spread convergence
MAX_CAPITAL = float(os.environ.get("MAX_CAPITAL", "10000"))  # global across all pairs

# Depth-weighted entry
MIN_DEPTH_USD = float(os.environ.get("MIN_DEPTH_USD", "500.0"))
DEPTH_UTILIZATION = float(os.environ.get("DEPTH_UTILIZATION", "0.3"))

# Scale-in
MAX_LAYERS = int(os.environ.get("MAX_LAYERS", "5"))
MIN_ADDON_INTERVAL_HOURS = float(os.environ.get("MIN_ADDON_INTERVAL_HOURS", "12"))
MIN_ADDON_TOKENS = float(os.environ.get("MIN_ADDON_TOKENS", "50"))

# Exit batching
EXIT_BATCH_MINUTES = int(os.environ.get("EXIT_BATCH_MINUTES", "15"))

# Margin pre-check (requires USER_ADDRESS for collateral API)
USER_ADDRESS = os.environ.get("USER_ADDRESS", "")

# Collateral filter: only trade pairs with these tokenIds (empty = all)
# 1=WBTC, 2=WETH, 3=USDT, 4=BNB, 5=HYPE
_allowed_raw = os.environ.get("ALLOWED_TOKEN_IDS", "")
ALLOWED_TOKEN_IDS = set(int(x) for x in _allowed_raw.split(",") if x.strip()) if _allowed_raw else set()

# Execution
TICK_INTERVAL_SECONDS = 60
DRY_RUN = True
STATE_FILE = "fr_arb_state.json"

# Dust threshold: positions below this size (tokens) are auto-closed
DUST_THRESHOLD_TOKENS = 0.01

# Capacity stepping
CAPACITY_STEP_TOKENS = 5.0
LIQUIDITY_FACTOR = float(os.environ.get("LIQUIDITY_FACTOR", "1.0"))
MIN_ENTRY_USD = float(os.environ.get("MIN_ENTRY_USD", "10.0"))

# Alerts
IFTTT_WEBHOOK_KEY = os.environ.get("IFTTT_WEBHOOK_KEY", "")
IFTTT_EVENT_NAME = os.environ.get("IFTTT_EVENT_NAME", "boros_event")
