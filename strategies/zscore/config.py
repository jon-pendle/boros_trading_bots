# Z-Score (MAD) Strategy Configuration
# Aligned with backtest alpha_decay scheme_B: lb192_ke1.0_kx0.5 (MAD + marginal exit)
# 50 rounds, mature pairs only
import os

# Z-Score Parameters
LOOKBACK = int(os.environ.get("ZS_LOOKBACK", "192"))        # z-score window in 15-min equivalent samples
SAMPLE_INTERVAL = int(os.environ.get("ZS_SAMPLE_INTERVAL", "15"))  # downsample interval (1-min ticks → 15-min equivalent)

# Exit-only mode: only allow exits, no new entries. Existing positions continue to exit normally.
EXIT_ONLY = os.environ.get("ZS_EXIT_ONLY", "false").lower() in ("true", "1", "yes")
K_ENTRY = float(os.environ.get("ZS_K_ENTRY", "1.0"))        # entry: |z| > 1.0 sigma
K_EXIT = float(os.environ.get("ZS_K_EXIT", "0.5"))          # exit: dir_z < 0.5 sigma
USE_MAD = os.environ.get("ZS_USE_MAD", "true").lower() in ("true", "1", "yes")

# Exit spread guard: absolute spread must be below this to exit
# Prevents exiting when spread is still wide (z reverted but spread hasn't)
ENTRY_SPREAD_THRESHOLD = float(os.environ.get("ZS_ENTRY_SPREAD_THRESHOLD", "0.042"))

# Capital
MAX_CAPITAL = float(os.environ.get("ZS_MAX_CAPITAL", "10000"))  # global across all pairs

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
# No force close — positions settle at maturity. Only exit on confirmed signal.

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
