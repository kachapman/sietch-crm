# Reconciler baseline compatibility (review branch only)

Date: 2026-11-06. Branch: `cursor/reconciler-baseline-compat-8047`.
Base: `ee6e40145c2329c81be3aa43c7b304d51428cd21` (`main`).
Package: `reconciler` `0.2.0`.

This branch does not merge, deploy, open a pull request, write a
production database, or carry claim data. It does not implement the
Sherwood v30 parser. That work stays one design, on Tumnus's branch
`reconciler-parser-v30` off `e04750d`.

## What this branch changed

| Path | Role |
| --- | --- |
| `reconciler/sietch_compat.py` | Sietch-side record and read-only signals. Does not parse. |
| `tests/test_reconciler_baseline_compat.py` | Synthetic tests. SHA-256 lock of the nine `ee6e401` parser modules. |
| `docs/RECONCILER_BASELINE_COMPAT.md` | This note. |
| `docs/SESSION_HISTORY.md`, `CHANGELOG.md` | Session record. |

Not modified: `parse.py`, `pdftext.py`, `diff.py`, `supplement.py`,
`tieout.py`, `money.py`, `notes.py`, `commission.py`, `reconciler/__init__.py`,
`server.py`, `Dockerfile`. `sietch_compat` is not added to `reconciler.__all__`
so the package export list stays the `ee6e401` list.

## Sherwood baseline was not reviewable here

Requested tree:

`https://github.com/kachapman/sherwood-toolbox` at `e04750d`,
`vendor/sietch-reconciler/reconciler/`.

What this VM could do:

- `gh` is logged in as the cloud-agent account `cursor`.
- `GET /repos/kachapman/sherwood-toolbox` returns HTTP 404.
- `git ls-remote` of that URL returns "Repository not found".
- Public fork `claudchereji/sherwood-toolbox` (pushed 2026-07-13) has no
  commit `e04750d` and no `vendor/sietch-reconciler`. Its `vendor/` entry
  is `vendor/restoration-common` only.
- Laptop paths and local eval reports were not read.

Tumnus's report is therefore **unverified**, not contradicted:

- Divergent from `ee6e401`: `parse.py`, `pdftext.py`, `diff.py`.
- Unchanged: `tieout.py`, `money.py`, `notes.py`, `commission.py`,
  `__init__.py`.
- `supplement.py`: upstream-only (Sietch has it; the vendor tree is not
  claimed to carry a Sherwood edit).

No Sherwood delta was ported. A blind replacement was not possible and
would have been the wrong shape anyway. There is no isolated "port"
commit because there was no verified hunk to apply.

## Public API held at ee6e401

These signatures are what a later patch must keep, or must migrate in
the same commit that changes them. Annotations are strings because the
modules use `from __future__ import annotations`.

```text
parse_estimate(path: str, force_text: bool = False) -> Dict
parse_text(text: str, path: str = "<text>", bbox: Optional[Dict] = None,
           allow_schema: bool = True) -> Dict
parse_supplement(path: str, force_text: bool = False) -> Dict
parse_supplement_text(text: str, path: str = "<supplement>") -> Dict
diff_items(old: List[Dict], new: List[Dict]) -> Dict
diff_summary(d: Dict) -> Dict
```

`pdftext.extract_text` still returns `status` of `ok`, `needs_ocr`, or
`error`. `parse_text` uses `ok` and `needs_ocr`. Neither function emits
`accepted`. `diff_items` has a `needs_review` list for non-exact line
matches. That list is not a parse-completeness status.

`reconciler.sietch_compat.acceptance_signals()` always sets
`final_acceptance` to `not_decided_waiting_on_v30`. A passing tie-out
does not flip that. The completeness gate is not implemented in this
package.

## Deployment gap (left in place)

`Dockerfile` installs `poppler-utils` and copies top-level `*.py`,
`doc_pdf/`, `scanner/`, and `public/`. It does not `COPY reconciler`.
The running dashboard image therefore does not contain this package.

`server.py` does not contain the string `reconciler` and does not call
`parse_estimate`. `POST /api/v2/admin/sync/full-reconcile` is the
OnlyOffice CRM sync (users, stages, tags, contacts, opportunities,
tasks). It is unrelated and was not edited.

No API route, UI, queue, worker, or cloud OCR fallback was added.
`OCR_HANDOFF_SPEC` in `sietch_compat.py` records the fields a later
handoff may fill (`page_disposition`, `ocr_needed_pages`,
`unresolved_estimate_pages`, `ocr_provenance`) and states that none of
them are emitted. Encrypted XACTDOC decryption is not implemented.
`parse.py` does not reference `xml.etree`, `zipfile`, or `XACTDOC`.

## Supplement coupling

`supplement.py` imports these objects from `parse.py` and uses them by
identity: `HEADER_RE`, `ITEM_RE`, `NUMTOK`, `QTY_RE`, `TOTALS_RE`,
`_parse_qty`, `is_numeric_line`.

`HEADER_RE` is `\bQUANTITY\b.*\bRCV\b`. A `QTY / REMOVE / REPLACE / TOTAL`
line matches neither `HEADER_RE` nor `header_schema` (that helper also
requires the substrings `QUANTITY` and `RCV`).

On a miss, `parse_supplement_text` returns immediately with
`status == "ok"`, no items, and an empty `summaries` list. A `Summary`
block later in the file is not read. The carrier parser is different:
it still walks summaries, keeps `status == "ok"`, and may run the
schema fallback. A v30 header change that only adds a new matcher
inside `parse_items`, and leaves `HEADER_RE` as it is, will not move
the supplement parser. A change that rewrites the shared `HEADER_RE`
object will. The integration commit has to choose one of those on
purpose.

A synthetic supplement that already uses `QUANTITY ... RCV` still
parses one line and ties out. That path was not rewritten.

## Baseline behavior the v30 patch owns

Synthetic fixtures only. Names are `SYNTHETIC PERSON` and claim ids
`SYN-0000x`. These runs do not reproduce the local evaluator's 961-PDF
counts (38.8% / 93.0% / 455 / 48). Those counts were not in this
workspace and were not recomputed. Several gaps can overlap on one
file; a unit test cannot say which count a real file would have
incremented.

Tests marked `BASELINE GAP` in `tests/test_reconciler_baseline_compat.py`
describe `ee6e401`. They are not the target contract. The integration
commit should replace them with the patch's behavior.

1. **Alternate header, no summary.** `parse_text` returns `status ok`,
   zero items, warning `no line-item table header (QUANTITY ... RCV)`,
   `schema_fallback` absent, and `tieout_estimate` **passes** because
   the only check is empty row-math. That is a false accept relative
   to the requested gate. The schema fallback never starts, because it
   runs only when the legacy tie-out fails.

2. **Alternate header, with a summary.** Tie-out fails
   (`items pretax vs summary Line Item Total`). Schema fallback runs
   and does not tie. `status` stays `ok`.

3. **Standard header is unchanged.** One synthetic `10.00 SQ` line at
   `$100.00` ties to `$1,000.00` on the legacy path. `looks_ocr` is
   false and `ocr_fix` returns the input unchanged.

4. **Line-number hole.** Items 1 and 3 produce the warning
   `line numbers not parsed: [2]`. `status` stays `ok` and tie-out
   passes when the printed dollars match the parsed rows. The warning
   is not a gate.

5. **Printed total the legacy parser can see.** Item RCV `$1,000.00`,
   printed `Line Item Totals` `$1,500.00`. `allow_schema=False` stores
   the mismatch and fails tie-out, with `status` still `ok`. The
   default path (`allow_schema=True`) switches to `parser_path
   "schema"`, drops `line_item_totals_printed`, passes tie-out, and
   `draft_gate` on that result against itself is `allowed`. The
   fallback can stop looking at a printed total the legacy parser
   already compared. This is not a detector for arbitrary missing
   lines.

6. **Offsetting omissions.** `tieout_estimate` passes a hand-built
   estimate whose parsed sum equals the printed total. Two omitted
   amounts that add to zero (`+5000` and `-5000` cents in the test)
   are not in the text and are not inferred. `sum_tie_detects_offsetting_omissions`
   is false. A matching total is not evidence that the scope is
   complete.

7. **Scan / garble / non-estimate.** `parse_text` of a short string
   returns `needs_ocr` and no items. `looks_garbled` has one family of
   reasons for an empty layer, a sparse layer, and prose with no
   Xactimate markers (the prose reason says it does not look like an
   estimate). There is no photo-vs-sketch-vs-blank-vs-scanned-estimate
   status. `parse_text` does not call `looks_garbled`, so a long
   non-estimate string is `status ok` with zero items.
   `looks_garbled` is document-level: a form-feed plus a blank second
   page does not create a per-page disposition. `extract_text` can set
   the whole document to `needs_ocr` when at least half of the
   non-empty pages are sparse (`too_few >= max(1, len(page_stats) // 2)`).
   That drops native pages together with the sparse ones. It does not
   return an OCR-needed page list, and a partial document is not held
   out of final acceptance by this package.

8. **Structured files.** `.xml`, `.esx`, and `.zip` go through
   `parse_estimate`'s PDF path. With `pdftotext` absent they return
   `status error` and no items. A zip member named
   `../outside/should-not-be-written.txt` is not extracted, because
   this package does not open zip archives. `force_text=True` on an
   `.esx` reads it as text, finds no `QUANTITY ... RCV` header, and
   returns `status ok` with zero items. No unsupported-format status
   exists yet. No encrypted XACTDOC path exists.

## Dependencies and suites

| Check | Result here |
| --- | --- |
| `python3 tests/test_reconciler_baseline_compat.py` | 25 passed, 0 failed. |
| `tests/test_reconciler_commission.py` | 20 passed. |
| `tests/test_reconciler_notes.py` | 12 passed. |
| `tests/test_reconciler_matcher.py` | 16 passed, 4 skipped (fixture subtests). |
| `tests/test_reconciler_parse.py` | 0 failures. 50 test methods, 114 skips. Every skip is a missing file under `tests/fixtures/reconciler/`. |
| `tests/test_reconciler_pairs.py` | 0 failures. 6 test methods, 56 skips, same missing fixtures. |
| `pdftotext` / poppler | **Not installed.** `apt-get` could not update package lists (permission denied on `/var/lib/apt/lists/partial`). `extract_text` was not run on a real PDF. The missing-binary path returns `status error`. |
| PostgreSQL | Not used. These suites do not import `server.py`. |
| Sherwood `e04750d` file diff | Not run. Repo not readable. |
| Local 961-PDF evaluator | Not run. Corpus and laptop reports stay local. |

## Narrow convergence plan

Do this when Tumnus's code-only v30 patch arrives. Do not start a second
parser in the meantime.

1. On a machine that can read `kachapman/sherwood-toolbox`, diff
   `vendor/sietch-reconciler/reconciler/` at `e04750d` against
   `ee6e401` for `parse.py`, `pdftext.py`, and `diff.py`. Confirm the
   unchanged list (`tieout`, `money`, `notes`, `commission`,
   `__init__`) and that `supplement.py` has no vendor edit.
2. Review that delta. Port only hunks that match Sietch's public
   signatures and tests. Do not copy the vendor tree over `ee6e401`.
3. Apply `reconciler-parser-v30` as the single core change on top of
   that reviewed baseline. Expected touch list, unless the patch says
   otherwise: `parse.py`, `pdftext.py`, and any tie-out/depreciation
   helpers the patch actually edits. Keep `supplement.py` unless the
   patch includes a supplement delta, and re-check the shared
   `HEADER_RE` identity described above.
4. In that same commit, replace each `BASELINE GAP` test with the
   patch's contract. Keep the native-text, signature, deployment-gap,
   and "offsetting omissions are not implied by a tied sum" tests
   unless the patch's contract changes them on purpose.
5. Preserve `parse_estimate(path, force_text=False)` and
   `parse_text(...)`. If a signature has to change, write the
   migration in that commit. Do not add `COPY reconciler` or a
   `server.py` route in the parser patch.
6. Leave PaddleOCR as the spec in `OCR_HANDOFF_SPEC` until a separate
   task builds a worker. No queue, no cloud fallback, no model call,
   no XACTDOC decryption in the parser patch.
7. Re-run `tests/test_reconciler_baseline_compat.py` and the existing
   `tests/test_reconciler_*.py` files. Golden samples stay outside git.

## Remaining local evaluation (not this VM)

- Diff `e04750d` as in step 1 and attach the file list to the patch.
- Apply the patch and re-run the local evaluator on the 961 PDFs.
  Report line recall and the accept rate from that run only. Do not
  commit the PDFs, the eval report, or any claim rows.
- Count, without double-counting, how many of the 455 alternate-header
  files now return items, and how many of the 48 mixed text/image
  files keep native pages and surface unresolved pages instead of
  `status ok`.
- Confirm a tied total is still not treated as proof that omitted
  lines cancelled out.
- Confirm supplement estimates that already use `QUANTITY ... RCV`
  still tie out, and that an alternate-header supplement is either
  dispatched or explicitly not accepted.
- Confirm `.esx` / `.xml` either parse from the structured source or
  return an explicit unsupported/error status, and that a hostile zip
  path is not extracted.
- Confirm the dashboard image and `full-reconcile` are still untouched.
