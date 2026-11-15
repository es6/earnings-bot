"""Property tests: rules the pure parsers keep for every input, not just the examples we
thought of. Hypothesis generates the inputs and shrinks any failure to its smallest case."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime

from hypothesis import given
from hypothesis import strategies as st

from earnings_bot.http_policy import retry_after
from earnings_bot.summary_format import amount_of

NOW = datetime(2026, 11, 17, 21, 10, tzinfo=UTC)
WORDS = {"K": ["K", "thousand"], "M": ["M", "million", "millions"], "B": ["B", "billion", "bn"]}
SCALE = {"": 1, "K": 10**3, "M": 10**6, "B": 10**9}


@given(st.integers(min_value=0, max_value=10**9))
def test_retry_after_seconds_are_taken_as_given(seconds: int) -> None:
    assert retry_after(f" {seconds} ", NOW) == seconds


@given(st.integers(min_value=-86_400, max_value=86_400))
def test_retry_after_dates_wait_until_then_and_never_a_negative_time(offset: int) -> None:
    when = NOW + timedelta(seconds=offset)
    assert retry_after(format_datetime(when, usegmt=True), NOW) == max(0, offset)


@given(st.text(max_size=40))
def test_retry_after_never_raises_and_never_waits_a_negative_time(raw: str) -> None:
    """A server's header is untrusted text; whatever it says, polling gets a usable wait."""
    assert retry_after(raw, NOW, default=30) >= 0


def _grouped(number: Decimal) -> str:
    whole, _, fraction = f"{number:f}".partition(".")
    return f"{int(whole):,}" + (f".{fraction}" if fraction else "")


amounts = st.decimals(min_value=0, max_value=10**6, places=2, allow_nan=False)
scales = st.sampled_from(["", "K", "M", "B"])


@given(amounts, scales, st.data(), st.sampled_from(["", "$", "€"]), st.booleans())
def test_an_amount_reads_back_as_its_value_in_base_units(
    number: Decimal, scale: str, data: st.DataObject, marker: str, grouped: bool
) -> None:
    word = data.draw(st.sampled_from(WORDS.get(scale, [""])))
    space = " " if word and len(word) > 2 else ""
    shown = _grouped(number) if grouped else f"{number:f}"
    assert amount_of(f"{marker}{shown}{space}{word}") == (number * SCALE[scale], False)


@given(amounts, scales)
def test_parentheses_and_a_minus_sign_both_mean_negative(number: Decimal, scale: str) -> None:
    positive = amount_of(f"${number:f}{scale}")
    assert positive is not None
    assert amount_of(f"$({number:f}){scale}") == (-positive[0], False)
    assert amount_of(f"-${number:f}{scale}") == (-positive[0], False)


@given(amounts)
def test_a_percentage_is_marked_and_never_scaled(number: Decimal) -> None:
    assert amount_of(f"{number:f}%") == (number, True)


@given(amounts, amounts)
def test_a_range_is_not_an_amount(low: Decimal, high: Decimal) -> None:
    assert amount_of(f"${low:f} to ${high:f}") is None
