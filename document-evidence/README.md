# Document evidence

A deterministic, local-only Python library for audit-ready document evidence.

## API

- `inventory(root)` inventories supported `PDF`, `DOCX`, and `XLSX` files as sorted relative-path records.
- `canonical_records(paths, root=root)` produces plain-text records with stable document locators:
  - PDF: `pdf:page=N`
  - DOCX: `docx:paragraph=N` and `docx:table=N,row=N,col=N`
  - XLSX: `xlsx:sheet=NAME,row=N,col=COLUMN`
- `assess_coverage(requirements, records, blockers=())` reports `PASS`, `PARTIAL`, or fail-closed `BLOCKED` coverage. A caller supplies local extraction failures in `blockers`; no network recovery is attempted.

All returned collections are tuples of JSON-serializable dictionaries, ordered deterministically. The library makes no HTTP calls and reads no credentials or environment secrets.

## Test

```bash
python -m pytest -q tests
```
