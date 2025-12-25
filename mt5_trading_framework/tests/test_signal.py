from bot.utils import Signal


def test_signal_enum():
    assert Signal.BUY.value == "BUY"
