from earnings_bot.text import html_to_text, pick_exhibit


def test_html_to_text_keeps_paragraphs_and_table_columns() -> None:
    html = """<html><head><title>x</title><style>.bw { color: red }</style></head><body>
    <p>SAN FRANCISCO--(BUSINESS WIRE)--Uber Technologies, Inc. today announced
       results &amp; more.</p>
    <script>track()</script>
    <table><tr><td><p>Revenue</p></td><td></td><td><p>$ 12,651</p></td><td>$ 14,211</td></tr>
    <tr><th>Trips</th><td>3,350</td><td>3,900</td></tr></table>
    </body></html>"""

    assert html_to_text(html).splitlines() == [
        "SAN FRANCISCO--(BUSINESS WIRE)--Uber Technologies, Inc. today announced results & more.",
        "Revenue | $ 12,651 | $ 14,211",
        "Trips | 3,350 | 3,900",
    ]


def test_pick_exhibit_prefers_the_press_release() -> None:
    files = [
        {"name": "0001543151-26-000027-index.html", "size": ""},
        {"name": "R1.htm", "size": "38314"},
        {"name": "uber-20260805.htm", "size": "24475"},
        {"name": "uberq226earningspressrelea.htm", "size": "447114"},
        {"name": "MetaLinks.json", "size": "19403"},
    ]

    assert pick_exhibit(files, "uber-20260805.htm") == "uberq226earningspressrelea.htm"


def test_pick_exhibit_falls_back_to_the_biggest_document() -> None:
    files = [
        {"name": "fico-20260805.htm", "size": "20000"},
        {"name": "a2026q3exhibit.htm", "size": "350000"},
        {"name": "small.htm", "size": "900"},
    ]

    assert pick_exhibit(files, "fico-20260805.htm") == "a2026q3exhibit.htm"
