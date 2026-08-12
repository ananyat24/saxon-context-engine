# Sample data sources

Small, permissively-licensed sample datasets for testing ingestion. Each one
is trimmed down from a larger public source; see below for the original,
full-size dataset if you want more volume.

## northwind/

Classic Northwind Traders sample business database: customers, employees,
products, suppliers, shippers, categories, and a sample of orders and order
line items. Structured, relational data, good for testing the CRM/ERP-style
ingestion path and cross-record entity references (the same customer/employee
IDs recur across orders).

- **Source**: [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/northwind-pubs), `instnwnd.sql`
- **License**: MIT (Microsoft Corporation) — see `NOTICE.md` in this folder for the required copyright notice
- **What was done**: parsed the T-SQL `INSERT` statements into plain CSVs, dropped the embedded binary image columns (product/category pictures, employee photos — irrelevant to this project and not worth the size), and trimmed Orders/Order Details from the full 830/2,155 rows down to a 40-order sample (with their matching line items) for a quick first test. Customers, Employees, Products, Suppliers, Shippers, and Categories are kept in full since they're already small.
- **Full dataset**: same source repo, or the pre-converted CSVs at [neo4j-contrib/northwind-neo4j](https://github.com/neo4j-contrib/northwind-neo4j) (no license file there, so not used directly here — see note below)

## manufacturing_ai4i2020/

Synthetic CNC/milling machine sensor readings (temperature, rotational
speed, torque, tool wear) with failure labels. Good for testing the
`manufacturing` ontology domain pack.

- **Source**: [AI4I 2020 Predictive Maintenance Dataset](https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset), UCI Machine Learning Repository
- **License**: CC BY 4.0 — attribution required, see `NOTICE.md`
- **What was done**: the full dataset is 10,000 rows, 3.4% of which are failure events. Kept all 339 failure rows plus a random sample of 500 non-failure rows (839 total) so the interesting minority class isn't diluted in a small sample.
- **Full dataset**: linked above, direct CSV download, no account needed

## legal_cuad/

Five real (but public) commercial contracts, each with a subset of their
expert-labeled clauses (document name, parties, term, etc.). Good for
testing the `legal` ontology domain pack and unstructured document ingestion.

- **Source**: [Contract Understanding Atticus Dataset (CUAD)](https://www.atticusprojectai.org/cuad), The Atticus Project. Contracts originate from public SEC EDGAR filings.
- **License**: CC BY 4.0 as stated on the project's site — the GitHub repo itself has no LICENSE file, so this rests on that public statement rather than a repo-level grant; see `NOTICE.md`.
- **What was done**: picked 5 contracts of moderate length with several labeled clauses from the 510 in the full CUADv1.json, and extracted just those contracts' full text plus their non-empty clause annotations.
- **Full dataset**: same source, `CUADv1.json` (510 contracts, ~13,000 annotations, ~40MB)
