# AITradingAgent Architecture Audit

## Цель

Этот аудит оценивает текущее состояние `AITradingAgent` без изменения кода, торговой логики, весов и `DecisionEngine`.

Фокус: найти реальные слабые места системы, технический долг, риски сопровождения и безопасные улучшения, которые повысят надёжность проекта.

## Краткий вывод

Проект находится в хорошем рабочем состоянии. В нём уже есть полноценный цикл:

- сбор рыночных данных;
- расчёт Trend / Structure / Momentum / Risk;
- DecisionEngine;
- логирование signals/debug/diagnostics/explanations;
- Telegram-интерфейс;
- Research / Experiments / Auto-Learning;
- Data Quality;
- Filter Effectiveness;
- Blocked Trade Simulation;
- Market Regime;
- Post Trade Analysis;
- AI Coach.

Главный риск сейчас не в отсутствии функций, а в сложности системы вокруг ядра: много CSV/JSON, много аналитических отчётов, много on-demand пересборок через Telegram. Это уже больше похоже на маленькую аналитическую платформу, поэтому критичны дисциплина данных, свежесть отчётов и понятные границы между live-логикой и research.

## Сильные стороны

### 1. Ядро принятия решений прозрачное

`DecisionEngine` прост и читаем:

- считает weighted long/short totals;
- использует `MIN_EDGE`;
- классифицирует сигналы в `HIGH PRIORITY / SETUP / WATCH / WAIT / NO TRADE`;
- не содержит скрытого auto-trading поведения.

Это хорошо для сопровождения и аудита.

### 2. Торговая логика отделена от аналитики

Большинство новых модулей работают как read-only слой:

- `strategy_research.py`
- `strategy_experiments.py`
- `auto_learning.py`
- `data_quality_check.py`
- `filter_effectiveness.py`
- `blocked_trade_simulator.py`
- `post_trade_analysis.py`
- `ai_coach.py`

Это правильная архитектурная граница: аналитика развивается, но live-решения не меняются автоматически.

### 3. Safe Auto-Learning действительно защищён

`auto_learning.py` не применяет веса автоматически и блокирует рекомендацию, если:

- закрытых сделок меньше 30;
- PF improvement меньше 0.10;
- winrate хуже baseline;
- изменение одного веса больше 0.05;
- сумма candidate-весов не равна 1.0;
- candidate не отличается от live weights.

Это хороший safety layer.

### 4. Telegram стал полноценным интерфейсом

Бот уже показывает:

- Dashboard;
- Coach;
- Market;
- Trades;
- PostTrade;
- Research;
- Experiments;
- Candidate;
- Learn;
- Quality;
- Filters;
- Blocked;
- Regime;
- History;
- Report.

Пользователю почти не нужен терминал для наблюдения за системой.

### 5. Data Quality уже закрывает часть операционных рисков

`data_quality_check.py` проверяет:

- наличие файлов;
- пустые файлы;
- схемы CSV;
- битые строки;
- пропуски важных колонок;
- некорректные signal/direction/quality;
- score/confidence;
- дубли;
- валидность сумм weights.

Это важный фундамент для дальнейшего self-learning.

## Найденные слабые места

## 1. DecisionEngine и фильтры

### A-01. Дополнительный higher-timeframe filter находится вне DecisionEngine

Критичность: Medium

В `analyze_symbol()` после расчёта `DecisionEngine` есть дополнительный фильтр:

- LONG отклоняется, если 4h/1d не bullish;
- SHORT отклоняется, если 4h/1d не bearish.

Это не ошибка, но архитектурно важно: `DecisionEngine` может выдать `SETUP`, а фактическая сделка всё равно не откроется из-за внешнего фильтра. В логах это может выглядеть как “SETUP был, сделки нет”.

Рекомендация:

- сейчас не менять;
- явно логировать причину rejection в отдельное поле или CSV в будущей версии;
- добавить это в diagnostics как отдельный blocker `HigherTF`.

Когда исправлять:

- v2.0, после накопления статистики.

### A-02. RiskEngine может давать отрицательные значения обеим сторонам

Критичность: Low / Medium

При высокой волатильности или середине диапазона Risk уменьшает обе стороны. Это логично, но делает итоговый score ниже и может усиливать частоту `NO TRADE`.

Сейчас это не надо менять, потому что PostTrade уже показывает `bad Risk` как частую проблему LOSS, а experiments показали, что усиление Risk ухудшало baseline.

Рекомендация:

- не менять Risk сейчас;
- продолжать PostTrade и Filter Effectiveness;
- отдельно исследовать Risk только через backtest.

Когда исправлять:

- только после 30-100 закрытых сделок.

### A-03. Momentum часто блокирует, но ослаблять его нельзя

Критичность: Medium

Filter Effectiveness и diagnostics показывают, что Momentum часто является главным blocker. Но blocked simulator по Momentum показал плохой результат для blocked trades.

Рекомендация:

- не ослаблять Momentum вручную;
- продолжать использовать blocked simulation как защиту от ложных выводов.

Когда исправлять:

- не сейчас.

### A-04. Возможны условия, которые встречаются редко

Критичность: Low

Сигналы `HIGH PRIORITY` требуют одновременно высокого score и confidence. При текущем `MIN_EDGE`, `MIN_CONFIDENCE` и весах это может быть редким событием, особенно в Range рынке.

Это не баг, а следствие консервативной стратегии.

Рекомендация:

- не менять пороги;
- отслеживать частоту `WATCH / SETUP / HIGH PRIORITY` на расширенном списке из 9 символов.

## 2. Статистика и файлы

### B-01. Очень много CSV/JSON артефактов

Критичность: Medium

Проект уже хранит много файлов:

- live logs;
- debug logs;
- diagnostics;
- explanations;
- research reports;
- experiments reports;
- candidate weights;
- auto-learning;
- blocked simulation;
- market regime;
- post-trade;
- coach;
- backups;
- legacy files.

Это полезно, но растёт риск:

- непонятно, какой файл canonical;
- отчёты могут устареть;
- Telegram может показывать старые данные;
- Data Quality может не проверять новые файлы.

Рекомендация:

- добавить единый registry артефактов в будущем;
- расширить Data Quality на новые файлы: `post_trade_analysis_*`, `ai_coach_*`, `market_regime_report.json`, `filter_effectiveness_report.json`, `blocked_trade_simulation_ALL_report.json`;
- в RUNBOOK добавить список актуальных v1.0/v1.1 артефактов.

Что можно исправить сейчас:

- обновить Data Quality coverage без изменения стратегии.

### B-02. Есть legacy и старые файлы, которые могут путать

Критичность: Low / Medium

Есть:

- `signals.csv`
- `setup_history.csv`
- `agent_stats.json`
- `active_setups.json`
- legacy folder;
- old learning/performance/calibration modules.

Часть уже перенесена в `legacy`, но рядом с v3 всё ещё лежат похожие старые файлы.

Рекомендация:

- не удалять;
- добавить `README` или раздел в RUNBOOK: какие файлы актуальны, какие legacy;
- в Telegram и analytics использовать только v3-файлы.

### B-03. Часть аналитики опирается на эвристическую привязку строк

Критичность: Medium

Например:

- PostTrade ищет ближайший signal/debug перед открытием сделки;
- Filter Effectiveness пытается сопоставлять blocked decision с будущими trade outcomes;
- blocked simulator использует OHLCV и synthetic SL/TP simulation.

Это нормально для research, но не стоит считать такие выводы равными live-доказательствам.

Рекомендация:

- в отчётах явно показывать `matched_signal_time`, `signal_age_minutes`, `matched_debug_time`;
- для Filter Effectiveness добавить confidence/coverage: сколько строк реально matched.

Что можно исправить сейчас:

- добавить coverage-поля в отчёты без изменения логики.

## 3. Производительность

### C-01. Telegram часто читает CSV/JSON заново

Критичность: Medium

Многие команды в `telegram_bot_v4.py` каждый раз читают файлы:

- `signals_v3.csv`;
- `decision_debug.csv`;
- `decision_diagnostics.csv`;
- `trades.csv`;
- JSON reports.

При текущем размере это терпимо, но по мере роста логов Telegram станет медленнее.

Рекомендация:

- добавить простой read cache по `mtime`;
- кэшировать CSV rows на 5-30 секунд;
- не пересчитывать тяжёлые отчёты чаще, чем изменились исходные файлы.

Что можно исправить сейчас:

- read cache в Telegram helper functions.

### C-02. On-demand subprocess из Telegram может блокировать ответ

Критичность: Medium

Команды Telegram запускают отчёты через `subprocess.run()`:

- experiments;
- auto-learning;
- data quality;
- filters;
- blocked;
- regime;
- posttrade;
- coach.

Если отчёт тяжёлый или сеть недоступна, Telegram handler может отвечать долго.

Рекомендация:

- для тяжёлых отчётов показывать “отчёт устарел / обновить вручную”;
- вынести heavy rebuild в отдельный scheduled job;
- для Telegram оставить чтение готовых артефактов.

Что можно исправить сейчас:

- добавить timeout для subprocess;
- добавить более понятный текст при timeout.

### C-03. Расширение до 9 символов увеличит нагрузку на Bybit

Критичность: Medium

Каждый символ грузит 1h/4h/1d OHLCV. При 9 символах это уже 27 запросов на цикл плюс retry.

Рекомендация:

- следить за `api_errors`;
- сохранить interval не ниже 300 секунд;
- позже добавить OHLCV cache per symbol/timeframe на один цикл или между циклами.

Что можно исправить сейчас:

- ничего срочного, уже есть retry и in-memory cache fallback.

## 4. Архитектура

### D-01. `multi_timeframe_agent_v3.py` слишком большой

Критичность: Medium

В одном файле находятся:

- config loading;
- exchange;
- storage helpers;
- engine classes;
- DecisionEngine;
- notification logic;
- trade opening;
- CLI loop.

Это работает, но повышает риск случайно задеть стратегию при обслуживании инфраструктуры.

Рекомендация:

- сейчас не рефакторить агрессивно;
- в v2 разделить на:
  - `engines.py`
  - `decision_engine.py`
  - `market_loader.py`
  - `agent_runner.py`
  - `storage.py`

Когда исправлять:

- v2.0, после стабилизации статистики.

### D-02. Повторяются helper-функции чтения CSV/JSON

Критичность: Low / Medium

Почти каждый аналитический модуль имеет свои:

- `read_csv_rows`;
- `read_json`;
- `safe_float`;
- `parse_time`.

Это безопасно, но ведёт к расхождениям поведения.

Рекомендация:

- создать `data_utils.py`;
- переносить постепенно;
- не менять расчётную логику при переносе.

Что можно исправить сейчас:

- пока оставить, если нет времени на regression check.

### D-03. Telegram-бот стал большим фасадом

Критичность: Medium

`telegram_bot_v4.py` уже содержит много форматтеров, ensure-functions и command handlers. Это удобно в одном файле, но сопровождать будет сложнее.

Рекомендация:

- в v2 вынести:
  - `telegram_formatters.py`
  - `telegram_commands.py`
  - `telegram_keyboards.py`
  - `report_runner.py`

Что можно исправить сейчас:

- не обязательно; текущий файл ещё работоспособен.

## 5. Надёжность

### E-01. Повреждённый CSV может частично ломать аналитику

Критичность: Medium

Data Quality умеет находить битые строки, но большинство аналитических модулей просто читают CSV через `csv.DictReader`. Если файл сильно повреждён, отчёт может быть неполным или молча неверным.

Рекомендация:

- перед важной аналитикой проверять `data_quality_report.json`;
- в heavy reports добавлять предупреждение, если Data Quality не OK;
- сохранять backup перед repair.

Что можно исправить сейчас:

- добавить в Coach/Dashboard предупреждение при `data_quality != OK`.

### E-02. Отсутствие сети влияет на market/regime/blocked cache

Критичность: Medium

Агент уже имеет retry и OHLCV cache в рамках процесса. Blocked simulator имеет WAITING_FOR_DATA fallback. Это хорошо.

Риск остаётся:

- market_regime может не построиться;
- новый расширенный universe увеличивает вероятность API error;
- Telegram может показать stale report.

Рекомендация:

- показывать время генерации отчёта в Telegram;
- не считать stale data ошибкой стратегии.

### E-03. Падение одного аналитического модуля не должно ломать Telegram

Критичность: Medium

Сейчас ensure-functions в Telegram обычно возвращают текст ошибки. Это хорошо.

Риск:

- тяжёлый subprocess может зависнуть;
- stack trace может быть длинным;
- пользователь видит технический текст.

Рекомендация:

- добавить timeout;
- унифицировать ошибки “отчёт временно недоступен”.

## 6. Safe Auto-Learning

### F-01. Auto-Learning защищён от auto-apply

Критичность: Positive

Текущая логика корректно держит статус `NOT_ENOUGH_DATA` при 19 закрытых сделках и не применяет candidate.

### F-02. Candidate сравнивается с best experiment, а не обязательно с candidate file metadata

Критичность: Low / Medium

`auto_learning.py` берёт `best_scenario` из experiments и candidate weights из `candidate_weights.json`. Если эти файлы рассинхронизируются, рекомендация может стать логически неоднозначной.

Рекомендация:

- проверять, что `candidate_weights.metadata.selected_scenario == experiments.best_scenario.scenario`;
- если нет, ставить `REJECTED_BY_RULES`.

Что можно исправить сейчас:

- безопасная проверка consistency без изменения стратегии.

### F-03. Риск ложных рекомендаций остаётся из-за малого числа сделок

Критичность: Medium

Даже при хороших правилах sample size мал. 19 сделок недостаточно для применения изменений.

Рекомендация:

- оставить правило 30+ минимум;
- лучше целиться в 50-100 сделок перед v2 изменениями.

## 7. Telegram Bot UX

### G-01. Бот функционально богатый, но перегружен командами

Критичность: Low / Medium

Команд много. Dashboard и Coach помогают, но пользователь может не понимать, куда идти за ответом.

Рекомендация:

- оставить `/start` как Dashboard;
- `/coach` использовать как ежедневный вывод;
- в `/help` оставить команды группами;
- не добавлять новые команды без сильной причины.

### G-02. Нет явного возраста данных на всех экранах

Критичность: Medium

Некоторые отчёты имеют `generated_at`, но не все Telegram-ответы его показывают. Пользователь может не понимать, свежий отчёт или нет.

Рекомендация:

- добавить `generated_at` в Dashboard/Coach/Research/Experiments/PostTrade/Quality;
- для stale reports показывать мягкое предупреждение.

Что можно исправить сейчас:

- UI-only improvement.

### G-03. Callback/command coverage хорошее, но требует runtime smoke test после крупных правок

Критичность: Low

После добавления Coach/PostTrade/Dashboard желательно проверить:

- `/start`
- `/dashboard`
- `/coach`
- `/posttrade`
- `/quality`
- `/blocked ALL`
- `/regime`
- inline-кнопки.

Рекомендация:

- добавить чеклист в RUNBOOK.

## 8. Что можно исправить сейчас

Без изменения стратегии и DecisionEngine можно безопасно сделать:

1. Расширить `data_quality_check.py` на новые артефакты:
   - `post_trade_analysis_report.json`
   - `ai_coach_report.json`
   - `market_regime_report.json`
   - `filter_effectiveness_report.json`
   - `blocked_trade_simulation_ALL_report.json`

2. Добавить timeout к `subprocess.run()` в Telegram ensure-functions.

3. Добавить `generated_at` / “обновлено” в ключевые Telegram-экраны.

4. Добавить mtime cache для чтения CSV/JSON в Telegram.

5. Добавить consistency check в `auto_learning.py`:
   - candidate scenario должен совпадать с experiments best scenario.

6. Добавить в RUNBOOK актуальный список команд:
   - `/dashboard`
   - `/coach`
   - `/posttrade`
   - `/blocked ALL`
   - `/regime`

7. Добавить документ “artifact map”:
   - какие файлы live;
   - какие research;
   - какие legacy;
   - какие generated.

## 9. Что лучше оставить до v2.0

1. Разделение `multi_timeframe_agent_v3.py` на модули.
2. Перенос `DecisionEngine` в отдельный файл.
3. Добавление нового blocker `HigherTF`.
4. Изменение Risk/Momentum/Structure логики.
5. Изменение порогов score/confidence.
6. Symbol-specific weights для новых 9 монет.
7. Regime-aware trading logic.
8. Любое auto-apply candidate weights.

## 10. Приоритеты

### High

1. Не менять DecisionEngine до 30+ закрытых сделок.
2. Следить за Data Quality после расширения до 9 монет.
3. Не применять candidate weights автоматически.
4. Не ослаблять Momentum без новых blocked-trade доказательств.

### Medium

1. Добавить freshness/generation time в Telegram.
2. Добавить timeout для Telegram report builders.
3. Расширить Data Quality на новые отчёты.
4. Добавить consistency check для candidate/experiments.

### Low

1. Убрать дубли helper-функций в `data_utils.py`.
2. Разнести Telegram formatters по отдельным файлам.
3. Документировать legacy/live artifacts.

## Итоговая оценка

Проект уже достаточно зрелый для длительного наблюдения в v1.x режиме. Главная задача сейчас — не добавлять всё новые аналитические слои, а обеспечить:

- чистоту данных;
- свежесть отчётов;
- понятную границу live/research;
- устойчивость Telegram-интерфейса;
- осторожность в выводах до накопления 30-100 закрытых сделок.

Текущий DecisionEngine не выглядит сломанным. Он выглядит консервативным. Это важно различать: отсутствие сделок в Range-рынке не является само по себе багом.
