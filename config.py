# ===============================
# Strategy Parameters
# ===============================

MIN_SCORE = 25
MIN_CONFIDENCE = 80
MIN_EDGE = 15

ATR_HIGH = 2.0
ATR_LOW = 1.0

PRICE_ZONE_LOW = 35
PRICE_ZONE_HIGH = 65

ATR_MULT = 1.5

ATR_TEST_VALUES = [
    1.0,
    1.2,
    1.4,
    1.5,
    1.6,
    1.8,
    2.0,
]


RR_TEST_VALUES = [
    1.5,
    2.0,
    2.5,
    3.0,
]

RISK_REWARD = 2.0

# ===============================
# Backtest
# ===============================

START_BALANCE = 1000.0
RISK_PER_TRADE = 0.01

# ===============================
# Market
# ===============================

TIMEFRAME = "1h"
LIMIT = 2000
START_BAR = 24 * 30

SETUP_COOLDOWN_HOURS = 6
RUN_INTERVAL = 900

# ==========================================================
# Logging
# ==========================================================
LOG_LEVEL = "NORMAL"
