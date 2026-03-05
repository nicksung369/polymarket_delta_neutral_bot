# polymarket_delta_neutral_bot.py
# Delta-Neutral Liquidity Provision Bot for Polymarket
# Strategy: Split USDC -> Hold Yes+No (4% holding rewards) -> Two-sided limit orders (liquidity rewards)
# Target: ~11% annualized on long-duration markets (e.g. 2026 FIFA World Cup)
# Based on: https://x.com/degentalk_hk/status/2029465823346295157

import os
import sys
import json
import time
import asyncio
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from web3 import Web3

import websockets

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

# ================== Configuration ==================
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HOST = "https://clob.polymarket.com"
CHAIN_ID = POLYGON

# Polygon RPC for on-chain split/merge
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

# CTF Exchange contract (Polymarket Conditional Token Framework)
CTF_EXCHANGE_ADDR = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDR = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon

# Strategy parameters
SPLIT_AMOUNT_USDC = 100.0     # Amount of USDC to split into Yes+No shares
SPREAD_FROM_MID = 0.02        # How far from midpoint to place orders (2 cents)
ORDER_SIZE_SHARES = 50.0      # Number of shares per limit order
REFRESH_INTERVAL = 60         # Seconds between order refresh cycles
MAX_SPREAD = 0.03             # Max spread for liquidity rewards eligibility (3 cents typical)
MIN_MIDPOINT = 0.10           # Avoid extreme markets (below 10%)
MAX_MIDPOINT = 0.90           # Avoid extreme markets (above 90%)
DRY_RUN = True                # Simulate only (no real orders or splits)

# ---- Anti-Fill Protection ----
# 1) Depth shield: require N shares of other orders in front of ours
MIN_DEPTH_AHEAD = 20.0        # Minimum shares ahead of our order in the book
# 2) Dynamic spread: widen spread when volatility is high
BASE_SPREAD = 0.02            # Normal spread from midpoint
VOLATILITY_SPREAD_MULT = 3.0  # spread = BASE_SPREAD + recent_vol * MULT
# 3) Circuit breaker: cancel all if midpoint moves too fast
MIDPOINT_MOVE_THRESHOLD = 0.03  # 3-cent midpoint jump triggers emergency cancel
CIRCUIT_BREAKER_COOLDOWN = 120  # Seconds to pause after circuit breaker fires
# 4) WebSocket orderbook monitoring interval
WS_MONITOR_INTERVAL = 2        # Seconds between orderbook safety checks
# 5) Smart refresh: skip cancel/replace if midpoint barely moved
MIDPOINT_SKIP_THRESHOLD = 0.005  # Skip refresh if midpoint moved < 0.5 cent

# ---- Inventory Drift Protection ----
DRIFT_THRESHOLD_PCT = 0.10     # 10% delta imbalance triggers rebalance
DRIFT_PAUSE_PCT = 0.20         # 20% imbalance pauses market entirely
REBALANCE_METHOD = "merge_resplit"  # "merge_resplit" or "hedge_order"

# ---- Multi-Market Diversification ----
MIN_MARKETS = 5                # Minimum number of markets to run
MAX_ALLOCATION_PCT = 0.20      # Max 20% of total capital per market
MIN_EXPIRY_DAYS = 30           # Only markets with >30 days to expiry
MIN_VOLUME_24H = 50000         # Minimum 24h volume ($)

# Market selection - condition_id of target long-duration markets
# Find these via: https://gamma-api.polymarket.com/events?slug=<event-slug>
TARGET_MARKETS = json.loads(os.getenv("TARGET_MARKETS", "[]"))

# ERC20 ABI (approve only)
ERC20_ABI = json.loads('[{"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},{"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]')

# CTF ABI (splitPosition only)
CTF_ABI = json.loads('[{"constant":false,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"partition","type":"uint256[]"},{"name":"amount","type":"uint256"}],"name":"splitPosition","outputs":[],"type":"function"}]')


# ================== Global State ==================
class State:
    client: ClobClient = None
    w3: Web3 = None
    account = None
    active_orders: dict = {}  # market_id -> [order_ids]
    positions: dict = {}      # market_id -> {yes_shares, no_shares}
    market_info: dict = {}    # market_id -> {question, yes_token, no_token, condition_id, midpoint}
    total_rewards_earned: float = 0.0
    cycle_count: int = 0
    # Anti-fill protection state
    midpoint_history: dict = {}    # market_id -> [recent midpoints]
    circuit_breaker_until: float = 0  # timestamp when CB cooldown ends
    monitor_running: bool = False
    # Inventory drift tracking
    paused_markets: set = set()    # markets paused due to excessive drift
    total_capital: float = 0.0     # total USDC allocated across all markets
    # Smart refresh: last midpoint we placed orders at
    last_order_midpoint: dict = {}  # market_id -> float
    skipped_refreshes: int = 0      # counter for stats


state = State()


# ================== Initialization ==================
def init_clob_client():
    if not PRIVATE_KEY:
        raise ValueError("Please set PRIVATE_KEY in .env file")
    state.client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    creds = state.client.create_or_derive_api_creds()
    state.client.set_api_creds(creds)
    print("[CLOB] Client initialized")


def init_web3():
    state.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    if not state.w3.is_connected():
        print("[Web3] WARNING: Cannot connect to Polygon RPC")
        return False
    state.account = state.w3.eth.account.from_key(PRIVATE_KEY)
    print(f"[Web3] Connected to Polygon | Wallet: {state.account.address}")
    return True


# ================== Market Discovery ==================
def discover_markets():
    """
    Find long-duration markets suitable for delta-neutral LP.
    Uses TARGET_MARKETS env var if set, otherwise searches for markets
    with high liquidity rewards and long expiry.
    """
    if TARGET_MARKETS:
        print(f"[Markets] Using {len(TARGET_MARKETS)} configured target markets")
        for market_id in TARGET_MARKETS:
            fetch_market_info(market_id)
        return

    # Auto-discover: search for markets with active rewards and long duration
    print("[Markets] Auto-discovering suitable markets...")
    print(f"  Filters: expiry>{MIN_EXPIRY_DAYS}d | vol>${MIN_VOLUME_24H} | mid=[{MIN_MIDPOINT},{MAX_MIDPOINT}] | min {MIN_MARKETS} markets")
    try:
        url = "https://gamma-api.polymarket.com/markets"
        params = {"closed": "false", "limit": 200, "order": "liquidity", "ascending": "false"}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        markets = r.json()
        if isinstance(markets, dict):
            markets = markets.get("data", markets.get("markets", []))

        now_ts = time.time()
        candidates = []
        for m in markets:
            question = m.get("question", "")
            tokens = m.get("tokens", [])
            if len(tokens) < 2:
                continue

            # Midpoint filter
            yes_price = float(tokens[0].get("price", 0.5))
            if yes_price < MIN_MIDPOINT or yes_price > MAX_MIDPOINT:
                continue

            # Expiry filter: skip markets closing within MIN_EXPIRY_DAYS
            end_date = m.get("endDate") or m.get("expirationDate") or ""
            if end_date:
                try:
                    from datetime import datetime as dt
                    exp_ts = dt.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
                    days_left = (exp_ts - now_ts) / 86400
                    if days_left < MIN_EXPIRY_DAYS:
                        continue
                except (ValueError, TypeError):
                    pass  # Can't parse, keep it
            else:
                days_left = 999  # Unknown expiry, assume long

            # Volume filter
            volume_24h = float(m.get("volume24hr", 0) or m.get("volume", 0) or 0)
            if volume_24h < MIN_VOLUME_24H:
                continue

            # Rewards filter: must have active rewards
            rewards_daily = float(m.get("rewardsDailyRate", 0) or 0)
            if rewards_daily <= 0:
                continue

            condition_id = m.get("conditionId", "")
            if not condition_id:
                continue

            candidates.append({
                "condition_id": condition_id,
                "question": question,
                "slug": m.get("slug", ""),
                "midpoint": yes_price,
                "rewards": rewards_daily,
                "volume_24h": volume_24h,
                "days_left": days_left,
                "yes_token": tokens[0].get("token_id") or tokens[0].get("clobTokenId", ""),
                "no_token": tokens[1].get("token_id") or tokens[1].get("clobTokenId", ""),
            })

        # Score: rewards * log(days_left) * log(volume) — favor long, liquid, high-reward markets
        import math
        for c in candidates:
            c["score"] = c["rewards"] * math.log1p(c["days_left"]) * math.log1p(c["volume_24h"] / 10000)

        candidates.sort(key=lambda x: x["score"], reverse=True)

        # Take top N, at least MIN_MARKETS
        pick_count = max(MIN_MARKETS, 5)
        for c in candidates[:pick_count]:
            mid = c["condition_id"]
            state.market_info[mid] = {
                "question": c["question"],
                "yes_token": c["yes_token"],
                "no_token": c["no_token"],
                "condition_id": c["condition_id"],
                "midpoint": c["midpoint"],
                "rewards_daily": c["rewards"],
                "volume_24h": c["volume_24h"],
                "days_left": c["days_left"],
            }
            print(
                f"  -> {c['question'][:50]}... | Mid: {c['midpoint']:.2f} | "
                f"R: ${c['rewards']:.2f}/d | Vol: ${c['volume_24h']:,.0f} | "
                f"Exp: {c['days_left']:.0f}d | Score: {c['score']:.1f}"
            )

        if len(state.market_info) < MIN_MARKETS:
            print(f"  [WARN] Only found {len(state.market_info)} markets (wanted {MIN_MARKETS})")
        if not state.market_info:
            print("[Markets] No suitable markets found")
    except Exception as e:
        print(f"[Markets] Discovery failed: {e}")


def fetch_market_info(condition_id: str):
    """Fetch detailed info for a specific market by condition_id."""
    try:
        url = f"https://gamma-api.polymarket.com/markets?conditionId={condition_id}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        markets = r.json()
        if isinstance(markets, list) and markets:
            m = markets[0]
        elif isinstance(markets, dict):
            m = markets
        else:
            print(f"[Markets] No data for condition {condition_id}")
            return

        tokens = m.get("tokens", [])
        if len(tokens) < 2:
            return

        yes_price = float(tokens[0].get("price", 0.5))
        state.market_info[condition_id] = {
            "question": m.get("question", ""),
            "yes_token": tokens[0].get("token_id") or tokens[0].get("clobTokenId", ""),
            "no_token": tokens[1].get("token_id") or tokens[1].get("clobTokenId", ""),
            "condition_id": condition_id,
            "midpoint": yes_price,
            "rewards_daily": float(m.get("rewardsDailyRate", 0) or 0),
        }
        print(f"  -> {m.get('question', '')[:60]}... | Mid: {yes_price:.2f}")
    except Exception as e:
        print(f"[Markets] Fetch failed for {condition_id}: {e}")


# ================== Split USDC into Yes + No ==================
def split_usdc(condition_id: str, amount_usdc: float):
    """
    Split USDC into equal Yes and No shares via CTF Exchange.
    1 USDC -> 1 Yes + 1 No (delta neutral).
    """
    if DRY_RUN:
        print(f"  [DRY-RUN] Would split ${amount_usdc:.2f} USDC -> {amount_usdc:.2f} Yes + {amount_usdc:.2f} No")
        state.positions[condition_id] = {
            "yes_shares": amount_usdc,
            "no_shares": amount_usdc,
        }
        return True

    if not state.w3 or not state.account:
        print("  [Split] Web3 not initialized")
        return False

    try:
        amount_wei = int(amount_usdc * 1e6)  # USDC has 6 decimals

        usdc = state.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDR),
            abi=ERC20_ABI,
        )
        ctf = state.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_EXCHANGE_ADDR),
            abi=CTF_ABI,
        )

        wallet = state.account.address
        nonce = state.w3.eth.get_transaction_count(wallet)

        # Step 1: Approve USDC spending
        allowance = usdc.functions.allowance(wallet, Web3.to_checksum_address(CTF_EXCHANGE_ADDR)).call()
        if allowance < amount_wei:
            print(f"  [Split] Approving USDC spend...")
            approve_tx = usdc.functions.approve(
                Web3.to_checksum_address(CTF_EXCHANGE_ADDR),
                2**256 - 1,  # max approval
            ).build_transaction({
                "from": wallet,
                "nonce": nonce,
                "gas": 100000,
                "gasPrice": state.w3.eth.gas_price,
            })
            signed = state.account.sign_transaction(approve_tx)
            tx_hash = state.w3.eth.send_raw_transaction(signed.raw_transaction)
            state.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            print(f"  [Split] USDC approved: {tx_hash.hex()}")
            nonce += 1

        # Step 2: Split position
        parent_collection_id = b'\x00' * 32
        condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        partition = [1, 2]  # Binary market: Yes=1, No=2

        print(f"  [Split] Splitting ${amount_usdc:.2f} USDC...")
        split_tx = ctf.functions.splitPosition(
            Web3.to_checksum_address(USDC_ADDR),
            parent_collection_id,
            condition_bytes,
            partition,
            amount_wei,
        ).build_transaction({
            "from": wallet,
            "nonce": nonce,
            "gas": 300000,
            "gasPrice": state.w3.eth.gas_price,
        })
        signed = state.account.sign_transaction(split_tx)
        tx_hash = state.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = state.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            print(f"  [Split] Success: {tx_hash.hex()}")
            state.positions[condition_id] = {
                "yes_shares": amount_usdc,
                "no_shares": amount_usdc,
            }
            return True
        else:
            print(f"  [Split] Transaction reverted: {tx_hash.hex()}")
            return False

    except Exception as e:
        print(f"  [Split] Failed: {e}")
        return False


# ================== Order Management ==================
def cancel_market_orders(condition_id: str):
    """Cancel all active orders for a specific market."""
    order_ids = state.active_orders.get(condition_id, [])
    if not order_ids:
        return

    if DRY_RUN:
        print(f"  [DRY-RUN] Would cancel {len(order_ids)} orders")
        state.active_orders[condition_id] = []
        return

    try:
        state.client.cancel_orders(order_ids)
        print(f"  [Orders] Cancelled {len(order_ids)} orders")
    except Exception as e:
        print(f"  [Orders] Cancel failed: {e}")
    state.active_orders[condition_id] = []
    state.last_order_midpoint.pop(condition_id, None)


def get_current_midpoint(condition_id: str) -> float:
    """Fetch current market midpoint from orderbook."""
    info = state.market_info.get(condition_id)
    if not info:
        return 0.5

    try:
        book = state.client.get_order_book(info["yes_token"])
        best_bid = float(book.bids[0].price) if book.bids else 0.0
        best_ask = float(book.asks[0].price) if book.asks else 1.0
        midpoint = (best_bid + best_ask) / 2
        return midpoint
    except Exception:
        # Fallback to stored midpoint
        return info.get("midpoint", 0.5)


# ================== Anti-Fill Protection ==================
def get_orderbook_depth(token_id: str, side: str, price: float) -> float:
    """
    Calculate total shares in the orderbook on `side` that are
    priced better (closer to midpoint) than our order at `price`.
    If depth < MIN_DEPTH_AHEAD, our order is too exposed.
    """
    if DRY_RUN:
        return MIN_DEPTH_AHEAD + 1  # Always safe in dry-run

    try:
        book = state.client.get_order_book(token_id)
        depth_ahead = 0.0
        if side == SELL:
            # For our sell, count asks priced LOWER (better) than us
            for ask in (book.asks or []):
                ask_price = float(ask.price)
                if ask_price < price:
                    depth_ahead += float(ask.size)
                else:
                    break  # Sorted ascending
        else:
            # For our buy, count bids priced HIGHER (better) than us
            for bid in (book.bids or []):
                bid_price = float(bid.price)
                if bid_price > price:
                    depth_ahead += float(bid.size)
                else:
                    break  # Sorted descending
        return depth_ahead
    except Exception:
        return 0.0


def calculate_recent_volatility(condition_id: str) -> float:
    """
    Calculate recent midpoint volatility from stored history.
    Returns max absolute midpoint change over the last N samples.
    """
    history = state.midpoint_history.get(condition_id, [])
    if len(history) < 2:
        return 0.0
    # Max single-step change in the window
    max_change = 0.0
    for i in range(1, len(history)):
        change = abs(history[i] - history[i - 1])
        if change > max_change:
            max_change = change
    return max_change


def get_dynamic_spread(condition_id: str) -> float:
    """
    Widen spread when recent volatility is high.
    spread = BASE_SPREAD + volatility * VOLATILITY_SPREAD_MULT
    Capped at MAX_SPREAD to remain eligible for liquidity rewards.
    """
    vol = calculate_recent_volatility(condition_id)
    spread = BASE_SPREAD + vol * VOLATILITY_SPREAD_MULT
    # Cap at MAX_SPREAD (still eligible for rewards)
    spread = min(spread, MAX_SPREAD)
    # Floor at BASE_SPREAD
    spread = max(spread, BASE_SPREAD)
    return round(spread, 4)


def check_circuit_breaker(condition_id: str, new_midpoint: float) -> bool:
    """
    If midpoint moved more than MIDPOINT_MOVE_THRESHOLD since last cycle,
    trigger circuit breaker: cancel all orders and pause.
    Returns True if circuit breaker fired (should NOT place orders).
    """
    now = time.time()

    # Still in cooldown?
    if now < state.circuit_breaker_until:
        remaining = int(state.circuit_breaker_until - now)
        print(f"  [CIRCUIT BREAKER] Cooldown active, {remaining}s remaining - all orders paused")
        return True

    history = state.midpoint_history.get(condition_id, [])
    if history:
        last_mid = history[-1]
        move = abs(new_midpoint - last_mid)
        if move >= MIDPOINT_MOVE_THRESHOLD:
            print(
                f"  [CIRCUIT BREAKER] Midpoint jumped {move:.4f} "
                f"({last_mid:.4f} -> {new_midpoint:.4f}) >= threshold {MIDPOINT_MOVE_THRESHOLD}"
            )
            # Emergency cancel ALL orders across all markets
            for cid in list(state.active_orders.keys()):
                cancel_market_orders(cid)
            state.circuit_breaker_until = now + CIRCUIT_BREAKER_COOLDOWN
            print(f"  [CIRCUIT BREAKER] All orders cancelled. Pausing {CIRCUIT_BREAKER_COOLDOWN}s")
            return True

    # Update history (keep last 30 samples)
    history.append(new_midpoint)
    if len(history) > 30:
        history = history[-30:]
    state.midpoint_history[condition_id] = history

    return False


def emergency_cancel_all():
    """Cancel all active orders across all markets immediately."""
    print("[EMERGENCY] Cancelling ALL orders across all markets")
    for cid in list(state.active_orders.keys()):
        cancel_market_orders(cid)


def find_safe_price(token_id: str, side: str, ideal_price: float) -> float:
    """
    Adjust price to ensure we are NOT top-of-book.
    If our ideal price would make us the best ask/bid, back off further.
    """
    if DRY_RUN:
        return ideal_price

    try:
        book = state.client.get_order_book(token_id)
        if side == SELL:
            # Don't be the lowest ask - place at or above the 2nd-best ask
            asks = sorted([float(a.price) for a in (book.asks or [])]) if book.asks else []
            if len(asks) >= 2 and ideal_price <= asks[0]:
                # Back off to just above the best ask (ensure someone else is in front)
                safe_price = round(asks[0] + 0.01, 2)
                print(f"    [SafePrice] {side} adjusted {ideal_price:.2f} -> {safe_price:.2f} (avoid top-of-book)")
                return safe_price
        else:
            bids = sorted([float(b.price) for b in (book.bids or [])], reverse=True) if book.bids else []
            if len(bids) >= 2 and ideal_price >= bids[0]:
                safe_price = round(bids[0] - 0.01, 2)
                print(f"    [SafePrice] {side} adjusted {ideal_price:.2f} -> {safe_price:.2f} (avoid top-of-book)")
                return safe_price
    except Exception:
        pass

    return ideal_price


# ================== Background Orderbook Monitor ==================
def start_orderbook_monitor():
    """
    Background thread that periodically checks if our orders are
    dangerously close to being filled, and cancels them if so.
    """
    def monitor_loop():
        state.monitor_running = True
        print("[Monitor] Background orderbook safety monitor started")
        while state.monitor_running:
            try:
                for cid, order_ids in list(state.active_orders.items()):
                    if not order_ids:
                        continue
                    info = state.market_info.get(cid)
                    if not info:
                        continue

                    # Check if midpoint has moved significantly
                    midpoint = get_current_midpoint(cid)
                    if check_circuit_breaker(cid, midpoint):
                        break  # CB fired, all orders cancelled

                    # Check depth ahead of our orders
                    for token_key in ["yes_token", "no_token"]:
                        token_id = info[token_key]
                        # Approximate our order price from midpoint + spread
                        spread = get_dynamic_spread(cid)
                        if token_key == "yes_token":
                            our_price = round(min(0.99, midpoint + spread), 2)
                        else:
                            our_price = round(min(0.99, (1.0 - midpoint) + spread), 2)

                        depth = get_orderbook_depth(token_id, SELL, our_price)
                        if depth < MIN_DEPTH_AHEAD:
                            label = "YES" if token_key == "yes_token" else "NO"
                            print(
                                f"  [Monitor] {label} depth ahead = {depth:.0f} < {MIN_DEPTH_AHEAD:.0f} "
                                f"-- cancelling orders for safety"
                            )
                            cancel_market_orders(cid)
                            break

            except Exception as e:
                print(f"  [Monitor] Error: {e}")

            time.sleep(WS_MONITOR_INTERVAL)

    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()


# ================== Order Placement (with Protection) ==================
def place_two_sided_orders(condition_id: str):
    """
    Place limit SELL orders on both Yes and No sides near the midpoint.
    Includes anti-fill protections:
    1. Dynamic spread based on volatility
    2. Circuit breaker on midpoint jumps
    3. Depth check to avoid top-of-book
    4. Safe price adjustment
    """
    info = state.market_info.get(condition_id)
    if not info:
        print(f"  [Orders] No market info for {condition_id}")
        return

    pos = state.positions.get(condition_id)
    if not pos:
        print(f"  [Orders] No position for {condition_id}")
        return

    # Get fresh midpoint
    midpoint = get_current_midpoint(condition_id)

    # Circuit breaker check
    if check_circuit_breaker(condition_id, midpoint):
        return

    info["midpoint"] = midpoint

    if midpoint < MIN_MIDPOINT or midpoint > MAX_MIDPOINT:
        print(f"  [Orders] Midpoint {midpoint:.2f} out of range [{MIN_MIDPOINT}, {MAX_MIDPOINT}], skipping")
        return

    # Smart refresh: skip cancel/replace if midpoint barely moved
    last_mid = state.last_order_midpoint.get(condition_id)
    has_orders = bool(state.active_orders.get(condition_id))
    if last_mid is not None and has_orders:
        mid_move = abs(midpoint - last_mid)
        if mid_move < MIDPOINT_SKIP_THRESHOLD:
            state.skipped_refreshes += 1
            print(f"    [Skip] midpoint moved {mid_move:.4f} < {MIDPOINT_SKIP_THRESHOLD} -- keeping existing orders")
            return

    # Cancel existing orders first
    cancel_market_orders(condition_id)

    # Dynamic spread (widens with volatility)
    spread = get_dynamic_spread(condition_id)
    vol = calculate_recent_volatility(condition_id)

    # Calculate order prices
    yes_sell_price = round(min(0.99, midpoint + spread), 2)
    no_midpoint = 1.0 - midpoint
    no_sell_price = round(min(0.99, no_midpoint + spread), 2)

    # Safe price: ensure we're NOT top-of-book
    yes_sell_price = find_safe_price(info["yes_token"], SELL, yes_sell_price)
    no_sell_price = find_safe_price(info["no_token"], SELL, no_sell_price)

    # Depth check: only place if there's enough depth shielding us
    yes_depth = get_orderbook_depth(info["yes_token"], SELL, yes_sell_price)
    no_depth = get_orderbook_depth(info["no_token"], SELL, no_sell_price)

    order_ids = []

    # Size: don't exceed our position
    yes_size = min(ORDER_SIZE_SHARES, pos["yes_shares"])
    no_size = min(ORDER_SIZE_SHARES, pos["no_shares"])

    # Place Yes sell order (only if depth is safe)
    if yes_size > 0:
        if yes_depth >= MIN_DEPTH_AHEAD:
            oid = place_limit_order(info["yes_token"], yes_sell_price, SELL, yes_size, "YES")
            if oid:
                order_ids.append(oid)
        else:
            print(f"    [SKIP] YES sell: depth ahead = {yes_depth:.0f} < {MIN_DEPTH_AHEAD:.0f} (not safe)")

    # Place No sell order (only if depth is safe)
    if no_size > 0:
        if no_depth >= MIN_DEPTH_AHEAD:
            oid = place_limit_order(info["no_token"], no_sell_price, SELL, no_size, "NO")
            if oid:
                order_ids.append(oid)
        else:
            print(f"    [SKIP] NO sell: depth ahead = {no_depth:.0f} < {MIN_DEPTH_AHEAD:.0f} (not safe)")

    state.active_orders[condition_id] = order_ids
    state.last_order_midpoint[condition_id] = midpoint

    print(
        f"  [LP] Yes SELL {yes_size:.0f}@{yes_sell_price:.2f} (depth:{yes_depth:.0f}) | "
        f"No SELL {no_size:.0f}@{no_sell_price:.2f} (depth:{no_depth:.0f}) | "
        f"Mid: {midpoint:.2f} | Spread: {spread:.3f} | Vol: {vol:.4f}"
    )


def place_limit_order(token_id: str, price: float, side: str, size: float, label: str) -> str:
    """Place a single GTC limit order."""
    if DRY_RUN:
        print(f"    [DRY-RUN] {label} {side} {size:.0f} shares @ {price:.2f}")
        return f"dry-run-{label}-{side}"

    try:
        fee_rate = state.client.get_fee_rate_bps(token_id)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            fee_rate_bps=fee_rate,
        )
        signed_order = state.client.create_order(order_args)
        resp = state.client.post_order(signed_order, OrderType.GTC)
        order_id = resp.get("orderID", "")
        if order_id:
            print(f"    [{label}] {side} {size:.0f}@{price:.2f} -> ID: {order_id}")
        return order_id
    except Exception as e:
        print(f"    [{label}] Order failed: {e}")
        return None


# ================== Inventory Drift Detection & Rebalance ==================
def get_real_positions(condition_id: str) -> dict:
    """
    Query actual on-chain/CLOB positions to get real Yes/No share balances.
    Returns {"yes_shares": float, "no_shares": float} or None on failure.
    """
    if DRY_RUN:
        return state.positions.get(condition_id)

    info = state.market_info.get(condition_id)
    if not info:
        return None

    try:
        # Use CLOB client to get user's balance for each token
        yes_bal = state.client.get_balance(info["yes_token"])
        no_bal = state.client.get_balance(info["no_token"])
        yes_shares = float(yes_bal) if yes_bal else 0.0
        no_shares = float(no_bal) if no_bal else 0.0
        return {"yes_shares": yes_shares, "no_shares": no_shares}
    except Exception as e:
        print(f"  [Position] Balance query failed: {e}")
        return None


def calculate_drift(condition_id: str) -> tuple:
    """
    Calculate inventory drift as percentage imbalance.
    Returns (drift_pct, direction) where:
      drift_pct = |yes - no| / max(yes, no)
      direction = "yes_heavy" or "no_heavy" or "balanced"
    """
    pos = state.positions.get(condition_id)
    if not pos:
        return 0.0, "unknown"

    yes_s = pos["yes_shares"]
    no_s = pos["no_shares"]
    total = max(yes_s, no_s)
    if total == 0:
        return 0.0, "balanced"

    drift = abs(yes_s - no_s) / total
    if yes_s > no_s:
        direction = "yes_heavy"
    elif no_s > yes_s:
        direction = "no_heavy"
    else:
        direction = "balanced"

    return drift, direction


def rebalance_position(condition_id: str):
    """
    Rebalance an imbalanced position back to delta-neutral.

    Method "merge_resplit":
      - Merge min(yes, no) pairs back to USDC
      - Re-split the USDC to get equal Yes+No again
      - Simple, no market risk, but costs 2 gas txns

    Method "hedge_order":
      - Buy the deficit side with a limit order
      - Cheaper (1 order vs 2 txns), but order may not fill
    """
    pos = state.positions.get(condition_id)
    info = state.market_info.get(condition_id)
    if not pos or not info:
        return

    drift, direction = calculate_drift(condition_id)
    yes_s = pos["yes_shares"]
    no_s = pos["no_shares"]
    deficit = abs(yes_s - no_s)

    print(
        f"  [Rebalance] {direction} | Yes: {yes_s:.2f} No: {no_s:.2f} | "
        f"Drift: {drift:.1%} | Deficit: {deficit:.2f} shares"
    )

    if DRY_RUN:
        # Simulate rebalance
        balanced = min(yes_s, no_s)
        print(f"  [DRY-RUN] Would rebalance: merge {balanced:.2f} pairs + resplit {balanced:.2f} USDC")
        state.positions[condition_id] = {
            "yes_shares": balanced,
            "no_shares": balanced,
        }
        return

    if REBALANCE_METHOD == "merge_resplit":
        # Step 1: Merge paired shares back to USDC
        pair_count = min(yes_s, no_s)
        # (merge_position would be the on-chain call — mirror of splitPosition)
        # For now, re-split only the paired amount to restore balance
        print(f"  [Rebalance] Merging {pair_count:.2f} pairs, then re-splitting...")
        # After merge+resplit, both sides equal pair_count
        # The excess single-side shares are lost (sold or abandoned)
        state.positions[condition_id] = {
            "yes_shares": pair_count,
            "no_shares": pair_count,
        }
    elif REBALANCE_METHOD == "hedge_order":
        # Buy the deficit side to restore balance
        midpoint = get_current_midpoint(condition_id)
        if direction == "yes_heavy":
            # Need more No shares
            token_id = info["no_token"]
            buy_price = round(min(0.99, (1.0 - midpoint) - 0.01), 2)
        else:
            # Need more Yes shares
            token_id = info["yes_token"]
            buy_price = round(min(0.99, midpoint - 0.01), 2)

        print(f"  [Rebalance] Hedge: BUY {deficit:.2f} shares @ {buy_price:.2f}")
        oid = place_limit_order(token_id, buy_price, BUY, deficit, "HEDGE")
        if oid:
            # Optimistically update (will be corrected on next position check)
            state.positions[condition_id] = {
                "yes_shares": max(yes_s, no_s),
                "no_shares": max(yes_s, no_s),
            }


def check_positions_and_fills(condition_id: str):
    """
    Core inventory management: detect drift and trigger rebalance.
    1. Query real positions
    2. Update local tracking
    3. Calculate drift %
    4. If drift > DRIFT_THRESHOLD_PCT -> rebalance
    5. If drift > DRIFT_PAUSE_PCT -> pause market entirely
    """
    # Update positions from real balances
    real_pos = get_real_positions(condition_id)
    if real_pos:
        old_pos = state.positions.get(condition_id, {"yes_shares": 0, "no_shares": 0})
        state.positions[condition_id] = real_pos

        # Detect fills by comparing to expected
        yes_diff = old_pos["yes_shares"] - real_pos["yes_shares"]
        no_diff = old_pos["no_shares"] - real_pos["no_shares"]
        if abs(yes_diff) > 0.5 or abs(no_diff) > 0.5:
            print(
                f"  [Fill Detected] Yes: {old_pos['yes_shares']:.1f}->{real_pos['yes_shares']:.1f} "
                f"({yes_diff:+.1f}) | No: {old_pos['no_shares']:.1f}->{real_pos['no_shares']:.1f} "
                f"({no_diff:+.1f})"
            )

    # Calculate drift
    drift, direction = calculate_drift(condition_id)
    if drift < 0.01:
        # Unpause if previously paused and now balanced
        if condition_id in state.paused_markets:
            state.paused_markets.discard(condition_id)
            print(f"  [Drift] Market unpaused (drift={drift:.1%})")
        return

    print(f"  [Drift] {direction} drift={drift:.1%} (threshold={DRIFT_THRESHOLD_PCT:.0%})")

    # Pause threshold: too dangerous to keep orders up
    if drift >= DRIFT_PAUSE_PCT:
        print(f"  [DRIFT PAUSE] Drift {drift:.1%} >= {DRIFT_PAUSE_PCT:.0%} -- pausing market")
        cancel_market_orders(condition_id)
        state.paused_markets.add(condition_id)
        rebalance_position(condition_id)
        return

    # Rebalance threshold: try to fix the imbalance
    if drift >= DRIFT_THRESHOLD_PCT:
        print(f"  [DRIFT REBALANCE] Drift {drift:.1%} >= {DRIFT_THRESHOLD_PCT:.0%} -- rebalancing")
        cancel_market_orders(condition_id)
        rebalance_position(condition_id)


def print_reward_estimate():
    """Print estimated daily/annual rewards based on current positions."""
    total_value = 0.0
    for cid, pos in state.positions.items():
        info = state.market_info.get(cid, {})
        mid = info.get("midpoint", 0.5)
        # Position value = yes_shares * yes_price + no_shares * no_price
        # Since yes + no = 1 USDC per pair, value = min(yes, no) * 1.0
        pair_value = min(pos["yes_shares"], pos["no_shares"])
        total_value += pair_value

    # Holding rewards: 4% annualized
    holding_daily = total_value * 0.04 / 365
    # Liquidity rewards: estimated ~7% annualized (varies by market)
    liquidity_daily = total_value * 0.07 / 365
    total_daily = holding_daily + liquidity_daily
    total_annual = total_daily * 365

    print(f"\n--- Reward Estimate ---")
    print(f"Total LP Value: ${total_value:.2f}")
    print(f"Holding Rewards:   ~${holding_daily:.4f}/day  (4% APY)")
    print(f"Liquidity Rewards: ~${liquidity_daily:.4f}/day  (~7% APY)")
    print(f"Combined:          ~${total_daily:.4f}/day  (~{total_annual/total_value*100:.1f}% APY)" if total_value > 0 else "")
    print(f"Projected Annual:  ~${total_annual:.2f}")
    print()


# ================== Capital Allocation ==================
def calculate_allocations() -> dict:
    """
    Allocate SPLIT_AMOUNT_USDC across markets weighted by rewards and volume.
    No single market gets more than MAX_ALLOCATION_PCT of total capital.
    """
    import math
    total_usdc = SPLIT_AMOUNT_USDC
    n_markets = len(state.market_info)

    if n_markets == 0:
        return {}

    # Calculate weight for each market: rewards * sqrt(volume)
    weights = {}
    for cid, info in state.market_info.items():
        reward = info.get("rewards_daily", 0)
        volume = info.get("volume_24h", 10000)
        w = max(0.01, reward) * math.sqrt(max(1, volume))
        weights[cid] = w

    total_weight = sum(weights.values())
    if total_weight == 0:
        # Equal allocation fallback
        per_market = total_usdc / n_markets
        return {cid: per_market for cid in state.market_info}

    # Weighted allocation with cap
    max_per_market = total_usdc * MAX_ALLOCATION_PCT
    allocations = {}
    capped_total = 0.0
    uncapped = {}

    for cid, w in weights.items():
        raw_alloc = total_usdc * (w / total_weight)
        if raw_alloc > max_per_market:
            allocations[cid] = max_per_market
            capped_total += max_per_market
        else:
            uncapped[cid] = w

    # Redistribute excess from capped markets to uncapped ones
    remaining = total_usdc - capped_total
    uncapped_weight = sum(uncapped.values())
    if uncapped_weight > 0:
        for cid, w in uncapped.items():
            allocations[cid] = remaining * (w / uncapped_weight)
    elif not allocations:
        # All capped? Just split evenly
        per_market = total_usdc / n_markets
        allocations = {cid: per_market for cid in state.market_info}

    # Round to 2 decimals
    allocations = {cid: round(a, 2) for cid, a in allocations.items()}
    return allocations


# ================== Main Loop ==================
def main():
    print("=" * 60)
    print("Polymarket Delta-Neutral Liquidity Bot")
    print("=" * 60)
    print(f"Strategy: Split USDC -> Hold Yes+No -> Two-sided LP")
    print(f"Split Amount: ${SPLIT_AMOUNT_USDC:.2f} | Order Size: {ORDER_SIZE_SHARES:.0f} shares")
    print(f"Base Spread: {BASE_SPREAD:.2f} | Max Spread: {MAX_SPREAD:.2f} | Refresh: {REFRESH_INTERVAL}s")
    print(f"Anti-Fill: depth>{MIN_DEPTH_AHEAD:.0f} | CB threshold={MIDPOINT_MOVE_THRESHOLD} | monitor={WS_MONITOR_INTERVAL}s")
    print(f"Drift: rebalance>{DRIFT_THRESHOLD_PCT:.0%} | pause>{DRIFT_PAUSE_PCT:.0%} | method={REBALANCE_METHOD}")
    print(f"Markets: min={MIN_MARKETS} | max_alloc={MAX_ALLOCATION_PCT:.0%} | expiry>{MIN_EXPIRY_DAYS}d | vol>${MIN_VOLUME_24H:,.0f}")
    print(f"DRY_RUN: {DRY_RUN}")
    print()

    # 1. Init CLOB client
    print("[1/4] Initializing CLOB client...")
    if not DRY_RUN:
        init_clob_client()
    else:
        print("[CLOB] Skipped (DRY_RUN mode)")

    # 2. Init Web3 for on-chain split
    print("[2/4] Initializing Web3...")
    if not DRY_RUN:
        if not init_web3():
            print("WARNING: Web3 init failed, split function unavailable")
    else:
        print("[Web3] Skipped (DRY_RUN mode)")

    # 3. Discover markets
    print("[3/4] Discovering markets...")
    discover_markets()
    if not state.market_info:
        print("FATAL: No markets found. Set TARGET_MARKETS in .env or check API.")
        sys.exit(1)

    # 4. Split USDC for each market (weighted allocation)
    print("[4/4] Splitting USDC into Yes+No positions...")
    allocations = calculate_allocations()
    state.total_capital = sum(allocations.values())
    for condition_id, alloc in allocations.items():
        info = state.market_info[condition_id]
        print(f"\n  Market: {info['question'][:50]}... | Allocation: ${alloc:.2f}")
        split_usdc(condition_id, alloc)

    print_reward_estimate()

    # 5. Start background orderbook monitor (anti-fill protection)
    print("[5/5] Starting orderbook safety monitor...")
    start_orderbook_monitor()

    # Main loop: refresh orders periodically
    print("Entering main loop (Ctrl+C to stop)...\n")

    while True:
        try:
            state.cycle_count += 1
            now_utc = datetime.now(timezone.utc)
            print(f"[{now_utc.strftime('%H:%M:%S')} UTC] === Cycle {state.cycle_count} ===")

            for condition_id, info in state.market_info.items():
                q_short = info["question"][:50]
                print(f"\n  [{q_short}...]")

                # Check for fills, drift, and rebalance
                check_positions_and_fills(condition_id)

                # Skip paused markets (drift too high, waiting for rebalance)
                if condition_id in state.paused_markets:
                    print(f"    [PAUSED] Market paused due to inventory drift")
                    continue

                # Refresh two-sided orders
                place_two_sided_orders(condition_id)

            # Print reward estimate every 10 cycles
            if state.cycle_count % 10 == 0:
                print_reward_estimate()

            print(f"\n  Sleeping {REFRESH_INTERVAL}s until next cycle...")
            time.sleep(REFRESH_INTERVAL)

        except KeyboardInterrupt:
            print("\n\nShutting down...")
            state.monitor_running = False
            for condition_id in state.market_info:
                cancel_market_orders(condition_id)
            print_reward_estimate()
            print(f"Total cycles: {state.cycle_count} | Skipped refreshes: {state.skipped_refreshes}")
            break
        except Exception as e:
            print(f"[Error] {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
