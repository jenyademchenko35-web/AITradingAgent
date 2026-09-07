# AITradingAgent v1.0 Runbook

## Назначение

Этот runbook описывает, как запускать и обслуживать актуальную версию проекта
`AITradingAgent v1.0`.

Основная рабочая ветка проекта:

- `multi_timeframe_agent_v3.py`
- `telegram_bot_v4.py`
- `decision_diagnostics.py`
- `explainable_ai.py`
- `calibration_report.py`

Все команды ниже нужно запускать только через проектное виртуальное окружение
`venv`.


## Правильный Порядок Запуска

После завершения controlled migration основной Agent управляется только systemd:

```bash
sudo systemctl start aitrading-agent.service
systemctl status aitrading-agent.service --no-pager
```

Не запускать основной Agent через `nohup`, `update.sh`, legacy watchdog или
ручной Python command. До завершения migration существующий session-scope Agent
остаётся работающим и не должен останавливаться обычным code update.

Telegram-бот запускается своим отдельным supervisor:

```bash
venv/bin/python telegram_bot_v4.py
```

Не использовать системный `python3` вместо `venv/bin/python`.

### Controlled session → systemd handoff

Migration выполняется отдельной операцией, а не из `update.sh`:

1. Проверить production health, один Agent, working DB/WAL, H2 outbox и Research.
2. Развернуть reviewed code и `deploy/systemd/aitrading-agent.service` без restart.
3. Скопировать unit в `/etc/systemd/system/aitrading-agent.service` вручную с sudo.
4. Выполнить `systemd-analyze verify` и `sudo systemctl daemon-reload`.
5. **DO NOT start** systemd unit, пока session Agent жив.
6. Дождаться нового `agent_loop_sleep_start` в текущем Agent log.
7. Проверить exact PID/cmdline/cwd и отправить `SIGTERM` только этому session PID.
8. Дождаться clean exit; не использовать SIGKILL как normal path.
9. Проверить отсутствие Agent и что canonical singleton lock свободен.
10. Запустить `sudo systemctl start aitrading-agent.service`.
11. Подтвердить ровно один Agent, его systemd cgroup и удерживаемый singleton.
12. Дождаться first full cycle: `database_status=OK`,
    `runtime_snapshot_published`, `Cycle duration`.
13. Проверить DB quick-check/WAL, H2 outbox, Research и H9 boundary.
14. Подтвердить `systemctl is-enabled aitrading-agent.service`.

### Rollback systemd Agent

Rollback не сбрасывает код или runtime state:

1. `sudo systemctl stop aitrading-agent.service`.
2. Проверить, что systemd PID исчез и singleton lock свободен.
3. Отключить unit от boot, прежде чем возвращать session launcher.
4. Из `/home/aitrading/AITradingAgent` запустить ровно один canonical session Agent:

```bash
nohup venv/bin/python -u multi_timeframe_agent_v3.py --loop --interval 300 \
  >> logs/agent_v3.log 2>&1 &
```

5. Проверить один PID, правильный cwd, singleton holder и first full cycle.
6. Не удалять и не пересоздавать DB, outbox, cooldown или trading state.

### Singleton основного агента

Canonical systemd launcher и controlled rollback command используют один lifetime
`flock`: `<passwd-home>/.local/state/AITradingAgent/agent.lock`. Passwd home,
а не переменная `HOME`, гарантирует один namespace для systemd и controlled rollback.
Блокировка берётся до
инициализации trading/outbox компонентов и удерживается открытым file descriptor
до завершения процесса. Второй запуск немедленно завершается с кодом `73`; наличие
старого lock-файла без живого владельца запуску не мешает. Не удаляйте lock-файл
для управления процессом — проверяйте владельца блокировки или список процессов.


## Agent supervision и logging

Canonical command закреплён в version-controlled systemd unit:

```bash
systemctl cat aitrading-agent.service
```

Agent stdout/stderr под systemd поступают в journald:

```bash
journalctl -u aitrading-agent.service -n 200 --no-pager
```

`logs/agent_v3.log` — diagnostic-only legacy session log. `logs/agent.log` —
legacy-unused для Agent после migration. Runtime health определяется по systemd,
`runtime_snapshot.json`, завершённым циклам и DB health, а не по freshness этих
file logs. Legacy watchdog основной Agent больше не контролирует.

Agent:

- запускает основной v3-агент;
- анализирует рынок по циклу;
- пишет сигналы, diagnostics, explanations и статистику;
- обновляет CSV/JSON, которые использует Telegram.


## Запуск Telegram-Бота

Основная команда:

```bash
venv/bin/python telegram_bot_v4.py
```

Бот является основным пользовательским интерфейсом v1.0 и показывает:

- статус агента;
- рынок;
- watchlist;
- diagnostics;
- статистику;
- сделки;
- calibration;
- историю;
- дневной отчёт.


## Как Остановить Лишние Экземпляры Бота

Если Telegram сообщает `Conflict`, значит уже запущен другой экземпляр бота.

Остановить лишние процессы:

```bash
pkill -f telegram_bot_v4.py
```

После этого снова запустить один экземпляр:

```bash
venv/bin/python telegram_bot_v4.py
```


## Проверка Компиляции

Минимальная проверка ключевых модулей:

```bash
venv/bin/python -B -m py_compile \
  multi_timeframe_agent_v3.py \
  telegram_bot_v4.py \
  decision_diagnostics.py \
  explainable_ai.py \
  calibration_report.py \
  backtest.py \
  trade_tracker.py \
  notification_manager.py \
  optimizer.py
```

Если команда завершилась без вывода ошибок, синтаксис в порядке.


## Запуск Служебных Скриптов

### Calibration Report

```bash
venv/bin/python calibration_report.py
```

Что делает:

- читает `decision_diagnostics.csv`;
- строит `calibration_report.json`;
- печатает рекомендации в терминал.

### Backtest

```bash
venv/bin/python backtest.py
```

При необходимости с параметрами:

```bash
venv/bin/python backtest.py --atr 1.5 --rr 2.0
```

### Optimizer

```bash
venv/bin/python optimizer.py
```

Что делает:

- запускает серии backtest;
- пишет результаты в `optimizer_results.csv`.


## Главные Файлы Проекта

Ключевые рабочие файлы v1.0:

- `signals_v3.csv` — последние сигналы агента
- `decision_debug.csv` — подробный debug по engine scores
- `decision_diagnostics.csv` — PASS/FAIL, blockers, lost score
- `trades.csv` — открытые и закрытые сделки
- `agent_v3_stats.json` — статистика циклов агента
- `calibration_report.json` — отчёт по калибровке
- `strategy_weights.json` — веса движков

Также используются:

- `decision_explanations.csv`
- `setup_history_v3.csv`
- `active_setups_v3.json`
- `bot_config.json`
- `last_notification.json`


## Что Не Трогать

Без отдельного анализа и задачи не менять:

- `DecisionEngine`
- торговые пороги
- математическую модель оценки сигналов
- `strategy_weights.json` вручную, без анализа diagnostics/calibration

Если нужна настройка поведения системы, сначала смотреть:

- `decision_diagnostics.csv`
- `calibration_report.json`
- `decision_debug.csv`


## Runtime-Проверка Telegram

Основные команды в Telegram:

- `/start`
- `/help`
- `/status`
- `/market`
- `/watchlist`
- `/diagnostics BTC`
- `/stats`
- `/trades`
- `/calibration`
- `/history`
- `/report`

Если бот запущен корректно, вся основная информация доступна через Telegram без
необходимости постоянно открывать терминал.


## Troubleshooting

### Telegram Conflict

Симптом:

- бот пишет, что другой экземпляр уже использует `getUpdates`

Решение:

```bash
pkill -f telegram_bot_v4.py
venv/bin/python telegram_bot_v4.py
```


### ccxt not found

Симптом:

- ошибка `ModuleNotFoundError: No module named 'ccxt'`

Причина:

- проект запущен не через `venv`

Решение:

```bash
systemctl status aitrading-agent.service --no-pager
```

или

```bash
venv/bin/python backtest.py
```


### Пустые CSV

Симптом:

- `signals_v3.csv`, `decision_diagnostics.csv` или другие файлы пустые

Причина:

- агент ещё не запускался;
- цикл не успел завершиться;
- есть ошибка получения данных с биржи

Решение:

1. Проверить `systemctl status aitrading-agent.service --no-pager`.
2. Дождаться завершения хотя бы одного цикла.
3. Проверить `journalctl -u aitrading-agent.service -n 200 --no-pager`.


### Нет BOT_TOKEN

Симптом:

- Telegram-бот не стартует;
- сообщение о том, что `BOT_TOKEN` не найден

Решение:

Проверить файл `.env` в корне проекта. В нём должен быть токен бота.

Пример:

```env
BOT_TOKEN=your_telegram_token
```


### Нет chat_id

Симптом:

- бот не может отправлять уведомления в чат

Решение:

1. Запустить Telegram-бот.
2. Написать боту `/start`.
3. Убедиться, что обновился файл `bot_config.json`.


## Legacy-Файлы

Старые скрипты сохранены в папке `legacy/`.

Их не нужно использовать для текущей v1.0-системы.

Актуальные entry points:

- `multi_timeframe_agent_v3.py`
- `telegram_bot_v4.py`


## Релизная Модель Работы

Для нормальной эксплуатации v1.0:

1. Запустить агент через `venv`.
2. Запустить Telegram-бот через `venv`.
3. Следить за системой в Telegram.
4. Использовать `calibration_report.py` и `backtest.py` отдельно, когда нужен
   анализ или настройка.
