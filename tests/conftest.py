import pytest

from earnings_bot import pipeline


@pytest.fixture(autouse=True)
def no_pdf_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release PDF waits 10 s for a page to deliver the text first. Tests don't."""
    monkeypatch.setattr(pipeline, "PDF_TEXT_GRACE", 0.0)
