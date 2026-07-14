"""Standalone polling scheduler for new closed trades."""

from __future__ import annotations

import time
from typing import Any

from adaptive_research.engine import AdaptiveResearchEngine


class AdaptiveResearchScheduler:
    """Poll research inputs without coupling the scheduler to the live agent."""

    def __init__(
        self,
        engine: AdaptiveResearchEngine | None = None,
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self.engine = engine or AdaptiveResearchEngine()
        self.interval_seconds = max(float(interval_seconds), 5.0)

    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        """Run one incremental check."""
        return self.engine.run(force=force)

    def run_forever(self) -> None:
        """Watch ``trades.csv`` until interrupted; failures stay isolated."""
        print(
            "Adaptive Research scheduler запущен: "
            f"interval={self.interval_seconds:g} сек"
        )
        while True:
            try:
                report = self.run_once()
                print(
                    f"Adaptive Research: {report.get('status')} "
                    f"at {report.get('generated_at')}"
                )
            except KeyboardInterrupt:
                print("Adaptive Research scheduler остановлен")
                return
            except Exception as exc:  # noqa: BLE001 - scheduler must survive
                print(f"Adaptive Research scheduler warning: {type(exc).__name__}: {exc}")
            time.sleep(self.interval_seconds)
