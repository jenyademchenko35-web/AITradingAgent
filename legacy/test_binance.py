import ccxt
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

exchange = ccxt.bybit()

ohlcv = exchange.fetch_ohlcv(
    'BTC/USDT',
    timeframe='1h',
    limit=100
)

df = pd.DataFrame(
    ohlcv,
    columns=['time', 'open', 'high', 'low', 'close', 'volume']
)

df['rsi'] = RSIIndicator(df['close']).rsi()

df['ema20'] = EMAIndicator(df['close'], window=20).ema_indicator()
df['ema50'] = EMAIndicator(df['close'], window=50).ema_indicator()

print("\nПоследняя свеча:\n")

print(
    f"""
Цена: {df['close'].iloc[-1]}
RSI: {round(df['rsi'].iloc[-1],2)}
EMA20: {round(df['ema20'].iloc[-1],2)}
EMA50: {round(df['ema50'].iloc[-1],2)}
"""
)
