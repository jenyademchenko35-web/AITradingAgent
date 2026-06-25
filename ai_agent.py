import ccxt
import pandas as pd
import requests

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

# Получаем данные с Bybit
exchange = ccxt.bybit()

ohlcv = exchange.fetch_ohlcv(
    "BTC/USDT",
    timeframe="1h",
    limit=100
)

df = pd.DataFrame(
    ohlcv,
    columns=["time", "open", "high", "low", "close", "volume"]
)

# Индикаторы
rsi = RSIIndicator(df["close"]).rsi().iloc[-1]
ema20 = EMAIndicator(df["close"], window=20).ema_indicator().iloc[-1]
ema50 = EMAIndicator(df["close"], window=50).ema_indicator().iloc[-1]
price = df["close"].iloc[-1]

# Промпт для Qwen
prompt = f"""
Ты профессиональный криптоаналитик.

Цена BTC: {price}
RSI: {rsi:.2f}
EMA20: {ema20:.2f}
EMA50: {ema50:.2f}

Правила:
- Если сигнал слабый, пиши NO TRADE
- Не придумывай данные
- Укажи вероятность от 0 до 100
- Ответ строго в формате

SIGNAL:
CONFIDENCE:
REASON:
"""

response = requests.post(
    "http://localhost:11434/api/generate",
    json={
        "model": "qwen3:14b",
        "prompt": prompt,
        "stream": False
    }
)

print("\n===== AI SIGNAL =====\n")
print(response.json()["response"])