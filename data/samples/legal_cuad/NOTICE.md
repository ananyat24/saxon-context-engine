Data in this folder is a small sample drawn from:

The Atticus Project. Contract Understanding Atticus Dataset (CUAD).
https://www.atticusprojectai.org/cuad

Stated as [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) on the
project's site. Note: the [GitHub repository](https://github.com/The-Atticus-Project/cuad)
itself has no LICENSE file, so this rests on that public license statement
rather than a repo-level grant. The underlying contracts are public filings
from the SEC's EDGAR system, not confidential documents.

Changes made: selected 5 of the 510 contracts in the full `CUADv1.json`
(moderate length, several labeled clauses each), and extracted just those
contracts' text and their non-empty clause annotations. See `../SOURCES.md`
for details.

Reference: Hendrycks, D., Burns, C., Chen, A., & Ball, S. (2021). CUAD: An
Expert-Annotated NLP Dataset for Legal Contract Review. NeurIPS 2021.
https://arxiv.org/abs/2103.06268
