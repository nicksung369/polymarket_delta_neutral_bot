# Polymarket Delta-Neutral Liquidity Bot

Automated delta-neutral liquidity provision bot for Polymarket prediction markets. Earns ~11% annualized returns through holding rewards (4%) + liquidity rewards (~7%).

## Strategy

Based on [this analysis](https://x.com/degentalk_hk/status/2029465823346295157):

1. **Split USDC** -- Use Polymarket's CTF Exchange `splitPosition()` to convert USDC into equal Yes + No shares (1 USDC -> 1 Yes + 1 No)
2. **Hold for Rewards** -- Holding both sides earns 4% annualized holding rewards (sampled hourly)
3. **Provide Liquidity** -- Place sell limit orders on both Yes and No sides near the midpoint to earn liquidity rewards
4. **Stay Delta-Neutral** -- Equal Yes + No positions mean zero directional risk; profit comes purely from rewards

### How It Works

```
USDC ──> splitPosition() ──> Yes shares + No shares
                                  │           │
                                  ▼           ▼
                          SELL order      SELL order
                          (near mid)     (near mid)
                                  │           │
                                  ▼           ▼
                          Holding Rewards (4% APY)
                          + Liquidity Rewards (~7% APY)
                          = ~11% Combined APY
```

### Best Markets

This strategy works best on **long-duration markets** (e.g. 2026 FIFA World Cup, election outcomes) where:
- Rewards have time to compound
- Midpoint stays in the 10%-90% range
- Market has active liquidity reward program

## Setup

### Prerequisites

- Python 3.10+
- A Polymarket wallet with USDC (Polygon network)
- USDC approved for Polymarket exchange contracts

### Install

```bash
git clone https://github.com/nicksung369/polymarket_delta_neutral_bot.git
cd polymarket_delta_neutral_bot
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Edit `.env`:

```
PRIVATE_KEY=0xyour_private_key_here
POLYGON_RPC=https://polygon-rpc.com
TARGET_MARKETS=["0xcondition_id_1","0xcondition_id_2"]
```

### Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DRY_RUN` | `True` | Simulate only, no real orders or splits |
| `SPLIT_AMOUNT_USDC` | `100.0` | USDC to split per market |
| `SPREAD_FROM_MID` | `0.02` | Distance from midpoint for orders |
| `ORDER_SIZE_SHARES` | `50.0` | Shares per limit order |
| `REFRESH_INTERVAL` | `60` | Seconds between order refresh |
| `MAX_SPREAD` | `0.03` | Max spread for reward eligibility |
| `TARGET_MARKETS` | `[]` | Specific condition IDs (empty = auto-discover) |

### Finding Market Condition IDs

```bash
# Search by event slug
curl "https://gamma-api.polymarket.com/events?slug=fifa-world-cup-2026"

# Or browse open markets
curl "https://gamma-api.polymarket.com/markets?closed=false&limit=50"
```

## Run

```bash
python polymarket_delta_neutral_bot.py
```

**Start with `DRY_RUN = True`** to verify logic before committing real funds.

## Reward Mechanics

### Holding Rewards (4% APY)
- Applied to all eligible positions
- Sampled randomly every hour
- Paid daily

### Liquidity Rewards
- Earned by placing limit orders near the midpoint
- Quadratic scoring: closer to mid = more rewards
- Two-sided orders score 3x vs single-sided
- Sampled every minute, paid weekly

## Disclaimer

This software is for educational and research purposes only. Use at your own risk. Cryptocurrency trading involves substantial risk of loss. The authors are not responsible for any financial losses incurred from using this bot.
