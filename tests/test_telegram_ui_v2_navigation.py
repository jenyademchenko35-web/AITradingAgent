from telegram_ui.callbacks import parse_callback
from telegram_ui.keyboards import deep_screen_keyboard, home_keyboard, paginated_keyboard
from telegram_ui.navigation import NavigationStore


def callback_values(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_navigation_tracks_previous_screen_and_back():
    clock = [100.0]
    store = NavigationStore(ttl_seconds=60, clock=lambda: clock[0])
    assert store.get("user").current_screen == "home"
    state = store.update("user", screen="market", selected_symbol="BTCUSDT")
    assert state.previous_screen == "home"
    state = store.update("user", screen="symbol")
    assert state.previous_screen == "market"
    assert store.back("user").current_screen == "market"


def test_navigation_expires_after_ttl():
    clock = [100.0]
    store = NavigationStore(ttl_seconds=10, clock=lambda: clock[0])
    store.update("user", screen="research", page=2)
    clock[0] = 110.0
    assert store.get("user").current_screen == "home"
    assert store.get("user").page == 0


def test_every_deep_keyboard_has_back_and_home():
    values = callback_values(deep_screen_keyboard("market"))
    parsed = [parse_callback(value) for value in values]
    assert {item.action for item in parsed} == {"back", "home"}


def test_pagination_keeps_navigation_controls():
    values = callback_values(paginated_keyboard("research", 2, has_previous=True, has_next=True))
    actions = [parse_callback(value).action for value in values]
    assert actions.count("page") == 2
    assert "back" in actions and "home" in actions
    assert all(value.startswith("ui:v2:") for value in callback_values(home_keyboard()))
