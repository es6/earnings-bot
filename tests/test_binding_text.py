from decimal import Decimal

import pytest

from earnings_bot.binding_text import duration, normalized, number_tokens, words


@pytest.mark.parametrize("months", [3, 6, 9, 12])
def test_a_window_ending_on_a_date_is_not_a_point_in_time(months: int) -> None:
    assert duration(f"{months} months ending December 31, 2026") == months
    assert duration("as of December 31, 2026") == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("$2.649 - $2.653 billion", [(2649, 6), (2653, 6)], id="dash-shares-the-scale"),
        pytest.param("$4.8 to $5.0 billion", [(48, 8), (50, 8)], id="to-shares-the-scale"),
        pytest.param("$(1.2)-$(1.3)B", [(-12, 8), (-13, 8)], id="negative-range"),
    ],
)
def test_a_range_reads_both_ends_at_the_scale_written_once(
    text: str, expected: list[tuple[int, int]]
) -> None:
    found = [amount for _, _, amount, _, _ in number_tokens(text)]

    assert found == [Decimal(digits).scaleb(power) for digits, power in expected]


def test_a_dash_right_after_an_amount_is_a_range_and_any_other_minus_stays() -> None:
    def amounts(text: str) -> list[Decimal]:
        return [amount for _, _, amount, _, _ in number_tokens(text)]

    assert amounts("$3 - $4") == [3, 4]
    assert amounts("(37.5) and -2.1%") == [Decimal("-37.5"), Decimal("-2.1")]
    assert amounts("loss of $5 million - see note") == [5_000_000]


def test_footnote_marks_go_but_amounts_glued_to_a_currency_stay() -> None:
    assert normalized("Net payout LTM12,13 of 61%") == "net payout ltm of 61%"
    assert normalized("Adjusted EPS1 of $0.81") == "adjusted eps of $0.81"
    assert normalized("R&D costs of around EUR1.2 billion") == "r&d costs of around eur1.2 billion"


def test_prefixes_written_fused_are_one_word_and_an_ampersand_joins_a_word() -> None:
    assert words("Pre-tax e-commerce ex-fuel non-GAAP") == "pretax ecommerce ex fuel non gaap"
    assert words("SG&A costs, Engines & Services") == "sg&a costs engines services"


def test_a_year_ending_on_a_date_is_a_year() -> None:
    assert duration("Year ending December 31, 2026") == 12
    assert duration("Quarter ending September 30") == 0  # a balance at the quarter's end
