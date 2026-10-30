from earnings_bot.estimates import Estimates
from earnings_bot.summary import unverified_numbers

RELEASE = """\
Uber Announces Results for Second Quarter 2026
Gross Bookings grew 24% year-over-year to $58.0 billion
Revenue | $ 12,651 | $ 14,211
Diluted EPS | $ 0.63 | $ 1.17
Trips | 3,350 | 3,900
Adjusted EBITDA margin was 4.8% of Gross Bookings"""


def test_numbers_from_the_release_check_out() -> None:
    post = """\
UBER $UBER Q2'26 EARNINGS HIGHLIGHTS

🔹 Revenue: $14.21B (Est. $14.05B) 🟢; +12% YoY
🔹 GAAP EPS: $1.17
🔹 Gross Bookings: $58.0B; +24% YoY
🔹 Trips: 3.9B
🔹 Adj. EBITDA margin: 4.8% of Gross Bookings"""

    # +12% isn't stated in the release, so it's the one number to check
    assert unverified_numbers(post, RELEASE, Estimates(eps=0.83, revenue=14.05e9)) == ["12%"]


def test_made_up_numbers_are_flagged() -> None:
    post = "UBER $UBER Q2'26 EARNINGS HIGHLIGHTS\n\n🔹 Revenue: $15.80B\n🔹 FY26 Guide: $61.2B"

    assert unverified_numbers(post, RELEASE, None) == ["15.80B", "61.2B"]


def test_labels_and_small_counts_are_ignored() -> None:
    post = "HEADER\n\nQ3 Guide:\n🔹 FY26 revenue growth in 2 segments"

    assert unverified_numbers(post, RELEASE, None) == []
