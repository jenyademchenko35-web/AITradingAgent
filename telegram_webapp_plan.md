# Telegram Web App Plan

## Цель

Сделать Telegram Web App для `AITradingAgent` как визуальную read-only панель управления внутри Telegram.

Главная идея: собрать уже существующие данные проекта в удобный интерфейс, не меняя торговую стратегию, `DecisionEngine`, веса и auto-learning алгоритм.

## Принципы

1. Web App только читает существующие CSV/JSON.
2. Web App не пересчитывает торговые решения.
3. Web App не применяет веса автоматически.
4. Web App не меняет `strategy_weights.json` и `candidate_weights.json`.
5. Все рискованные действия остаются ручными и вне Web App v1.

## 1. Экраны

### Dashboard

Главный экран состояния системы.

Показывает:

- статус агента: Online / Offline
- последний цикл
- количество закрытых сделок
- Winrate
- Profit Factor
- Max Drawdown
- статус Auto-Learning
- статус Data Quality
- лучшую experiment-конфигурацию
- статус candidate weights
- текущий режим рынка
- главный blocker
- следующее рекомендуемое действие

Источники:

- `agent_v3_stats.json`
- `trades.csv`
- `auto_learning_recommendation.json`
- `data_quality_report.json`
- `strategy_experiments_report.json`
- `candidate_weights.json`
- `market_regime_report.json`
- `blocked_trade_simulation_ALL_report.json`

### Market

Экран текущего состояния рынка и последних сигналов.

Показывает:

- символ
- последний сигнал
- direction
- score
- confidence
- quality
- timestamp

Источники:

- `signals_v3.csv`
- `decision_diagnostics.csv`

### Trades

Экран сделок.

Показывает:

- открытые сделки
- закрытые сделки
- PnL
- win/loss статус
- статистику по символам
- статистику по LONG / SHORT

Источники:

- `trades.csv`

### Research

Экран исследования стратегии.

Показывает:

- всего решений
- распределение сигналов
- главный blocker
- средний lost_score
- near-setup лидеры
- слабые фильтры по символам
- ключевые рекомендации

Источники:

- `strategy_research_report.json`

### Experiments

Экран сравнения сценариев.

Показывает:

- baseline
- best scenario
- worst scenario
- comparison baseline vs best
- Winrate
- Profit Factor
- Average PnL
- Max Drawdown
- SETUP / HIGH PRIORITY count
- NO TRADE count

Источники:

- `strategy_experiments_report.json`

### Learn

Экран safe auto-learning.

Показывает:

- статус рекомендации
- confidence
- текущие боевые веса
- candidate weights
- proposed changes
- baseline vs candidate
- почему изменение не применено
- чего не хватает для approval

Источники:

- `auto_learning_recommendation.json`
- `strategy_weights.json`
- `candidate_weights.json`

### Quality

Экран качества данных.

Показывает:

- статус OK / WARNING / ERROR
- количество проблем
- топ проблем
- что нужно исправить
- валидность CSV/JSON
- валидность весов

Источники:

- `data_quality_report.json`

### Blocked

Экран blocked trade simulation.

Показывает:

- общий статус simulation
- результаты по Momentum / Structure / Risk / Trend
- количество кандидатов
- количество симуляций
- Winrate
- Profit Factor
- Average R
- best/worst symbol
- рекомендацию

Источники:

- `blocked_trade_simulation_ALL_report.json`

### Regime

Экран рыночного режима.

Показывает:

- режим по каждому символу
- volatility regime
- momentum state
- режимы: Trending Up, Trending Down, Range, High Volatility, Low Volatility

Источники:

- `market_regime_report.json`

## 2. Данные

Web App v1 читает только эти файлы:

- `agent_v3_stats.json`
- `signals_v3.csv`
- `trades.csv`
- `strategy_research_report.json`
- `strategy_experiments_report.json`
- `auto_learning_recommendation.json`
- `data_quality_report.json`
- `blocked_trade_simulation_ALL_report.json`
- `market_regime_report.json`

Дополнительно можно читать:

- `decision_diagnostics.csv`
- `strategy_weights.json`
- `candidate_weights.json`

Но только в read-only режиме.

## 3. Стек

### Backend

Использовать `FastAPI`.

Роль backend:

- читать CSV/JSON из `BASE_DIR`
- отдавать данные через HTTP API
- не выполнять торговые расчёты
- не менять файлы стратегии
- не применять веса

Пример API:

- `GET /api/dashboard`
- `GET /api/market`
- `GET /api/trades`
- `GET /api/research`
- `GET /api/experiments`
- `GET /api/learn`
- `GET /api/quality`
- `GET /api/blocked`
- `GET /api/regime`

### Frontend

Использовать простой `HTML/CSS/JS`.

Почему без тяжёлого framework на первом этапе:

- меньше зависимостей
- проще деплой
- проще обслуживать
- достаточно для read-only панели

Frontend должен:

- открываться внутри Telegram Web App
- иметь нижнюю или верхнюю навигацию по экранам
- показывать компактные карточки и таблицы
- обновлять данные по кнопке Refresh
- корректно работать на мобильном экране

### Telegram Integration

В `telegram_bot_v4.py` добавить кнопку Telegram WebApp.

Возможный вариант:

- inline-кнопка `📊 Web Dashboard`
- открывает URL Web App

Для Telegram Web App нужен публичный HTTPS URL. Для локальной разработки можно использовать временный tunnel, но в production лучше иметь постоянный домен.

## 4. Что НЕ делать

Запрещено в рамках Telegram Web App v1:

1. Не менять `DecisionEngine`.
2. Не менять торговую логику.
3. Не менять `strategy_weights.json`.
4. Не менять `candidate_weights.json`.
5. Не применять веса автоматически.
6. Не добавлять кнопки Apply / Approve / Trade.
7. Не запускать live orders.
8. Не пересчитывать сигналы внутри Web App.
9. Не использовать Web App как источник торговых решений.

Web App v1 должен быть только визуальной панелью наблюдения.

## 5. Архитектура

Предлагаемая структура:

```text
webapp/
  app.py
  static/
    index.html
    styles.css
    app.js
```

`webapp/app.py`:

- FastAPI server
- helper для чтения CSV
- helper для чтения JSON
- API endpoints

`webapp/static/index.html`:

- главный HTML
- подключение Telegram WebApp JS SDK
- контейнеры экранов

`webapp/static/styles.css`:

- мобильная сетка
- карточки
- таблицы
- статусы OK / WARNING / ERROR

`webapp/static/app.js`:

- загрузка API
- переключение экранов
- форматирование данных
- refresh

## 6. Безопасность

Минимальные правила:

1. Backend должен открывать только известные файлы из `BASE_DIR`.
2. Никаких произвольных путей из query params.
3. Все endpoints read-only.
4. В ответах не отдавать секреты из `.env`.
5. Не показывать `BOT_TOKEN`, API keys, chat_id.
6. Если файл отсутствует, возвращать понятный статус `missing`, а не stack trace.

## 7. План реализации

### Этап 1. Read-only backend

Создать FastAPI backend:

- `/api/dashboard`
- `/api/market`
- `/api/trades`
- `/api/research`
- `/api/experiments`
- `/api/learn`
- `/api/quality`
- `/api/blocked`
- `/api/regime`

Проверить, что все endpoints читают существующие файлы и не меняют данные.

### Этап 2. Простой frontend

Создать мобильный интерфейс:

- Dashboard как стартовый экран
- навигация по разделам
- compact cards
- таблицы для Market и Trades
- цветовые статусы

### Этап 3. Telegram WebApp button

В `telegram_bot_v4.py` добавить кнопку:

- `📊 Web Dashboard`

Кнопка открывает Web App URL.

Команды бота остаются рабочими.

### Этап 4. Runtime check

Проверить:

- Web App открывается в Telegram
- Dashboard грузится
- все разделы открываются
- отсутствующие файлы отображаются спокойно
- бот продолжает работать как раньше

## 8. Production notes

Для постоянной работы нужен HTTPS endpoint.

Варианты:

1. VPS + nginx + uvicorn/gunicorn.
2. Railway / Render / Fly.io.
3. Локальный запуск + tunnel только для тестов.

Команда локального запуска может быть такой:

```bash
venv/bin/python -m uvicorn webapp.app:app --host 0.0.0.0 --port 8000
```

## 9. Итог

Telegram Web App должен стать визуальной панелью управления поверх уже существующей аналитики.

Он не заменяет агента, не меняет стратегию и не принимает торговые решения. Его задача — сделать данные понятными, быстрыми и удобными для просмотра внутри Telegram.
