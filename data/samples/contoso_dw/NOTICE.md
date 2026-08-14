Data in this folder is a sample drawn from:

Contoso Data Generator V2, The SQLBI Corp.
https://github.com/sql-bi/Contoso-Data-Generator-V2-Data
(`csv-10k.7z` release asset, tagged `ready-to-use-data`)

Licensed under [MIT](https://github.com/sql-bi/Contoso-Data-Generator/blob/main/LICENSE).
All data is synthetic -- customer names, addresses, and other personal-looking
fields are generated, not real people.

Changes made: from the ~3,996-row `sales.csv` in the 10k release, took an
evenly-spaced sample of 60 rows across the full date range (rather than the
first 60, for temporal variety), then kept only the customers and products
those 60 sales rows actually reference (60 of ~105,000 customers, 59 of 2,518
products). `store.csv` (74 stores) is kept in full since it's already small.
`date.csv` (a calendar dimension) and `currencyexchange.csv` (daily FX rates)
were left out entirely -- neither contains entities or facts worth
extracting, just repetitive calendar/rate data. See `../SOURCES.md` for
details.
