# Codex agents

Каталог `.codex/agents/` содержит project-scoped роли Codex в современном standalone TOML-формате. Каждый файл задает обязательные поля `name`, `description` и `developer_instructions`. Имя файла совпадает с `name`, чтобы роль было проще находить и вызывать.

## Торговые и операционные роли

- `ai-trading-project-guardian` — проверяет безопасность и архитектурные границы; защищает LIVE, DecisionEngine и config.py от изменений без явного разрешения.
- `backtest-scientist` — занимается только replay, backtest, walk-forward и статистической проверкой результатов.
- `risk-calibration-engineer` — анализирует Risk, Position Size, Exposure и Drawdown, не меняя Entry/Exit.
- `strategy-researcher` — проверяет гипотезы только в Shadow Research и не переносит рекомендации напрямую в LIVE.
- `trading-data-scientist` — отвечает за аналитику, метрики, статистику, качество данных и отчеты.
- `telegram-bot-maintainer` — поддерживает только Telegram-интерфейс и не изменяет торговую логику.
- `server-operator` — отвечает за запуск, процессы, scheduler, логи, health и мониторинг.

## Общие инженерные роли

- `code-reviewer` — ревью корректности, сопровождаемости и рисков реализации.
- `debugger` — локализация причин ошибок и построение минимального воспроизводимого сценария.
- `devops-engineer` — CI/CD, release automation и конфигурация окружений.
- `git-workflow-manager` — ветки, merge flow, release branching и командные Git-процессы.
- `python-pro` — Python runtime, packaging, typing, testing и реализация на Python.
- `quant-analyst` — количественный анализ моделей, симуляций и числовой логики.
- `risk-manager` — общий анализ продуктовых, операционных, финансовых и архитектурных рисков.

## Общие ограничения торговых ролей

Все специализированные торговые роли следуют принципам Shadow First и Research First, предпочитают анализ предположениям, объясняют reasoning, не меняют LIVE автоматически и не изменяют торговую логику без явного разрешения пользователя.

## Примеры использования

В запросе к Codex явно укажите нужную роль и задачу, например:

```text
Используй Strategy Researcher, чтобы сформулировать read-only проверку гипотезы по replay-данным.
Используй Backtest Scientist, чтобы сравнить walk-forward результат с baseline.
Используй Trading Data Scientist, чтобы проверить freshness, sample size и качество trades.csv.
Используй Server Operator, чтобы диагностировать health процесса и scheduler без изменения LIVE.
Используй Project Guardian, чтобы оценить, затрагивает ли предложенный diff DecisionEngine или config.py.
```

`Project Guardian` в последнем примере означает роль `ai-trading-project-guardian`. Codex также может подобрать роль по ее `description`, но явное имя делает намерение однозначным.
