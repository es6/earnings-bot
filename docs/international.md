# International tickers

The plan for tracking companies listed outside the US, agreed with the owner on November 15,
2026. Nothing here is built yet. Facts marked **checked** were tested on Nov 15; the rest is
what we expect, to be checked when its part is built.

## Scope

- **Markets:** ASX, Hong Kong, Tokyo, Toronto (and TSX Venture), and Europe's main markets:
  Euronext (Paris, Amsterdam, Brussels, Lisbon, Dublin, Milan, Oslo), London, Germany (Xetra
  and Frankfurt), Switzerland (SIX), the Nasdaq Nordic markets (Stockholm, Copenhagen,
  Helsinki, Iceland), Madrid, Vienna, Warsaw, Athens, Prague and Budapest.
- **Any other exchange** is learned when `/track` meets one of its tickers, identifies the
  company and the exchange correctly, and confirms it with a second source (below).
- **US tickers keep today's path, untouched.** The international logic runs only when the
  symbol names a non-US exchange, or when a plain symbol isn't on SEC's list.

## Entering a symbol

The standard form is `SYMBOL.SUFFIX`, with Yahoo Finance's suffixes, which is what most people
copy from finance sites. `/track` also accepts `EXCHANGE:SYMBOL`, as TradingView and Google
Finance write it, and stores the standard form.

| Market | Standard form | Also accepted | OpenFIGI code | MIC | Time zone | Currency |
|---|---|---|---|---|---|---|
| ASX | `TEA.AX` | `ASX:TEA` | AU (**checked**) | XASX | Australia/Sydney | AUD |
| Hong Kong | `0700.HK` | `HKEX:700` | HK (**checked**) | XHKG | Asia/Hong_Kong | HKD |
| Tokyo | `7203.T` | `TSE:7203` | JP (**checked**) | XTKS | Asia/Tokyo | JPY |
| Toronto | `SHOP.TO` | `TSX:SHOP` | CN (**checked**) | XTSE | America/Toronto | CAD |
| TSX Venture | `ABC.V` | `TSXV:ABC` | CV | XTSX | America/Toronto | CAD |
| Euronext Paris | `SAF.PA` | `EPA:SAF` | FP (**checked**) | XPAR | Europe/Paris | EUR |
| Euronext Amsterdam | `ASML.AS` | `AMS:ASML` | NA | XAMS | Europe/Amsterdam | EUR |
| Euronext Brussels | `UCB.BR` | `EBR:UCB` | BB | XBRU | Europe/Brussels | EUR |
| Euronext Lisbon | `EDP.LS` | `ELI:EDP` | PL | XLIS | Europe/Lisbon | EUR |
| Euronext Dublin | `RYA.IR` | `ISE:RYA` | ID | XMSM | Europe/Dublin | EUR |
| Euronext Milan | `ENI.MI` | `BIT:ENI` | IM | XMIL | Europe/Rome | EUR |
| Euronext Oslo | `EQNR.OL` | `OSL:EQNR` | NO | XOSL | Europe/Oslo | NOK |
| London | `VOD.L` | `LON:VOD` | LN (**checked**) | XLON | Europe/London | GBP (prices in pence) |
| Xetra | `SAP.DE` | `ETR:SAP` | GY | XETR | Europe/Berlin | EUR |
| Frankfurt | `SAP.F` | `FRA:SAP` | GF | XFRA | Europe/Berlin | EUR |
| SIX | `NESN.SW` | `SIX:NESN` | SW / SE | XSWX | Europe/Zurich | CHF |
| Stockholm | `ERIC-B.ST` | `STO:ERIC-B` | SS (**checked**) | XSTO | Europe/Stockholm | SEK |
| Copenhagen | `NOVO-B.CO` | `CPH:NOVO-B` | DC | XCSE | Europe/Copenhagen | DKK |
| Helsinki | `NOKIA.HE` | `HEL:NOKIA` | FH | XHEL | Europe/Helsinki | EUR |
| Iceland | `MAREL.IC` | `ICE:MAREL` | IR | XICE | Atlantic/Reykjavik | ISK |
| Madrid | `SAN.MC` | `BME:SAN` | SM | XMAD | Europe/Madrid | EUR |
| Vienna | `OMV.VI` | `VIE:OMV` | AV | XWBO | Europe/Vienna | EUR |
| Warsaw | `PKO.WA` | `WSE:PKO` | PW | XWAR | Europe/Warsaw | PLN |
| Athens | `OPAP.AT` | `ATH:OPAP` | GA | XATH | Europe/Athens | EUR |
| Prague | `CEZ.PR` | `PRA:CEZ` | CP | XPRA | Europe/Prague | CZK |
| Budapest | `OTP.BD` | `BUD:OTP` | HB | XBUD | Europe/Budapest | HUF |

The OpenFIGI codes not marked checked come from memory and are confirmed when the exchange
table is built. A prefix may also be the suffix's letters (`AX:TEA`) or the MIC
(`XASX:TEA`).

Rules:
- Case doesn't matter, and spaces are trimmed.
- Hong Kong codes get four digits: `700.HK` becomes `0700.HK`. Tokyo codes are four
  characters, including the newer letter codes such as `130A.T`.
- Share classes take a dash: `ERIC B`, `ERIC.B` and `ERIC-B` on Stockholm all become
  `ERIC-B.ST`. OpenFIGI writes the same share as `ERICB` (**checked**), so each exchange maps
  the standard form to OpenFIGI's.
- A plain US ticker is unchanged. `BRK.B` stays American, because `.B` isn't an exchange
  suffix, and `BRK-B` means the same.
- A bare symbol that isn't on SEC's list and could be several listings gets a question, not a
  guess. A bare `SAF` matches 27 listings in OpenFIGI (**checked**), most of them Safran on
  different European venues.
- Alerts show the standard form, the company and the exchange: `$TEA.AX · Tasmea Limited ·
  ASX`.

## Identifying a ticker (and learning a new exchange)

1. **Parse.** A known suffix or prefix names the exchange. A plain symbol on SEC's list takes
   today's US path.
2. **Look it up.** For a known exchange, start with the exchange's own list of companies, then
   OpenFIGI. OpenFIGI's mapping API answered without a key and named the right company for
   `TEA` (AU), `700` (HK), `7203` (JP), `SAF` (FP), `SHOP` (CN) and `VOD` (LN) (**checked**). A
   free key raises its rate limit; the owner signs up if the keyless limit gets in the way.
3. **Confirm with a second source.** The company counts as identified only when a second,
   independent source agrees with OpenFIGI on the company and the listing: the exchange's own
   company page, or the company's IR site naming the ticker and exchange. When they disagree,
   or only one answers, `/track` shows the candidates and the member picks one with a button.
   Nothing is saved on a guess.
4. **Learn the exchange, if it's new.** The official exchange registry (ISO 10383) gives its
   country and city, and from those its time zone; `exchange_calendars` gives its trading
   hours and holidays when it knows the exchange. The new exchange is added automatically once
   two sources agree (the owner's decision). Until it has a dedicated source, its companies
   are watched through generic routes: the IR site, newswires and Chrome. The owner gets a
   status note naming what was learned and flagging trading hours that couldn't be confirmed.
5. **Research.** The rest of `/track` runs as for any company: IR site, release date, release
   history and speed audit.

## What changes in the bot

| Area | Today | Change |
|---|---|---|
| Company identity | Every company is keyed by its SEC CIK: 15 of 58 modules and the database's keys | A stable made-up key for non-SEC companies (negative, so it can never collide with a CIK) plus a listing: exchange and local symbol, and ISIN when known. SEC sources and SEC-based research run only for SEC filers. No table changes |
| Ticker lookup | SEC's list only | Parse the symbol; the exchange's own list; OpenFIGI; the second-source check |
| Periods | `FYyyyyQn` only | Half-year (`FY2026H1`), full year and quarters, named as each company names its own fiscal year |
| Market hours | US 09:30 to 16:00 ET built into window placement and polling | Each exchange's hours and holidays from `exchange_calendars` (the owner's decision); "before the open" means the exchange's own open |
| Release times | US Eastern clock, or a clock without daylight saving when it fits better | The exchange's time zone as the home clock |
| Summaries | English releases; formatting assumes dollars | Japanese and Chinese releases summarized in English by Muse Ember at low effort (the owner's decision); ¥, HK$, A$, €, £ (and pence), CHF, kr, zł, Kč, Ft and C$ |
| Discord | `$NVDA` | `$TEA.AX` with the exchange named |

## Dedicated sources by market

| Market | Where a release appears first | Where dates come from | Notes |
|---|---|---|---|
| ASX | ASX's announcements data, `asx.api.markitdigital.com/asx-research/1.0/companies/{code}/announcements`: time to the second in UTC, headline, type, price-sensitive flag (**checked**, answered the bot's normal client) | The company's own announcements | Half-year and full-year reports (Appendix 4D and 4E); most come before the Sydney open |
| Toronto | Canadian newswires (GlobeNewswire, Cision, Newsfile, Business Wire) and the IR site; SEDAR+ filings as a backup | IR event pages and wire date notices | The closest to the US: quarterly, Eastern time. Many are already on SEC's list and stay on the US path |
| Hong Kong | HKEXnews announcements, by category and time | Board-meeting notices, which give the results date in advance | English and Chinese; results often come after the close or at lunch |
| Tokyo | TDnet, Japan's timely-disclosure system | JPX's published results schedule | Mostly Japanese, some English; results usually come around 15:00 Tokyo time |
| London | The London Stock Exchange's news service (RNS) | The company's financial calendar | Releases at 07:00 London time |
| Euronext | Euronext's company press releases | The company's financial calendar | Before the open or after the close |
| Germany | Deutsche Börse and the EQS news service | The company's financial calendar | |
| Nasdaq Nordic | Nasdaq's company news, which goes out through GlobeNewswire | The company's financial calendar | |
| SIX and the rest | The company's IR site, wires, national news services | The company's financial calendar | |

Safran's `/finance` page answered the bot's normal client with 403 (**checked**), so French
IR sites may need the Chrome route, as some US ones do.

## Testing `/track` until it gets everything first time

The owner wants to run the whole `/track` process over and over, fixing what it gets wrong
together with Claude, until a fresh run gets every fact right on the first try. See
[track-testing.md](track-testing.md). Each market goes live only after its answer keys pass
on a batch of companies.

## Order of work

About 15 to 20 PRs. The identity change waits until the live streak ends (Nov 20) and the
multiple-servers PRs (#136 to #138) are merged, since they touch the same code.

**Phase 0: test mode**
1. `/track ... test:true` and its saved reports (track-testing.md). The rest is built and
   checked with it.

**Phase A: foundations**
1. Symbol parsing and the exchange table (this document's table), with `exchange_calendars`.
2. A company key that isn't from SEC, and SEC sources only for SEC filers.
3. Half-year and full-year periods.
4. Each exchange's hours, holidays and home clock for windows and polling.
5. Currencies, units and languages in summaries.

**Phase B: identification**
1. Lookup for known exchanges: the exchange's own company list, then OpenFIGI, then the
   second-source check.
2. Learning a new exchange, with the confirm button when sources disagree.

**Phase C: dedicated sources, one PR per market, each tested against past releases**
1. ASX
2. Toronto
3. Hong Kong
4. Tokyo
5. London
6. Euronext
7. Germany
8. Nasdaq Nordic
9. SIX and the rest

**Phase D: live checks as each market reports.** Approximate, to confirm per company:
- ASX mostly in February and August.
- Hong Kong's quarterly reporters in late November, the rest in March and August.
- Tokyo every quarter, next late January to February.
- Europe February to March and July to August.
- Toronto every quarter.

## Decisions (owner, Nov 15)

1. Standard form `SYMBOL.SUFFIX` (Yahoo Finance's suffixes), with `EXCHANGE:SYMBOL` also
   accepted.
2. OpenFIGI for identification. A free key if the keyless limit gets in the way, which the
   owner signs up for.
3. `exchange_calendars` for trading hours and holidays.
4. Japanese and Chinese releases summarized in English by Muse Ember at low effort.
5. New exchanges learned automatically once two sources agree.
6. The international logic runs only for symbols that name a non-US exchange, or plain
   symbols that aren't on SEC's list.
7. A `/track` test mode, proposed in track-testing.md.
