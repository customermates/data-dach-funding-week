# DACH Funding Week

Who in Germany, Austria and Switzerland raised money, how much, and which funds backed them. One row per funding
round, aggregated per ISO week (Monday to Sunday). Updated daily by a script; maintained by
[Customermates](https://customermates.com).

- `rounds.csv`: company, country, city, round, amount (as announced and in EUR), investor firms, sector, AI and
  AI-agent flags, announcement date and source links.
- `weeks.csv`: rounds, disclosed total and median in EUR, AI share and the most active investors per week.
- `investors.csv`: investor firms by number of rounds in the current year.

## Method and caveats

- Sources: [Tech.eu](https://tech.eu), [Gründerszene](https://www.businessinsider.de/gruenderszene/) and
  [presseportal.de](https://www.presseportal.de). Only facts and links are stored, never article text.
- A keyword filter picks funding news; a model (Claude Haiku) extracts the round; a script checks that the company,
  amount and investors appear in the source text and keeps companies headquartered in DE, AT, CH or LI.
- The same round reported by several sources within 21 days is one row. Investors are firms and public bodies only;
  individual people are never stored. "AI" and "AI agents" are our own classification.
- Coverage is what these sources report: not every round is public, and amounts are as announced. EUR values use the
  ECB reference rate of the announcement day (frankfurter.app).

Licence of this dataset: CC BY 4.0, credit "Customermates Data".
