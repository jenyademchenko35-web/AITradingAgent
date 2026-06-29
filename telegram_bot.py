def format_last_signals() -> str:
    signals = _load_last_signals()
    if not signals:
        return "Нет данных."

    lines = ["📈 Last Signals"]
    for signal in signals[:10]:
        timestamp = _value(signal, "timestamp")
        symbol = signal.get("symbol", "").replace("/", "")
        signal_name = _value(signal, "signal")
        score = _value(signal, "score")
        distance = _format_distance(signal)

        lines.append("")
        lines.append(timestamp)
        lines.append(symbol)
        lines.append(f"🚦 {signal_name}")
        lines.append(f"⭐ Score: {score}")
        lines.append(f"🎯 Distance: {distance}")

    return "\n".join(lines)
