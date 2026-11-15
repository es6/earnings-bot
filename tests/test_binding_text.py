import pytest

from earnings_bot.binding_text import duration


@pytest.mark.parametrize("months", [3, 6, 9, 12])
def test_a_window_ending_on_a_date_is_not_a_point_in_time(months: int) -> None:
    assert duration(f"{months} months ending December 31, 2026") == months
    assert duration("as of December 31, 2026") == 0
