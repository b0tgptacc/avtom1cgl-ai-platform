# Document compose and quality reporting

A local-only Python library for composing structured commercial documents from the two approved specifications:

- `compose_offer(fields)` — `approved-offer-v1`
- `compose_invoice(fields)` — `approved-invoice-v1`

Both reject missing required or unapproved fields, retain only approved line-item fields, and render unknown optional values as `[UNKNOWN]`. They return structured dictionaries plus deterministic plain-text content; rendering into a binary office format is intentionally outside this small contract.

`write_quality_reports(records, output_dir)` writes a matching `quality-report.json` and `quality-report.csv`, including pass/fail summary counts. `synthetic_evaluation_corpus()` loads the checked-in fictional cases in `synthetic_evaluation_corpus.json`.

The library performs no network calls and reads no runtime configuration.

## Test

```bash
python -m pytest -q document-compose/tests
```
