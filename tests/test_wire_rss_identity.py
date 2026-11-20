"""Publisher identity survives the Dublin Core namespace used in live GN RSS."""

from pathlib import Path

import pytest

from first_print.sources import GLOBENEWSWIRE_EARNINGS, RssFeed
from tests.test_sources import response

FEED = (Path(__file__).parent / "fixtures/globe-wrd-20261120.xml").read_text(encoding="utf-8")


@pytest.mark.parametrize("issuer", ["WeRide", "Example Mobility", "Northern Analytics"])
def test_rss_publisher_identity_uses_the_live_namespace_without_accepting_another_issuer(
    issuer: str,
) -> None:
    # The real GN item keeps tickers in categories, not in the headline/short lead.
    body = FEED.replace("WeRide Inc.", f"{issuer} Inc.")
    source = RssFeed("gn", GLOBENEWSWIRE_EARNINGS, ["NASDAQ: WRD"], publisher=issuer)

    [found] = source.parse(response(body))

    assert "/3407344/" in found.url and found.earnings
    assert found.published == "Fri, 20 Nov 2026 11:55 GMT"
    # A wire item mentioning another company is not issued by it.
    assert not RssFeed("gn", GLOBENEWSWIRE_EARNINGS, publisher="Another Issuer").parse(
        response(body)
    )
    unknown = body.replace(
        "http://dublincore.org/documents/dcmi-namespace/", "https://other.example/"
    )
    assert not source.parse(response(unknown))
    # Standard Dublin Core remains supported; an unrecognized prefix is not trusted.
    standard = body.replace(
        "http://dublincore.org/documents/dcmi-namespace/", "http://purl.org/dc/elements/1.1/"
    )
    assert source.parse(response(standard))[0].url == found.url
