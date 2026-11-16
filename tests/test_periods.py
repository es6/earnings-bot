"""Which fiscal period a headline is about."""

import pytest

from earnings_bot.periods import agrees_with_period


@pytest.mark.parametrize(
    ("headline", "period", "expected"),
    [
        # Home Depot names a fiscal year for the calendar year it starts in, while a plan worked
        # out from its January year end names it for the year it ends in
        ("The Home Depot Announces Third Quarter Fiscal 2026 Results", "FY2027Q3", True),
        ("Lowe's Reports Third Quarter 2026 Sales and Earnings Results", "FY2027Q3", True),
        ("NVIDIA Announces Financial Results for Third Quarter Fiscal 2027", "FY2027Q3", True),
        ("Company Reports Q2 2026 Financial Results", "FY2026Q3", False),
        ("Company Reports Q3 2024 Financial Results", "FY2026Q3", False),
        ("Company Reports Record Third Quarter Revenue", "FY2026Q3", True),
        ("Company Reports Record Second Quarter Revenue", "FY2026Q3", False),
    ],
)
def test_a_headline_agrees_with_the_planned_quarter(
    headline: str, period: str, expected: bool
) -> None:
    assert agrees_with_period(headline, period) is expected
