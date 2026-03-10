"""
FR Arbitrage Strategy

Exploits yield spreads between pairs of markets with different maturities
for the same underlying asset.

Entry: When spread between two maturities exceeds threshold.
Exit: When spread narrows below threshold OR max hold time exceeded.
Direction: Short the higher-rate maturity, long the lower-rate maturity.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from strategies.framework.base_strategy import BaseStrategy
from strategies.framework.interfaces import IContext
from strategies.framework.pricing import PricingEngine
import strategies.fr_arb.config as config

logger = logging.getLogger(__name__)

# Max parallel orderbook fetches
OB_WORKERS = 8


class FRArbitrageStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(name="fr_arb", target_markets=[])
        self._ob_cache = {}  # per-tick orderbook cache
        self._collateral_cache = {}  # per-tick: {tokenId: availableBalance}
        self._im_params_cache = {}  # per-tick: {market_id: im_params_dict}
        self._onchain_positions = {}  # per-tick: {market_id: {side, size, ...}}
        self._pairs = {}  # dynamically generated: {label: (mkt_a, mkt_b)}

    def _prefetch_orderbooks(self, context: IContext):
        """Fetch all orderbooks in parallel at tick start."""
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
        """Get orderbook from per-tick cache."""
        if market_id not in self._ob_cache:
            self._ob_cache[market_id] = context.data.get_orderbook(market_id)
        return self._ob_cache[market_id]

    def _get_best_prices(self, context: IContext, market_id: int):
        """Get best bid/ask from orderbook. Returns (None, None) if illiquid."""
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

    def _get_im_params(self, context: IContext, market_id: int) -> dict:
        """Extract IM parameters from market info. Cached per tick."""
        if market_id in self._im_params_cache:
            return self._im_params_cache[market_id]
        info = context.data.get_market_info(market_id)
        im_data = info.get('imData', {})
        cfg = info.get('config', {})
        # kIM is stored as BigInt string with 18 decimals
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
                      spot_price: float) -> tuple[bool, float]:
        """
        Margin pre-check: can we open even 1 token on both legs?
        Returns (has_margin, max_tokens_by_margin).
        If collateral data unavailable, returns (True, inf) to skip the check.
        """
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

        im_per_token_A = PricingEngine.calculate_im_per_token(
            rate=rate_A, k_im=params_A['k_im'],
            t_thresh_seconds=params_A['t_thresh'],
            i_tick_thresh=params_A['i_tick_thresh'],
            tick_step=params_A['tick_step'],
            margin_floor=params_A['margin_floor'],
            time_to_maturity_seconds=ttm_A,
        )
        im_per_token_B = PricingEngine.calculate_im_per_token(
            rate=rate_B, k_im=params_B['k_im'],
            t_thresh_seconds=params_B['t_thresh'],
            i_tick_thresh=params_B['i_tick_thresh'],
            tick_step=params_B['tick_step'],
            margin_floor=params_B['margin_floor'],
            time_to_maturity_seconds=ttm_B,
        )

        # IM is denominated in collateral token (not USD).
        # im_per_token = IM required per 1 token of position size, in collateral units.
        tid_A = params_A['token_id']
        tid_B = params_B['token_id']
        avail_A = self._collateral_cache.get(tid_A, 0)
        avail_B = self._collateral_cache.get(tid_B, 0)

        if im_per_token_A <= 0 or im_per_token_B <= 0:
            return True, float('inf')

        max_tokens_A = avail_A / im_per_token_A if im_per_token_A > 0 else float('inf')
        max_tokens_B = avail_B / im_per_token_B if im_per_token_B > 0 else float('inf')

        # Both legs share same collateral pool if same tokenId
        if tid_A == tid_B:
            combined_im = im_per_token_A + im_per_token_B
            max_tokens = avail_A / combined_im if combined_im > 0 else float('inf')
        else:
            max_tokens = min(max_tokens_A, max_tokens_B)

        has_margin = max_tokens >= 1.0
        return has_margin, max_tokens

    def _deduct_im_from_cache(self, context: IContext, mkt_A: int, mkt_B: int,
                              rate_A: float, rate_B: float, tokens: float):
        """
        After a successful entry, reduce _collateral_cache by estimated IM
        so subsequent entries in the same tick see reduced available balance.
        """
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

    def on_tick(self, context: IContext) -> list[dict]:
        self._ob_cache.clear()  # fresh cache each tick
        self._collateral_cache.clear()
        self._im_params_cache.clear()
        self._onchain_positions.clear()
        self._collateral_fetch_ok = False
        events = []

        # Pre-fetch all market info (populates cache for pair generation)
        context.data.get_all_market_ids()

        # Dynamically generate pairs from active markets
        self._pairs = context.data.generate_pairs(
            allowed_token_ids=config.ALLOWED_TOKEN_IDS or None)
        targets = set()
        for mkt_a, mkt_b in self._pairs.values():
            targets.add(mkt_a)
            targets.add(mkt_b)
        self.target_markets = list(targets)

        # Pre-fetch orderbooks in parallel
        self._prefetch_orderbooks(context)

        # Fetch collateral detail for margin pre-check + on-chain position check
        if config.USER_ADDRESS and hasattr(context.data, 'get_collateral_detail'):
            try:
                detail = context.data.get_collateral_detail(config.USER_ADDRESS)
                for tid, info in detail.items():
                    self._collateral_cache[tid] = info['available']
                    for pos in info.get('positions', []):
                        self._onchain_positions[pos['market_id']] = pos
                self._collateral_fetch_ok = True
            except Exception as e:
                logger.warning("Collateral fetch failed, skipping margin check: %s", e)

        # Sync state from on-chain data (source of truth)
        self._sync_state_from_onchain(context)

        processed_orphans: set[int] = set()

        for pair_name, (mkt_A, mkt_B) in self._pairs.items():
            pos_A = context.state.get_position(self.name, mkt_A)
            pos_B = context.state.get_position(self.name, mkt_B)

            bid_A, ask_A = self._get_best_prices(context, mkt_A)
            bid_B, ask_B = self._get_best_prices(context, mkt_B)

            if None in [bid_A, ask_A, bid_B, ask_B]:
                continue

            info_A = context.data.get_market_info(mkt_A)
            info_B = context.data.get_market_info(mkt_B)
            tick_step_A = float(info_A.get('imData', {}).get('tickStep', 1))
            tick_step_B = float(info_B.get('imData', {}).get('tickStep', 1))

            events.append({
                "type": "scan",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "bid_a": bid_A, "ask_a": ask_A,
                "bid_b": bid_B, "ask_b": ask_B,
                "has_position": pos_A is not None and pos_B is not None,
            })

            # --- EXIT LOGIC ---
            if pos_A and pos_B:
                exit_events = self._check_exit(
                    context, pair_name, mkt_A, mkt_B, pos_A, pos_B,
                    bid_A, ask_A, bid_B, ask_B,
                )
                events.extend(exit_events)

            # --- ORPHAN: only one leg in THIS pair ---
            elif pos_A or pos_B:
                orphan_mid = mkt_A if pos_A else mkt_B
                if orphan_mid in processed_orphans:
                    continue
                # Check if this market is paired with ANY other market
                is_paired = False
                for _, (a, b) in self._pairs.items():
                    partner = b if a == orphan_mid else (a if b == orphan_mid else None)
                    if partner is not None and context.state.get_position(self.name, partner):
                        is_paired = True
                        break
                if not is_paired:
                    processed_orphans.add(orphan_mid)
                    orphan_events = self._close_orphan(
                        context, pair_name, mkt_A, mkt_B, pos_A, pos_B)
                    events.extend(orphan_events)

            # --- ENTRY LOGIC ---
            elif not pos_A and not pos_B:
                entry_events = self._check_entry(
                    context, pair_name, mkt_A, mkt_B,
                    bid_A, ask_A, bid_B, ask_B,
                    tick_step_A, tick_step_B,
                )
                events.extend(entry_events)

        return events

    def _sync_state_from_onchain(self, context: IContext):
        """
        Rebuild state from on-chain positions each tick.
        On-chain data is the source of truth — state is just a projection.
        """
        if not self._collateral_fetch_ok:
            return

        all_pos = context.state.get_all_positions(self.name)
        onchain_ids = set(self._onchain_positions.keys())

        # Remove positions no longer on-chain
        for mid in list(all_pos.keys()):
            if mid not in onchain_ids:
                logger.info("Position [%d] no longer on-chain, clearing state", mid)
                context.state.clear_position(self.name, mid)

        # Add/update on-chain positions in state
        for mid, onchain in self._onchain_positions.items():
            existing = context.state.get_position(self.name, mid)
            if existing:
                # Sync size + wei from on-chain
                existing['tokens'] = onchain['size']
                existing['tokens_wei'] = onchain.get('size_wei', '')
                existing['side'] = onchain['side']
            else:
                # New position — look up entry_time
                entry_time = None
                if hasattr(context.state, 'get_entry_time'):
                    entry_time = context.state.get_entry_time(self.name, mid)
                if not entry_time:
                    entry_time = datetime.now(timezone.utc).isoformat()
                    logger.warning("  [%d] No entry time found, using now", mid)

                context.state.set_position(self.name, mid, {
                    "entry_time": entry_time,
                    "side": onchain['side'],
                    "entry_rate": onchain.get('entry_rate', 0),
                    "position_size": 0,
                    "tokens": onchain['size'],
                    "tokens_wei": onchain.get('size_wei', ''),
                    "token_id": onchain.get('token_id', 0),
                })
                side_str = "SHORT" if onchain['side'] == 1 else "LONG"
                logger.info("  Synced [%d] %s %.4f tokens from on-chain",
                            mid, side_str, onchain['size'])

    def _close_orphan(self, context, pair_name, mkt_A, mkt_B,
                      pos_A, pos_B) -> list[dict]:
        """
        Auto-close an orphan position (one leg missing, other still open).
        State is already synced with on-chain, so the position is confirmed to exist.
        """
        events = []
        orphan_pos = pos_A or pos_B
        orphan_mid = mkt_A if pos_A else mkt_B
        entry_time = datetime.fromisoformat(orphan_pos['entry_time'])
        duration_hours = (context.now - entry_time).total_seconds() / 3600
        side = orphan_pos['side']
        tokens = orphan_pos['tokens']
        side_str = "SHORT" if side == 1 else "LONG"

        logger.warning(
            "[%s] Orphan [%d] %s %.4f tokens (%.1fh) — auto-closing",
            pair_name, orphan_mid, side_str, tokens, duration_hours,
        )

        result = context.executor.close_position(
            market_id=orphan_mid, side=side, tokens=tokens,
            tokens_wei=orphan_pos.get('tokens_wei', ''),
            round_id=orphan_pos.get('round_id'),
        )

        if result:
            context.state.clear_position(self.name, orphan_mid)
            events.append({
                "type": "exit",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "reason": "orphan_auto_close",
                "current_spread": 0,
                "duration_hours": round(duration_hours, 2),
                "tokens_closed": round(tokens, 4),
                "round_id": orphan_pos.get('round_id', ''),
            })
        else:
            logger.warning("[%s] Orphan close failed [%d], will retry next tick",
                           pair_name, orphan_mid)
            events.append({
                "type": "exec_fail",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "reason": "orphan_close_failed",
                "spread": 0,
                "tokens": round(tokens, 4),
            })

        return events

    def _check_exit(self, context, pair_name, mkt_A, mkt_B, pos_A, pos_B,
                    bid_A, ask_A, bid_B, ask_B) -> list[dict]:
        events = []
        entry_time = datetime.fromisoformat(pos_A['entry_time'])
        duration_hours = (context.now - entry_time).total_seconds() / 3600

        side_A = pos_A['side']  # 1=SHORT, 0=LONG

        # Close rates: reverse the entry direction
        close_rate_A = ask_A if side_A == 1 else bid_A
        close_rate_B = bid_B if side_A == 1 else ask_B

        # Current spread in the same direction as entry
        if side_A == 1:
            current_spread = close_rate_A - close_rate_B
        else:
            current_spread = close_rate_B - close_rate_A

        force_close = duration_hours >= config.MAX_HOLD_HOURS
        normal_close = (
            current_spread < config.EXIT_SPREAD_THRESHOLD
            and duration_hours >= config.MIN_HOLD_HOURS
        )

        if not (normal_close or force_close):
            events.append({
                "type": "hold",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "current_spread": round(current_spread, 6),
                "duration_hours": round(duration_hours, 2),
                "tokens_a": pos_A['tokens'],
                "tokens_b": pos_B['tokens'],
            })
            return events

        # Use cached orderbooks for liquidity check
        book_A = self._get_orderbook(context, mkt_A)
        book_B = self._get_orderbook(context, mkt_B)

        if side_A == 1:
            liq_A = book_A.get('asks', [])
            liq_B = book_B.get('bids', [])
        else:
            liq_A = book_A.get('bids', [])
            liq_B = book_B.get('asks', [])

        tokens_A = pos_A['tokens']
        tokens_B = pos_B['tokens']
        target_close = min(tokens_A, tokens_B)

        # Use full book depth (all levels) — the FOK order with slippage
        # protection will handle price limits on-chain.
        avail_A = sum(sz for _, sz in liq_A)
        avail_B = sum(sz for _, sz in liq_B)

        safe_close_tokens = min(target_close, avail_A, avail_B)
        if safe_close_tokens < 0.1:
            return events

        reason = "FORCE (Max Time)" if force_close else f"Spread={current_spread:.2%}"
        logger.info("[%s] EXIT %s, Closing=%.1f/%.1f Tokens",
                    pair_name, reason, safe_close_tokens, target_close)

        size_usd_A = safe_close_tokens * (ask_A if side_A == 1 else bid_A)
        size_usd_B = safe_close_tokens * (bid_B if side_A == 1 else ask_B)

        round_id = pos_A.get('round_id', f"{pair_name}_{pos_A['entry_time']}")

        # Pass exact wei values when closing full position to avoid float dust
        full_close = (safe_close_tokens >= target_close - 0.1)
        wei_a = pos_A.get('tokens_wei', '') if full_close else ''
        wei_b = pos_B.get('tokens_wei', '') if full_close else ''

        # Atomic dual close: both legs succeed or both fail
        result = context.executor.close_dual_position(
            mkt_a=mkt_A, side_a=side_A, tokens_a=safe_close_tokens,
            mkt_b=mkt_B, side_b=pos_B['side'], tokens_b=safe_close_tokens,
            tokens_wei_a=wei_a, tokens_wei_b=wei_b,
            round_id=round_id,
        )

        if result:
            pos_A['tokens'] -= safe_close_tokens
            pos_B['tokens'] -= safe_close_tokens
            pos_A['position_size'] = pos_A.get('position_size', 0) - size_usd_A
            pos_B['position_size'] = pos_B.get('position_size', 0) - size_usd_B

            if pos_A['tokens'] < 0.1:
                context.state.clear_position(self.name, mkt_A)
                context.state.clear_position(self.name, mkt_B)

            events.append({
                "type": "exit",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "reason": reason,
                "current_spread": round(current_spread, 6),
                "duration_hours": round(duration_hours, 2),
                "tokens_closed": round(safe_close_tokens, 4),
                "round_id": round_id,
            })
        else:
            # Atomic: both legs rejected together, will retry next tick
            logger.warning("[%s] EXIT failed (atomic), will retry next tick", pair_name)
            events.append({
                "type": "exec_fail",
                "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "reason": "dual_exit_failed",
                "spread": round(current_spread, 6),
                "tokens": round(safe_close_tokens, 4),
            })

        return events

    def _check_entry(self, context, pair_name, mkt_A, mkt_B,
                     bid_A, ask_A, bid_B, ask_B,
                     tick_step_A, tick_step_B) -> list[dict]:
        events = []

        # Scenario 1: Short A (Bid_A), Long B (Ask_B)
        spread_1 = bid_A - ask_B
        # Scenario 2: Short B (Bid_B), Long A (Ask_A)
        spread_2 = bid_B - ask_A

        best_scenario = 1 if spread_1 > spread_2 else 2
        max_spread = max(spread_1, spread_2)

        if max_spread <= config.ENTRY_SPREAD_THRESHOLD:
            return events

        # On-chain position check: skip if either market already has a position
        if self._onchain_positions and (
                mkt_A in self._onchain_positions or mkt_B in self._onchain_positions):
            onchain = {m: self._onchain_positions[m]
                       for m in (mkt_A, mkt_B) if m in self._onchain_positions}
            details = ", ".join(
                f"[{m}] {p['side']} {p['size']:.4f}" for m, p in onchain.items())
            logger.info("[%s] Skip: on-chain position exists (%s)", pair_name, details)
            events.append({
                "type": "skip",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "spread": round(max_spread, 6),
                "reason": "onchain_position_exists",
            })
            return events

        # Margin pre-check: skip pair early if insufficient collateral
        spot_price = context.data.get_spot_price(mkt_A) or 1.0
        rate_for_margin_A = bid_A if best_scenario == 1 else ask_A
        rate_for_margin_B = ask_B if best_scenario == 1 else bid_B
        has_margin, margin_max_tokens = self._check_margin(
            context, mkt_A, mkt_B,
            rate_for_margin_A, rate_for_margin_B, spot_price)

        if not has_margin:
            events.append({
                "type": "skip",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "spread": round(max_spread, 6),
                "reason": "insufficient_margin",
            })
            return events

        # Use cached orderbooks
        book_A = self._get_orderbook(context, mkt_A)
        book_B = self._get_orderbook(context, mkt_B)

        if best_scenario == 1:
            liquidity_A = book_A.get('bids', [])   # We sell to bids
            liquidity_B = book_B.get('asks', [])   # We buy from asks
        else:
            liquidity_A = book_A.get('asks', [])   # We buy from asks
            liquidity_B = book_B.get('bids', [])   # We sell to bids

        # Capital constraint: position value = Rate × TTR × Spot per token
        max_yield_price = (max(bid_A, ask_B) if best_scenario == 1
                           else max(bid_B, ask_A))
        if max_yield_price <= 0:
            max_yield_price = 0.01
        info_cap = context.data.get_market_info(mkt_A)
        maturity_ts = int(info_cap.get('imData', {}).get('maturity', 0))
        now_ts = time.time()
        time_remaining_years = max(0.0, (maturity_ts - now_ts) / 31_536_000)
        token_position_value = abs(max_yield_price) * time_remaining_years * spot_price
        if token_position_value <= 0:
            token_position_value = 0.01
        max_allowed_tokens = ((config.MAX_CAPITAL / 2.0) / token_position_value
                              if token_position_value > 0 else float('inf'))
        # Also cap by available margin
        max_allowed_tokens = min(max_allowed_tokens, margin_max_tokens)

        step = config.CAPACITY_STEP_TOKENS
        test_tokens = 0.0
        max_safe_tokens = 0.0

        while True:
            next_test = min(test_tokens + step, max_allowed_tokens)
            if next_test <= test_tokens:
                break
            test_tokens = next_test

            # VWAP for side A
            vwap_A = self._calculate_vwap(liquidity_A, test_tokens)
            if vwap_A is None:
                break

            # VWAP for side B
            vwap_B = self._calculate_vwap(liquidity_B, test_tokens)
            if vwap_B is None:
                break

            test_spread = (vwap_A - vwap_B if best_scenario == 1
                           else vwap_B - vwap_A)

            if test_spread < config.ENTRY_SPREAD_THRESHOLD:
                break
            max_safe_tokens = test_tokens

            if test_tokens >= max_allowed_tokens:
                break

        # Apply liquidity factor only when book depth is the binding constraint
        # (not when capital or margin cap stopped the walk first)
        liquidity_constrained = max_safe_tokens < max_allowed_tokens
        if liquidity_constrained:
            tokens = max_safe_tokens * config.LIQUIDITY_FACTOR
        else:
            tokens = max_safe_tokens

        # Minimum entry check (USD position value)
        estimated_usd = tokens * token_position_value
        if tokens < 0.01 or estimated_usd < config.MIN_ENTRY_USD:
            events.append({
                "type": "skip",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "spread": round(max_spread, 6),
                "reason": "insufficient_capacity",
            })
            return events
        logger.info("[%s] ENTRY Scenario %d (Spread=%.2f%%, Tokens=%.1f)",
                    pair_name, best_scenario, max_spread * 100, tokens)

        if best_scenario == 1:
            # Short A (1), Long B (0)
            side_A, side_B = 1, 0
            size_usd_A = tokens * bid_A
            size_usd_B = tokens * ask_B
        else:
            # Short B (1), Long A (0)
            side_A, side_B = 0, 1
            size_usd_A = tokens * ask_A
            size_usd_B = tokens * bid_B

        limit_A = PricingEngine.calculate_limit_tick(
            side=side_A, best_bid=bid_A, best_ask=ask_A, tick_step=tick_step_A)
        limit_B = PricingEngine.calculate_limit_tick(
            side=side_B, best_bid=bid_B, best_ask=ask_B, tick_step=tick_step_B)

        round_id = f"{pair_name}_{context.now.isoformat()}"

        # Atomic dual-market entry: both legs succeed or both fail
        success = context.executor.submit_dual_order(
            mkt_a=mkt_A, side_a=side_A,
            mkt_b=mkt_B, side_b=side_B,
            size_tokens=tokens,
            limit_tick_a=limit_A, limit_tick_b=limit_B,
            round_id=round_id,
        )

        if success:
            tokens_wei = str(int(tokens * 1e18))
            context.state.set_position(self.name, mkt_A, {
                "entry_time": context.now.isoformat(),
                "side": side_A,
                "position_size": size_usd_A,
                "entry_rate": rate_for_margin_A,
                "tokens": tokens,
                "tokens_wei": tokens_wei,
                "round_id": round_id,
            })
            context.state.set_position(self.name, mkt_B, {
                "entry_time": context.now.isoformat(),
                "side": side_B,
                "position_size": size_usd_B,
                "entry_rate": rate_for_margin_B,
                "tokens": tokens,
                "tokens_wei": tokens_wei,
                "round_id": round_id,
            })

            # Deduct estimated IM from collateral cache so subsequent
            # entries in the same tick see reduced available balance.
            self._deduct_im_from_cache(context, mkt_A, mkt_B,
                                       rate_for_margin_A, rate_for_margin_B, tokens)

            events.append({
                "type": "entry",
                "pair": pair_name,
                "market_id_a": mkt_A,
                "market_id_b": mkt_B,
                "scenario": best_scenario,
                "spread": round(max_spread, 6),
                "tokens": round(tokens, 4),
                "size_usd_a": round(size_usd_A, 2),
                "size_usd_b": round(size_usd_B, 2),
                "round_id": round_id,
            })
        else:
            # Atomic: both legs rejected together, no partial fill risk
            logger.error("[%s] Dual entry failed (atomic)", pair_name)
            events.append({
                "type": "exec_fail",
                "pair": pair_name,
                "market_id_a": mkt_A, "market_id_b": mkt_B,
                "reason": "dual_entry_failed",
                "spread": round(max_spread, 6),
                "tokens": round(tokens, 4),
            })

        return events

    @staticmethod
    def _calculate_vwap(liquidity: list, target_tokens: float):
        """Walk orderbook levels to calculate VWAP for target_tokens. Returns None if book exhausted."""
        remaining = target_tokens
        total_usd = 0.0
        for px, sz in liquidity:
            take = min(remaining, sz)
            total_usd += take * px
            remaining -= take
            if remaining <= 1e-6:
                break
        if remaining > 1e-6:
            return None
        return total_usd / target_tokens if target_tokens > 0 else 0
