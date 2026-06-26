def format_market_status() -> str:
    signals = read_signals()
    if not signals:
        return "Нет данных."

    latest_by_symbol: dict[str, dict[str, Any]] = {}
    for row in signals:
        symbol = row.get("symbol", "")
        if symbol:
            latest_by_symbol[symbol] = row

    blocks = ["📊 Market Status"]
    for symbol in SYMBOL_ORDER:
        row = latest_by_symbol.get(symbol)
        display_symbol = symbol.replace("/", "")
        if not row:
            blocks.append(f"\n{display_symbol}\nНет данных.")
            continue

        direction = _value(row, "direction")
        if direction == "LONG":
            direction_line = f"📈 Direction: {direction}"
        elif direction == "SHORT":
            direction_line = f"📉 Direction: {direction}"
        else:
            direction_line = f"➡️ Direction: {direction}"

        blocks.append(
            "\n".join(
                [
                    f"\n{display_symbol}",
                    direction_line,
                    f"🚦 Signal: {_value(row, 'signal')}",
                    f"⭐ Score: {_value(row, 'score')}",
                    f"🎯 Distance: {_format_distance(row)}",
                ]
            )
        )

    return "\n".join(blocks)
