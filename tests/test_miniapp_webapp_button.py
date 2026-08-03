from telegram_ui.keyboards import home_keyboard


def environment(**overrides):
    values = {
        "TELEGRAM_UI_V2_ENABLED": "true",
        "MINIAPP_ENABLED": "true",
        "MINIAPP_OWNER_ONLY": "true",
        "MINIAPP_OWNER_USER_ID": "42",
        "MINIAPP_PUBLIC_URL": "https://mini.example/app",
    }
    values.update(overrides)
    return values


def test_owner_sees_https_webapp_button():
    rows = home_keyboard(user_id=42, environ=environment()).inline_keyboard
    button = rows[0][0]
    assert button.text == "⚡ Открыть TradeWatcher"
    assert button.web_app.url == "https://mini.example/app"
    assert button.callback_data is None


def test_non_owner_disabled_and_invalid_urls_hide_button():
    assert len(home_keyboard(user_id=7, environ=environment()).inline_keyboard) == 4
    assert len(home_keyboard(user_id=42, environ=environment(MINIAPP_ENABLED="false")).inline_keyboard) == 4
    assert len(home_keyboard(user_id=42, environ=environment(MINIAPP_PUBLIC_URL="http://mini.example")).inline_keyboard) == 4
    assert len(home_keyboard(user_id=42, environ=environment(TELEGRAM_UI_V2_ENABLED="false")).inline_keyboard) == 4


def test_legacy_default_keyboard_is_unchanged():
    buttons = [button.callback_data for row in home_keyboard().inline_keyboard for button in row]
    assert len(buttons) == 8
    assert buttons[0] == "ui:v2:signals"
