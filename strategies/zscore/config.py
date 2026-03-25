# Z-Score Strategy Configuration
# Aligned with backtest zscore_iter iter_1 optimal: lb480_ke1.0_kx0.5 (MAD)
# Sharpe 4.62, $6415 PnL @ $10K cap, 67 rounds, 78% win rate
import os

# Z-Score Parameters
LOOKBACK = int(os.environ.get("ZS_LOOKBACK", "480"))        # ~5 days at 15min ticks
K_ENTRY = float(os.environ.get("ZS_K_ENTRY", "1.0"))        # entry: |z| > 1.0 sigma
K_EXIT = float(os.environ.get("ZS_K_EXIT", "0.5"))          # exit: dir_z < 0.5 sigma
USE_MAD = os.environ.get("ZS_USE_MAD", "true").lower() in ("true", "1", "yes")

# Capital
MAX_CAPITAL = float(os.environ.get("ZS_MAX_CAPITAL", "20000"))  # global across all pairs

# Depth-weighted entry
MIN_DEPTH_USD = float(os.environ.get("ZS_MIN_DEPTH_USD", "500.0"))
DEPTH_UTILIZATION = float(os.environ.get("ZS_DEPTH_UTILIZATION", "0.3"))

# Scale-in
MAX_LAYERS = int(os.environ.get("ZS_MAX_LAYERS", "5"))
MIN_ADDON_INTERVAL_HOURS = float(os.environ.get("ZS_MIN_ADDON_INTERVAL_HOURS", "12"))
MIN_ADDON_TOKENS = float(os.environ.get("ZS_MIN_ADDON_TOKENS", "50"))

# Exit batching
EXIT_BATCH_MINUTES = int(os.environ.get("ZS_EXIT_BATCH_MINUTES", "15"))

# Hold time
MIN_HOLD_HOURS = 1.0
MAX_HOLD_HOURS = float('inf')
Z_NONE_EXIT_HOURS = float(os.environ.get("ZS_Z_NONE_EXIT_HOURS", "48"))  # force exit if z=None for this long

# Execution
CAPACITY_STEP_TOKENS = 5.0
LIQUIDITY_FACTOR = float(os.environ.get("ZS_LIQUIDITY_FACTOR", "1.0"))
MIN_ENTRY_USD = float(os.environ.get("ZS_MIN_ENTRY_USD", "10.0"))

# Dust
DUST_THRESHOLD_TOKENS = 0.01

# Shared config (inherited from global)
USER_ADDRESS = os.environ.get("USER_ADDRESS", "")
_allowed_raw = os.environ.get("ALLOWED_TOKEN_IDS", "")
ALLOWED_TOKEN_IDS = set(int(x) for x in _allowed_raw.split(",") if x.strip()) if _allowed_raw else set()
