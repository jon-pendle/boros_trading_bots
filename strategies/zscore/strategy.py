"""
Z-Score Strategy — MAD robust z-score entry/exit with scale-in.

Entry: |z| > k_entry (z computed from rolling MAD of mid-rate spread)
Exit: directional z < k_exit (spread mean-reverted enough)
Scale-in: adds layers when z stays extreme and interval met.

State is pair-level: each pair (e.g. "ETH_74_75") has ONE position record
containing both legs. This correctly handles shared markets (market 74 can
appear in ETH_74_75 and ETH_74_76 independently).
"""
import json
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from strategies.framework.base_strategy import BaseStrategy
from strategies.framework.interfaces import IContext
from strategies.framework.pricing import PricingEngine
import strategies.zscore.config as config

logger = logging.getLogger(__name__)

OB_WORKERS = 8
HISTORY_FILE = os.environ.get("ZS_HISTORY_FILE", "logs/zscore_spread_history.json")
POSITIONS_FILE = os.environ.get("ZS_POSITIONS_FILE", "logs/zscore_positions.json")
HISTORY_SAVE_INTERVAL = 60  # save every N ticks


def _calc_vwap(liq, tokens):
    """Walk OB levels, return VWAP rate for filling `tokens`. None if insufficient."""
    remaining = tokens
    total = 0.0
    for px, sz in liq:
        if sz <= 1e-9:
            continue
        take = min(remaining, sz)
        total += take * px
        remaining -= take
        if remaining <= 1e-6:
            return total / tokens
    return None



def _marginal_exit_tokens(exit_liq_A, exit_liq_B, side_A,
                          total_tokens, exit_threshold):
    """Marginal PnL scan: max tokens where marginal execution spread < threshold.
    Each slice checks the incremental VWAP spread stays below the exit threshold."""
    max_closeable = 0.0
    prev_cum_A = 0.0
    prev_cum_B = 0.0
    step = max(1.0, total_tokens / 100.0)
    test = 0.0

    while test < total_tokens:
        test = min(test + step, total_tokens)

        vwap_A = _calc_vwap(exit_liq_A, test)
        vwap_B = _calc_vwap(exit_liq_B, test)
        if vwap_A is None or vwap_B is None:
            break

        cum_A = vwap_A * test
        cum_B = vwap_B * test

        slice_tokens = test - max_closeable
        if slice_tokens < 0.01:
            break
        slice_apr_A = (cum_A - prev_cum_A) / slice_tokens
        slice_apr_B = (cum_B - prev_cum_B) / slice_tokens

        if side_A == 1:
            marginal_spread = slice_apr_A - slice_apr_B
        else:
            marginal_spread = slice_apr_B - slice_apr_A

        if marginal_spread >= exit_threshold:
            break

        max_closeable = test
        prev_cum_A = cum_A
        prev_cum_B = cum_B

    return max_closeable


class ZScoreStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="zscore", target_markets=[])
        self._ob_cache = {}
        self._collateral_cache = {}
        self._im_params_cache = {}
        self._onchain_positions = {}
        self._pairs = {}
        self._tick_count = 0
        # Z-score state: persists across ticks and restarts
        self._spread_history: dict[str, deque] = {}
        self._load_spread_history()
        self._positions_dirty = False  # track if positions changed this tick
        self._orphan_last_warn: dict[int, float] = {}  # market_id -> last warning timestamp

    # ------------------------------------------------------------------
    # Spread history persistence
    # ------------------------------------------------------------------

    def _load_spread_history(self):
        """Load spread history from disk on startup."""
        try:
            path = Path(HISTORY_FILE)
            if not path.exists():
                logger.info("No spread history file, starting fresh warmup")
                return
            with open(path) as f:
                data = json.load(f)
            count = 0
            for pair_name, values in data.items():
                self._spread_history[pair_name] = deque(values, maxlen=config.LOOKBACK * config.SAMPLE_INTERVAL)
                count += len(values)
            logger.info("Loaded spread history: %d pairs, %d total observations",
                        len(data), count)
        except Exception as e:
            logger.warning("Failed to load spread history: %s", e)

    def _save_spread_history(self):
        """Save spread history to disk."""
        try:
            path = Path(HISTORY_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: list(v) for k, v in self._spread_history.items()}
            tmp = str(path) + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, str(path))
        except Exception as e:
            logger.warning("Failed to save spread history: %s", e)

    # ------------------------------------------------------------------
    # Pair position persistence
    # ------------------------------------------------------------------

    def _load_pair_positions(self, context: IContext) -> bool:
        """Load pair positions from disk into state manager on startup.
        Returns True if loaded successfully (or no file), False on error."""
        try:
            path = Path(POSITIONS_FILE)
            if not path.exists():
                return True
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.error("Positions file corrupt (not a dict), ignoring")
                return False
            for pair_name, pos in data.items():
                if not isinstance(pos, dict) or 'mkt_A' not in pos:
                    logger.warning("Skipping invalid pair record: %s", pair_name)
                    continue
                context.state.set_position(self.name, pair_name, pos)
            logger.info("Loaded %d pair positions from disk", len(data))
            return True
        except Exception as e:
            logger.error("Failed to load pair positions: %s — "
                         "orphan detection DISABLED this tick to prevent unwanted closes", e)
            return False

    def _save_pair_positions(self, context: IContext):
        """Save all pair positions to disk (atomic write)."""
        try:
            pairs = self._get_all_pair_positions(context)
            path = Path(POSITIONS_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(pairs, f, indent=2, default=str)
            os.replace(tmp, str(path))
        except Exception as e:
            logger.warning("Failed to save pair positions: %s", e)

    # ------------------------------------------------------------------
    # Z-Score computation
    # ------------------------------------------------------------------

    def _get_zscore(self, pair_name: str, spread: float):
        """Compute MAD-based robust z-score for the pair's spread.
        History stores every tick (1-min resolution). Z-score is computed
        on downsampled history (every SAMPLE_INTERVAL-th value) to match
        backtest 15-min granularity. Current spread is evaluated against
        the downsampled distribution."""
        maxlen = config.LOOKBACK * config.SAMPLE_INTERVAL
        if pair_name not in self._spread_history:
            self._spread_history[pair_name] = deque(maxlen=maxlen)
        self._spread_history[pair_name].append(spread)

        # Downsample: take every SAMPLE_INTERVAL-th value from full history
        full = self._spread_history[pair_name]
        interval = config.SAMPLE_INTERVAL
        sampled = [full[i] for i in range(len(full) - 1, -1, -interval)]
        # sampled is newest-first; reverse for chronological order
        sampled.reverse()

        min_obs = max(config.LOOKBACK // 2, 20)
        if len(sampled) < min_obs:
            return None

        arr = np.array(sampled)

        if config.USE_MAD:
            median = np.median(arr)
            mad = np.median(np.abs(arr - median)) * 1.4826
            if mad < 1e-8:
                return None
            center = median
            scale = mad
        else:
            center = arr.mean()
            scale = arr.std()
            if scale < 1e-8:
                return None

        return (spread - center) / scale

    # ------------------------------------------------------------------
    # Pair-level state helpers
    # ------------------------------------------------------------------

    def _get_pair_pos(self, context: IContext, pair_name: str):
        """Get pair-level position (keyed by pair_name string)."""
        return context.state.get_position(self.name, pair_name)

    def _set_pair_pos(self, context: IContext, pair_name: str, data: dict):
        context.state.set_position(self.name, pair_name, data)
        self._positions_dirty = True

    def _clear_pair_pos(self, context: IContext, pair_name: str):
        context.state.clear_position(self.name, pair_name)
        self._positions_dirty = True

    def _get_all_pair_positions(self, context: IContext) -> dict:
        """Get all pair positions. Filter to only pair-level records (string keys with mkt_A)."""
        all_pos = context.state.get_all_positions(self.name)
        return {k: v for k, v in all_pos.items()
                if isinstance(k, str) and 'mkt_A' in v}

    def _market_claimed_by_pair(self, context: IContext, market_id: int) -> bool:
        """Check if any existing pair position references this market."""
        for _, pos in self._get_all_pair_positions(context).items():
            if pos.get('mkt_A') == market_id or pos.get('mkt_B') == market_id:
                return True
        return False

    def _market_occupied(self, context: IContext, market_id: int,
                          exclude_pair: str = None) -> bool:
        """Return True if any existing pair holds this market.
        Matches backtest behavior: one market can only belong to one pair at a time."""
        for pname, pos in self._get_all_pair_positions(context).items():
            if pname == exclude_pair:
                continue
            if pos.get('mkt_A') == market_id or pos.get('mkt_B') == market_id:
                return True
        return False

    # ------------------------------------------------------------------
    # Orderbook helpers
    # ------------------------------------------------------------------

    def _prefetch_orderbooks(self, context: IContext):
        market_ids = list(self.target_markets)

        def fetch(mid):
            return mid, context.data.get_orderbook(mid)

        with ThreadPoolExecutor(max_workers=OB_WORKERS) as pool:
            futures = {pool.submit(fetch, mid): mid for mid in market_ids}
            for future in as_completed(futures):
                try:
                    mid, book = future.result()
                    self._ob_cache[mid] = book
                except Exception as e:
                    mid = futures[future]
                    logger.warning("OB fetch failed for %d: %s", mid, e)
                    self._ob_cache[mid] = {"bids": [], "asks": []}

    def _get_orderbook(self, context: IContext, market_id: int) -> dict:
        if market_id not in self._ob_cache:
            self._ob_cache[market_id] = context.data.get_orderbook(market_id)
        return self._ob_cache[market_id]

    def _get_best_prices(self, context: IContext, market_id: int):
        book = self._get_orderbook(context, market_id)
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        if not bids or not asks:
            return None, None
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        if abs(best_bid) > 1.0 or abs(best_ask) > 1.0:
            return None, None
        return best_bid, best_ask

    # ------------------------------------------------------------------
    # Margin helpers
    # ------------------------------------------------------------------

    def _get_im_params(self, context: IContext, market_id: int) -> dict:
        if market_id in self._im_params_cache:
            return self._im_params_cache[market_id]
        info = context.data.get_market_info(market_id)
        im_data = info.get('imData', {})
        cfg = info.get('config', {})
        k_im_raw = cfg.get('kIM', '0')
        try:
            k_im = int(k_im_raw) / 1e18
        except (ValueError, TypeError):
            k_im = float(k_im_raw) if k_im_raw else 0.0
        params = {
            'k_im': k_im,
            't_thresh': float(cfg.get('tThresh', 0)),
            'i_tick_thresh': int(im_data.get('iTickThresh', 0)),
            'tick_step': int(im_data.get('tickStep', 1)),
            'margin_floor': float(im_data.get('marginFloor', 0)),
            'token_id': int(info.get('tokenId', 0)),
        }
        self._im_params_cache[market_id] = params
        return params

    def _check_margin(self, context: IContext, mkt_A: int, mkt_B: int,
                      rate_A: float, rate_B: float,
                      spot_price: float) -> tuple:
        if not self._collateral_cache:
            return True, float('inf')
        now_ts = time.time()
        params_A = self._get_im_params(context, mkt_A)
        params_B = self._get_im_params(context, mkt_B)
        info_A = context.data.get_market_info(mkt_A)
        info_B = context.data.get_market_info(mkt_B)
        maturity_A = int(info_A.get('imData', {}).get('maturity', 0))
        maturity_B = int(info_B.get('imData', {}).get('maturity', 0))
        ttm_A = max(0, maturity_A - now_ts)
        ttm_B = max(0, maturity_B - now_ts)

        im_A = PricingEngine.calculate_im_per_token(
            rate=rate_A, k_im=params_A['k_im'],
            t_thresh_seconds=params_A['t_thresh'],
            i_tick_thresh=params_A['i_tick_thresh'],
            tick_step=params_A['tick_step'],
            margin_floor=params_A['margin_floor'],
            time_to_maturity_seconds=ttm_A,
        )
        im_B = PricingEngine.calculate_im_per_token(
            rate=rate_B, k_im=params_B['k_im'],
            t_thresh_seconds=params_B['t_thresh'],
            i_tick_thresh=params_B['i_tick_thresh'],
            tick_step=params_B['tick_step'],
            margin_floor=params_B['margin_floor'],
            time_to_maturity_seconds=ttm_B,
        )

        tid_A = params_A['token_id']
        tid_B = params_B['token_id']
        avail_A = self._collateral_cache.get(tid_A, 0)
        avail_B = self._collateral_cache.get(tid_B, 0)

        if im_A <= 0 or im_B <= 0:
            return True, float('inf')

        if tid_A == tid_B:
            combined = im_A + im_B
            max_tokens = avail_A / combined if combined > 0 else float('inf')
        else:
            max_tokens = min(avail_A / im_A, avail_B / im_B)

        return max_tokens >= 1.0, max_tokens

    def _deduct_im_from_cache(self, context: IContext, mkt_A: int, mkt_B: int,
                              rate_A: float, rate_B: float, tokens: float):
        if not self._collateral_cache:
            return
        now_ts = time.time()
        params_A = self._get_im_params(context, mkt_A)
        params_B = self._get_im_params(context, mkt_B)
        info_A = context.data.get_market_info(mkt_A)
        info_B = context.data.get_market_info(mkt_B)
        ttm_A = max(0, int(info_A.get('imData', {}).get('maturity', 0)) - now_ts)
        ttm_B = max(0, int(info_B.get('imData', {}).get('maturity', 0)) - now_ts)

        im_A = PricingEngine.calculate_im_per_token(
            rate=rate_A, k_im=params_A['k_im'],
            t_thresh_seconds=params_A['t_thresh'],
            i_tick_thresh=params_A['i_tick_thresh'],
            tick_step=params_A['tick_step'],
            margin_floor=params_A['margin_floor'],
            time_to_maturity_seconds=ttm_A,
        ) * tokens
        im_B = PricingEngine.calculate_im_per_token(
            rate=rate_B, k_im=params_B['k_im'],
            t_thresh_seconds=params_B['t_thresh'],
            i_tick_thresh=params_B['i_tick_thresh'],
            tick_step=params_B['tick_step'],
            margin_floor=params_B['margin_floor'],
            time_to_maturity_seconds=ttm_B,
        ) * tokens

        tid_A = params_A['token_id']
        tid_B = params_B['token_id']
        if tid_A in self._collateral_cache:
            self._collateral_cache[tid_A] = max(0, self._collateral_cache[tid_A] - im_A)
        if tid_B in self._collateral_cache:
            self._collateral_cache[tid_B] = max(0, self._collateral_cache[tid_B] - im_B)
        logger.info("  IM deducted: tid=%d -%.4f, tid=%d -%.4f", tid_A, im_A, tid_B, im_B)

    # ------------------------------------------------------------------
    # Capital tracking
    # ------------------------------------------------------------------

    def _get_total_allocated(self, context: IContext) -> float:
        """Sum position_size across all pair positions (global capital)."""
        total = 0.0
        for _, pos in self._get_all_pair_positions(context).items():
            total += pos.get('position_size_A', 0) + pos.get('position_size_B', 0)
        return total

    # ------------------------------------------------------------------
    # VWAP capacity scan
    # ------------------------------------------------------------------

    def _run_vwap_capacity_scan(self, context, mkt_A, mkt_B, best_scenario,
                                bid_A, ask_A, bid_B, ask_B,
                                spot_price, effective_capital) -> float:
        book_A = self._get_orderbook(context, mkt_A)
        book_B = self._get_orderbook(context, mkt_B)

        if best_scenario == 1:
            liquidity_A = book_A.get('bids', [])
            liquidity_B = book_B.get('asks', [])
        else:
            liquidity_A = book_A.get('asks', [])
            liquidity_B = book_B.get('bids', [])

        info_cap = context.data.get_market_info(mkt_A)
        maturity_ts = int(info_cap.get('imData', {}).get('maturity', 0))
        now_ts = time.time()
        time_remaining_years = max(0.0, (maturity_ts - now_ts) / 31_536_000)

        max_yield_price = max(bid_A, ask_B) if best_scenario == 1 else max(bid_B, ask_A)
        if max_yield_price <= 0:
            max_yield_price = 0.01
        token_position_value = abs(max_yield_price) * time_remaining_years * spot_price
        if token_position_value <= 0:
            token_position_value = 0.01

        max_allowed_tokens = effective_capital / token_position_value

        step = config.CAPACITY_STEP_TOKENS
        test_tokens = 0.0
        max_safe_tokens = 0.0

        while True:
            next_test = min(test_tokens + step, max_allowed_tokens)
            if next_test <= test_tokens:
                break
            test_tokens = next_test

            vwap_A = _calc_vwap(liquidity_A, test_tokens)
            if vwap_A is None:
                break
            vwap_B = _calc_vwap(liquidity_B, test_tokens)
            if vwap_B is None:
                break

            # VWAP spread must exceed entry threshold (matches backtest)
            test_spread = (vwap_A - vwap_B if best_scenario == 1
                           else vwap_B - vwap_A)
            if test_spread < config.ENTRY_SPREAD_THRESHOLD:
                break

            max_safe_tokens = test_tokens
            if test_tokens >= max_allowed_tokens:
                break

        return max_safe_tokens * config.LIQUIDITY_FACTOR

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def on_tick(self, context: IContext) -> list:
        self._ob_cache.clear()
        self._collateral_cache.clear()
        self._im_params_cache.clear()
        self._onchain_positions.clear()
        self._collateral_fetch_ok = False
        self._positions_dirty = False
        self._tick_count += 1
        events = []

        # Load persisted pair positions on first tick
        if self._tick_count == 1:
            self._positions_load_ok = self._load_pair_positions(context)
        if not hasattr(self, '_positions_load_ok'):
            self._positions_load_ok = True

        context.data.get_all_market_ids()

        self._pairs = context.data.generate_pairs(
            allowed_token_ids=config.ALLOWED_TOKEN_IDS or None)
        targets = set()
        for mkt_a, mkt_b in self._pairs.values():
            targets.add(mkt_a)
            targets.add(mkt_b)
        self.target_markets = list(targets)

        self._prefetch_orderbooks(context)

        if config.USER_ADDRESS and hasattr(context.data, 'get_collateral_detail'):
            try:
                detail = context.data.get_collateral_detail(config.USER_ADDRESS)
                for tid, info in detail.items():
                    self._collateral_cache[tid] = info['available']
                    for pos in info.get('positions', []):
                        self._onchain_positions[pos['market_id']] = pos
                self._collateral_fetch_ok = True
            except Exception as e:
                logger.warning("Collateral fetch failed: %s", e)

        self._sync_state_from_onchain(context)

        # ================================================================
        # Phase 0: compute z-scores for all pairs
        # ================================================================
        pair_data = {}
        for pair_name, (mkt_A, mkt_B) in self._pairs.items():
            bid_A, ask_A = self._get_best_prices(context, mkt_A)
            bid_B, ask_B = self._get_best_prices(context, mkt_B)
            if None in [bid_A, ask_A, bid_B, ask_B]:
                continue

            mid_A = (bid_A + ask_A) / 2.0
            mid_B = (bid_B + ask_B) / 2.0
            spread = mid_A - mid_B
            z = self._get_zscore(pair_name, spread)

            pair_data[pair_name] = {
                'bid_A': bid_A, 'ask_A': ask_A,
                'bid_B': bid_B, 'ask_B': ask_B,
                'spread': spread, 'z': z,
            }

            events.append({
                "type": "scan",
                "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "bid_a": bid_A, "ask_a": ask_A,
                "bid_b": bid_B, "ask_b": ask_B,
                "has_position": self._get_pair_pos(context, pair_name) is not None,
                "z_score": round(z, 3) if z is not None else None,
            })

        # ================================================================
        # Phase 1: EXIT — directional z-score mean reversion
        # ================================================================
        for pair_name, (mkt_A, mkt_B) in self._pairs.items():
            pair_pos = self._get_pair_pos(context, pair_name)
            if not pair_pos:
                continue

            pd_ = pair_data.get(pair_name)
            if not pd_:
                continue

            exit_events = self._check_exit(context, pair_name, pair_pos, pd_)
            events.extend(exit_events)

        # Orphan detection: on-chain markets not claimed by any pair
        # SKIP if position load failed — prevents unwanted closes
        if self._collateral_fetch_ok and self._positions_load_ok:
            claimed_markets = set()
            for _, pos in self._get_all_pair_positions(context).items():
                claimed_markets.add(pos.get('mkt_A'))
                claimed_markets.add(pos.get('mkt_B'))

            for mid, onchain in self._onchain_positions.items():
                if mid not in claimed_markets and mid in targets:
                    now_ts = time.time()
                    last_warn = self._orphan_last_warn.get(mid, 0)
                    if now_ts - last_warn >= 6 * 3600:  # throttle: 6 hours
                        side_str = "SHORT" if onchain['side'] == 1 else "LONG"
                        logger.warning("Orphan market [%d] %s %.4f tokens — NOT closing (manual review needed)",
                                       mid, side_str, onchain['size'])
                        events.append({
                            "type": "skip", "pair": f"orphan_{mid}",
                            "market_id_a": mid, "market_id_b": 0,
                            "reason": "orphan_detected",
                            "tokens": round(onchain['size'], 4),
                        })
                        self._orphan_last_warn[mid] = now_ts

        # ================================================================
        # Phase 2: ENTRY + SCALE-IN — z-score signal
        # ================================================================
        entry_candidates = []
        scalein_candidates = []

        for pair_name, (mkt_A, mkt_B) in self._pairs.items():
            pd_ = pair_data.get(pair_name)
            if not pd_ or pd_['z'] is None:
                continue

            z = pd_['z']
            if abs(z) <= config.K_ENTRY:
                continue

            best_scenario = 1 if z > 0 else 2
            bid_A, ask_A = pd_['bid_A'], pd_['ask_A']
            bid_B, ask_B = pd_['bid_B'], pd_['ask_B']

            pair_pos = self._get_pair_pos(context, pair_name)

            if not pair_pos:
                # New entry — check on-chain doesn't have untracked positions
                if self._onchain_positions and (
                        mkt_A in self._onchain_positions or mkt_B in self._onchain_positions):
                    # Only skip if the on-chain position is NOT claimed by another pair
                    mkt_A_claimed = self._market_claimed_by_pair(context, mkt_A)
                    mkt_B_claimed = self._market_claimed_by_pair(context, mkt_B)
                    if (mkt_A in self._onchain_positions and not mkt_A_claimed) or \
                       (mkt_B in self._onchain_positions and not mkt_B_claimed):
                        events.append({
                            "type": "skip", "pair": pair_name,
                            "market_id_a": mkt_A, "market_id_b": mkt_B,
                            "z_score": round(z, 3),
                            "reason": "onchain_position_exists",
                        })
                        continue

                # Depth score
                spot_price = context.data.get_spot_price(mkt_A) or 1.0
                book_A = self._get_orderbook(context, mkt_A)
                book_B = self._get_orderbook(context, mkt_B)

                if best_scenario == 1:
                    depth_A = sum(sz for _, sz in book_A.get('bids', []))
                    depth_B = sum(sz for _, sz in book_B.get('asks', []))
                else:
                    depth_A = sum(sz for _, sz in book_A.get('asks', []))
                    depth_B = sum(sz for _, sz in book_B.get('bids', []))

                depth_score = min(depth_A, depth_B) * spot_price
                if depth_score < config.MIN_DEPTH_USD:
                    events.append({
                        "type": "skip", "pair": pair_name,
                        "market_id_a": mkt_A, "market_id_b": mkt_B,
                        "z_score": round(z, 3),
                        "reason": "insufficient_depth",
                    })
                    continue

                # Market exclusivity: one market can only belong to one pair
                if (self._market_occupied(context, mkt_A) or
                        self._market_occupied(context, mkt_B)):
                    events.append({
                        "type": "skip", "pair": pair_name,
                        "market_id_a": mkt_A, "market_id_b": mkt_B,
                        "z_score": round(z, 3),
                        "reason": "market_occupied",
                    })
                    continue

                # Margin pre-check
                rate_A = bid_A if best_scenario == 1 else ask_A
                rate_B = ask_B if best_scenario == 1 else bid_B
                has_margin, margin_max = self._check_margin(
                    context, mkt_A, mkt_B, rate_A, rate_B, spot_price)
                if not has_margin:
                    events.append({
                        "type": "skip", "pair": pair_name,
                        "market_id_a": mkt_A, "market_id_b": mkt_B,
                        "z_score": round(z, 3),
                        "reason": "insufficient_margin",
                    })
                    continue

                entry_candidates.append({
                    'pair_name': pair_name, 'mkt_A': mkt_A, 'mkt_B': mkt_B,
                    'bid_A': bid_A, 'ask_A': ask_A, 'bid_B': bid_B, 'ask_B': ask_B,
                    'best_scenario': best_scenario, 'max_spread': abs(pd_['spread']),
                    'depth_score': depth_score, 'spot_price': spot_price,
                    'z_score': z, 'margin_max': margin_max,
                })

            else:
                # Scale-in candidate
                layers = pair_pos.get('layers', [])
                if len(layers) >= config.MAX_LAYERS:
                    continue
                last_addon = datetime.fromisoformat(
                    pair_pos.get('last_addon_time', pair_pos['entry_time']))
                hours_since = (context.now - last_addon).total_seconds() / 3600
                if hours_since < config.MIN_ADDON_INTERVAL_HOURS:
                    continue
                if pair_pos['side_A'] != (1 if best_scenario == 1 else 0):
                    continue

                spot_price = context.data.get_spot_price(mkt_A) or 1.0
                book_A = self._get_orderbook(context, mkt_A)
                book_B = self._get_orderbook(context, mkt_B)
                if best_scenario == 1:
                    depth_A = sum(sz for _, sz in book_A.get('bids', []))
                    depth_B = sum(sz for _, sz in book_B.get('asks', []))
                else:
                    depth_A = sum(sz for _, sz in book_A.get('asks', []))
                    depth_B = sum(sz for _, sz in book_B.get('bids', []))
                depth_score = min(depth_A, depth_B) * spot_price

                scalein_candidates.append({
                    'pair_name': pair_name, 'mkt_A': mkt_A, 'mkt_B': mkt_B,
                    'bid_A': bid_A, 'ask_A': ask_A, 'bid_B': bid_B, 'ask_B': ask_B,
                    'best_scenario': best_scenario, 'max_spread': abs(pd_['spread']),
                    'depth_score': depth_score, 'spot_price': spot_price,
                    'z_score': z, 'current_tokens': pair_pos['tokens'],
                    'layers': layers,
                })

        # Process entries (most extreme z first)
        if config.EXIT_ONLY:
            entry_candidates.clear()
            scalein_candidates.clear()
        entry_candidates.sort(key=lambda c: abs(c['z_score']), reverse=True)
        for cand in entry_candidates:
            # Re-check market exclusivity (previous entry in this tick may have claimed it)
            if (self._market_occupied(context, cand['mkt_A']) or
                    self._market_occupied(context, cand['mkt_B'])):
                events.append({
                    "type": "skip", "pair": cand['pair_name'],
                    "market_id_a": cand['mkt_A'], "market_id_b": cand['mkt_B'],
                    "z_score": round(cand['z_score'], 3),
                    "reason": "market_occupied",
                })
                continue

            total_allocated = self._get_total_allocated(context)
            remaining_capital = config.MAX_CAPITAL - total_allocated
            if remaining_capital <= 0:
                break

            depth_based_capital = cand['depth_score'] * config.DEPTH_UTILIZATION
            effective_capital = min(depth_based_capital, remaining_capital / 2.0)

            tokens = self._run_vwap_capacity_scan(
                context, cand['mkt_A'], cand['mkt_B'], cand['best_scenario'],
                cand['bid_A'], cand['ask_A'], cand['bid_B'], cand['ask_B'],
                cand['spot_price'], effective_capital)

            margin_max = cand.get('margin_max', float('inf'))
            if tokens > margin_max:
                tokens = margin_max

            info_cap = context.data.get_market_info(cand['mkt_A'])
            maturity_ts = int(info_cap.get('imData', {}).get('maturity', 0))
            ttr = max(0.0, (maturity_ts - time.time()) / 31_536_000)
            max_yield = (max(cand['bid_A'], cand['ask_B']) if cand['best_scenario'] == 1
                         else max(cand['bid_B'], cand['ask_A']))
            tpv = abs(max_yield) * ttr * cand['spot_price'] if max_yield else 0.01
            estimated_usd = tokens * tpv

            if tokens < 0.01 or estimated_usd < config.MIN_ENTRY_USD:
                events.append({
                    "type": "skip", "pair": cand['pair_name'],
                    "market_id_a": cand['mkt_A'], "market_id_b": cand['mkt_B'],
                    "z_score": round(cand['z_score'], 3),
                    "reason": "insufficient_capacity",
                })
                continue

            entry_events = self._execute_entry(context, cand, tokens, layers=None)
            events.extend(entry_events)

        # Process scale-in
        scalein_candidates.sort(key=lambda c: abs(c['z_score']), reverse=True)
        for cand in scalein_candidates:
            total_allocated = self._get_total_allocated(context)
            remaining_capital = config.MAX_CAPITAL - total_allocated
            if remaining_capital <= 0:
                break

            depth_based_capital = cand['depth_score'] * config.DEPTH_UTILIZATION
            effective_capital = min(depth_based_capital, remaining_capital / 2.0)

            total_capacity = self._run_vwap_capacity_scan(
                context, cand['mkt_A'], cand['mkt_B'], cand['best_scenario'],
                cand['bid_A'], cand['ask_A'], cand['bid_B'], cand['ask_B'],
                cand['spot_price'], effective_capital)

            addon_tokens = total_capacity - cand['current_tokens']
            if addon_tokens < config.MIN_ADDON_TOKENS:
                continue

            max_yield = (max(cand['bid_A'], cand['ask_B']) if cand['best_scenario'] == 1
                         else max(cand['bid_B'], cand['ask_A']))
            estimated_usd = addon_tokens * abs(max_yield) if max_yield else 0
            if estimated_usd < config.MIN_ENTRY_USD:
                continue

            entry_events = self._execute_entry(
                context, cand, addon_tokens, layers=cand['layers'])
            events.extend(entry_events)

        # Persist pair positions on any mutation
        if self._positions_dirty:
            self._save_pair_positions(context)

        # Persist spread history
        if self._tick_count == 1 or self._tick_count % HISTORY_SAVE_INTERVAL == 0:
            self._save_spread_history()

        return events

    # ------------------------------------------------------------------
    # State sync from on-chain (pair-level)
    # ------------------------------------------------------------------

    def _sync_state_from_onchain(self, context: IContext):
        if not self._collateral_fetch_ok:
            return

        onchain_ids = set(self._onchain_positions.keys())
        all_pairs = self._get_all_pair_positions(context)

        # Dust: close tiny on-chain positions
        for mid, onchain in self._onchain_positions.items():
            if onchain['size'] < config.DUST_THRESHOLD_TOKENS:
                side_str = "SHORT" if onchain['side'] == 1 else "LONG"
                logger.info("  [%d] Dust %s %.6f tokens — auto-closing",
                            mid, side_str, onchain['size'])
                try:
                    context.executor.close_position(
                        market_id=mid, side=onchain['side'],
                        tokens=onchain['size'],
                        tokens_wei=onchain.get('size_wei', ''),
                    )
                except Exception as e:
                    logger.warning("  [%d] Dust close failed: %s", mid, e)

        # Validate pair states against on-chain
        for pair_name, pos in list(all_pairs.items()):
            mkt_A = pos.get('mkt_A')
            mkt_B = pos.get('mkt_B')
            a_on = mkt_A in onchain_ids
            b_on = mkt_B in onchain_ids

            if not a_on and not b_on:
                # Both legs gone from chain
                logger.info("Pair [%s] no longer on-chain, clearing", pair_name)
                self._clear_pair_pos(context, pair_name)
                continue

            # Update tokens from on-chain (take the minimum — our pair's
            # share may be less than on-chain total if market is shared)
            if a_on and b_on:
                oc_A = self._onchain_positions[mkt_A]
                oc_B = self._onchain_positions[mkt_B]

                # Correct side from on-chain (source of truth)
                if pos.get('side_A') != oc_A['side'] or pos.get('side_B') != oc_B['side']:
                    logger.warning("  Pair [%s] side mismatch: state A=%s B=%s, chain A=%s B=%s — correcting",
                                   pair_name, pos.get('side_A'), pos.get('side_B'),
                                   oc_A['side'], oc_B['side'])
                    pos['side_A'] = oc_A['side']
                    pos['side_B'] = oc_B['side']
                    self._positions_dirty = True

                # Our pair's tokens can't exceed on-chain total
                on_tokens = min(oc_A['size'], oc_B['size'])
                if pos['tokens'] > on_tokens + 0.1:
                    scale = on_tokens / pos['tokens'] if pos['tokens'] > 0 else 0
                    pos['tokens'] = on_tokens
                    pos['position_size_A'] = pos.get('position_size_A', 0) * scale
                    pos['position_size_B'] = pos.get('position_size_B', 0) * scale
                    for layer in pos.get('layers', []):
                        layer['tokens'] *= scale
                    self._positions_dirty = True
                    logger.info("  Pair [%s] scaled to %.4f tokens (on-chain sync)",
                                pair_name, on_tokens)

            elif not a_on or not b_on:
                # One leg gone — log warning but do NOT clear pair or close.
                # Remaining leg settles at maturity; force close risks large loss.
                gone = mkt_A if not a_on else mkt_B
                remaining = mkt_B if not a_on else mkt_A
                logger.warning("Pair [%s] leg [%d] gone from chain, [%d] remains — holding for settlement",
                               pair_name, gone, remaining)

        # Recover unknown on-chain positions as new pair records
        # (skip if already claimed by an existing pair)
        all_pairs = self._get_all_pair_positions(context)  # re-read after mutations
        claimed = set()
        for _, pos in all_pairs.items():
            claimed.add(pos.get('mkt_A'))
            claimed.add(pos.get('mkt_B'))

        for mid, onchain in self._onchain_positions.items():
            if mid in claimed:
                continue
            if onchain['size'] < config.DUST_THRESHOLD_TOKENS:
                continue
            # Try to find a matching pair from generated pairs
            for pname, (a, b) in self._pairs.items():
                if mid == a or mid == b:
                    partner = b if mid == a else a
                    if partner in self._onchain_positions and partner not in claimed:
                        # Found a pair — recover both legs
                        oc_partner = self._onchain_positions[partner]
                        entry_time = datetime.now(timezone.utc).isoformat()
                        if hasattr(context.state, 'get_entry_time'):
                            et = context.state.get_entry_time(self.name, mid)
                            if et:
                                entry_time = et

                        tokens = min(onchain['size'], oc_partner['size'])
                        side_A = onchain['side'] if mid == a else oc_partner['side']
                        side_B = oc_partner['side'] if mid == a else onchain['side']

                        self._set_pair_pos(context, pname, {
                            "mkt_A": a, "mkt_B": b,
                            "side_A": side_A, "side_B": side_B,
                            "entry_time": entry_time,
                            "tokens": tokens,
                            "position_size_A": 0, "position_size_B": 0,
                            "entry_rate_A": onchain.get('entry_rate', 0),
                            "entry_rate_B": oc_partner.get('entry_rate', 0),
                            "round_id": f"recovered_{pname}",
                            "layers": [{
                                "round_id": f"recovered_{pname}",
                                "tokens": tokens,
                                "position_size_A": 0,
                                "position_size_B": 0,
                                "entry_time": entry_time,
                                "entry_apr_A": onchain.get('entry_rate', 0),
                                "entry_apr_B": oc_partner.get('entry_rate', 0),
                            }],
                        })
                        claimed.add(mid)
                        claimed.add(partner)
                        logger.info("  Recovered pair [%s] %.4f tokens from on-chain",
                                    pname, tokens)
                        break

    # ------------------------------------------------------------------
    # EXIT: z-score mean reversion + top-3 depth + z=None fallback
    # ------------------------------------------------------------------

    def _check_exit(self, context, pair_name, pair_pos, pd_) -> list:
        events = []
        mkt_A = pair_pos['mkt_A']
        mkt_B = pair_pos['mkt_B']
        entry_time = datetime.fromisoformat(pair_pos['entry_time'])
        duration_hours = (context.now - entry_time).total_seconds() / 3600
        side_A = pair_pos['side_A']
        z = pd_['z']

        if z is not None:
            dir_z = z if side_A == 1 else -z
        else:
            dir_z = None

        # Current directional spread (for absolute spread guard)
        mid_A = (pd_['bid_A'] + pd_['ask_A']) / 2.0
        mid_B = (pd_['bid_B'] + pd_['ask_B']) / 2.0
        current_spread = (mid_A - mid_B) if side_A == 1 else (mid_B - mid_A)

        # Exit only on confirmed signal — no force close.
        # Positions settle naturally at maturity; force close risks large realized loss.
        is_exit_signal = (
            dir_z is not None
            and dir_z < config.K_EXIT
            and current_spread < config.ENTRY_SPREAD_THRESHOLD
            and duration_hours >= config.MIN_HOLD_HOURS
        )

        should_close = False
        if is_exit_signal:
            last_close = pair_pos.get('last_exit_batch_time')
            if last_close is None:
                should_close = True
            else:
                minutes_since = (context.now - datetime.fromisoformat(last_close)).total_seconds() / 60
                if minutes_since >= config.EXIT_BATCH_MINUTES:
                    should_close = True

        if not should_close:
            events.append({
                "type": "hold", "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "current_spread": round(pd_['spread'], 6),
                "z_score": round(z, 3) if z is not None else None,
                "dir_z": round(dir_z, 3) if dir_z is not None else None,
                "duration_hours": round(duration_hours, 2),
                "tokens_a": pair_pos['tokens'],
                "tokens_b": pair_pos['tokens'],
            })
            return events

        # Determine exit depth via marginal PnL scan + top-3 floor
        book_A = self._get_orderbook(context, mkt_A)
        book_B = self._get_orderbook(context, mkt_B)

        if side_A == 1:
            exit_liq_A = book_A.get('asks', [])
            exit_liq_B = book_B.get('bids', [])
        else:
            exit_liq_A = book_A.get('bids', [])
            exit_liq_B = book_B.get('asks', [])

        total_tokens = pair_pos['tokens']

        # Marginal PnL scan only — no top-3 floor override.
        # Every closed token is guaranteed: execution spread < entry threshold.
        safe_close_tokens = _marginal_exit_tokens(
            exit_liq_A, exit_liq_B, side_A, total_tokens,
            config.ENTRY_SPREAD_THRESHOLD)

        if safe_close_tokens < 0.1:
            return events

        reason = f"z={dir_z:.2f}<{config.K_EXIT}"

        logger.info("[%s] EXIT %s, Closing=%.1f/%.1f Tokens",
                    pair_name, reason, safe_close_tokens, total_tokens)

        round_id = pair_pos.get('round_id', f"{pair_name}_{pair_pos['entry_time']}")

        # Never use exact wei when market is potentially shared
        full_close = (safe_close_tokens >= total_tokens - 0.1)

        result = context.executor.close_dual_position(
            mkt_a=mkt_A, side_a=side_A, tokens_a=safe_close_tokens,
            mkt_b=mkt_B, side_b=pair_pos['side_B'], tokens_b=safe_close_tokens,
            tokens_wei_a='', tokens_wei_b='',
            round_id=round_id,
        )

        if result:
            if full_close:
                self._clear_pair_pos(context, pair_name)
            else:
                close_fraction = safe_close_tokens / total_tokens
                pair_pos['tokens'] -= safe_close_tokens
                pair_pos['position_size_A'] = pair_pos.get('position_size_A', 0) * (1 - close_fraction)
                pair_pos['position_size_B'] = pair_pos.get('position_size_B', 0) * (1 - close_fraction)
                for layer in pair_pos.get('layers', []):
                    layer['tokens'] *= (1 - close_fraction)
                pair_pos['last_exit_batch_time'] = context.now.isoformat()

            events.append({
                "type": "exit", "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "reason": reason,
                "current_spread": round(pd_['spread'], 6),
                "z_score": round(z, 3) if z is not None else None,
                "duration_hours": round(duration_hours, 2),
                "tokens_closed": round(safe_close_tokens, 4),
                "round_id": round_id,
            })
        else:
            logger.warning("[%s] EXIT failed (atomic), will retry next tick", pair_name)
            events.append({
                "type": "exec_fail", "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "reason": "dual_exit_failed",
                "spread": round(pd_['spread'], 6),
                "tokens": round(safe_close_tokens, 4),
            })

        return events

    # ------------------------------------------------------------------
    # ENTRY: atomic dual order with pair-level state
    # ------------------------------------------------------------------

    def _execute_entry(self, context, cand, tokens, layers=None) -> list:
        events = []
        mkt_A = cand['mkt_A']
        mkt_B = cand['mkt_B']
        bid_A, ask_A = cand['bid_A'], cand['ask_A']
        bid_B, ask_B = cand['bid_B'], cand['ask_B']
        best_scenario = cand['best_scenario']
        pair_name = cand['pair_name']
        is_scalein = layers is not None

        info_A = context.data.get_market_info(mkt_A)
        info_B = context.data.get_market_info(mkt_B)
        tick_step_A = float(info_A.get('imData', {}).get('tickStep', 1))
        tick_step_B = float(info_B.get('imData', {}).get('tickStep', 1))

        book_A = self._get_orderbook(context, mkt_A)
        book_B = self._get_orderbook(context, mkt_B)

        if best_scenario == 1:
            side_A, side_B = 1, 0
            entry_apr_A = _calc_vwap(book_A.get('bids', []), tokens) or bid_A
            entry_apr_B = _calc_vwap(book_B.get('asks', []), tokens) or ask_B
            size_usd_A = tokens * bid_A
            size_usd_B = tokens * ask_B
        else:
            side_A, side_B = 0, 1
            entry_apr_A = _calc_vwap(book_A.get('asks', []), tokens) or ask_A
            entry_apr_B = _calc_vwap(book_B.get('bids', []), tokens) or bid_B
            size_usd_A = tokens * ask_A
            size_usd_B = tokens * bid_B

        limit_A = PricingEngine.calculate_limit_tick(
            side=side_A, best_bid=bid_A, best_ask=ask_A, tick_step=tick_step_A)
        limit_B = PricingEngine.calculate_limit_tick(
            side=side_B, best_bid=bid_B, best_ask=ask_B, tick_step=tick_step_B)

        round_id = f"{pair_name}_{context.now.isoformat()}"

        action = "SCALE-IN" if is_scalein else "ENTRY"
        logger.info("[%s] %s Scenario %d (z=%.2f, Tokens=%.1f)",
                    pair_name, action, best_scenario,
                    cand['z_score'], tokens)

        success = context.executor.submit_dual_order(
            mkt_a=mkt_A, side_a=side_A,
            mkt_b=mkt_B, side_b=side_B,
            size_tokens=tokens,
            limit_tick_a=limit_A, limit_tick_b=limit_B,
            round_id=round_id,
        )

        if success:
            new_layer = {
                "round_id": round_id,
                "tokens": tokens,
                "position_size_A": size_usd_A,
                "position_size_B": size_usd_B,
                "entry_time": context.now.isoformat(),
                "entry_apr_A": entry_apr_A,
                "entry_apr_B": entry_apr_B,
            }

            if layers is None:
                all_layers = [new_layer]
            else:
                all_layers = layers + [new_layer]

            total_tokens = sum(l['tokens'] for l in all_layers)
            total_size_A = sum(l.get('position_size_A', 0) for l in all_layers)
            total_size_B = sum(l.get('position_size_B', 0) for l in all_layers)
            first_entry = all_layers[0]['entry_time']

            w_apr_A = sum(l['tokens'] * l.get('entry_apr_A', 0) for l in all_layers) / total_tokens
            w_apr_B = sum(l['tokens'] * l.get('entry_apr_B', 0) for l in all_layers) / total_tokens

            # Store pair-level state (single record for both legs)
            self._set_pair_pos(context, pair_name, {
                "mkt_A": mkt_A, "mkt_B": mkt_B,
                "side_A": side_A, "side_B": side_B,
                "entry_time": first_entry,
                "position_size_A": total_size_A,
                "position_size_B": total_size_B,
                "entry_rate_A": w_apr_A,
                "entry_rate_B": w_apr_B,
                "tokens": total_tokens,
                "round_id": all_layers[0]['round_id'],
                "last_addon_time": context.now.isoformat(),
                "layers": all_layers,
            })

            rate_A = bid_A if best_scenario == 1 else ask_A
            rate_B = ask_B if best_scenario == 1 else bid_B
            self._deduct_im_from_cache(context, mkt_A, mkt_B, rate_A, rate_B, tokens)

            events.append({
                "type": "entry", "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "scenario": best_scenario,
                "spread": round(cand['max_spread'], 6),
                "z_score": round(cand['z_score'], 3),
                "tokens": round(tokens, 4),
                "size_usd_a": round(size_usd_A, 2),
                "size_usd_b": round(size_usd_B, 2),
                "round_id": round_id,
                "is_scalein": is_scalein,
                "layer_count": len(all_layers),
            })
        else:
            logger.error("[%s] Dual %s failed (atomic)", pair_name, action.lower())
            events.append({
                "type": "exec_fail", "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "reason": f"dual_{action.lower()}_failed",
                "spread": round(cand['max_spread'], 6),
                "tokens": round(tokens, 4),
            })

        return events
