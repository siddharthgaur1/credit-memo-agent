# Credit Memo Agent

**Drafts a business-loan credit memo from borrower documents — extraction cited, ratios computed in code, and by default nothing leaves your machine.**

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/) [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![Runs 100% local](https://img.shields.io/badge/runs-100%25%20local%20(Ollama)-brightgreen)](#run-with-zero-paid-keys)

> **Live demo:** not hosted — this processes financial documents and defaults to a
> **fully local** model precisely so document contents never leave the machine.
> Hosting it publicly would defeat that. It runs free locally on Ollama — below.

## Run with zero paid keys

```bash
git clone https://github.com/siddharthgaur1/credit-memo-agent
cd credit-memo-agent
pip install -r requirements.txt

# install https://ollama.com, then:
ollama pull llama3.1:8b
cp .env.example .env          # LLM_BACKEND=ollama is already the default

uvicorn src.api.main:app --reload      # or: streamlit run dashboard/app.py
```

Nothing about the borrower leaves the machine: a local Ollama model does the
extraction, credit ratios are computed deterministically **in Python** (not by the
LLM), and the `.docx` is written locally. The OpenAI backend is opt-in and, per
`.env.example`, for synthetic data only — setting a key does **not** switch to it.
Data-locality design and full threat model: [SECURITY.md](SECURITY.md).

---

Drafts a business instalment loan credit memo from a borrower's document file:
validates which required documents are present, extracts the financials into a
typed schema with per-figure citations, computes the credit ratios
deterministically, flags risks against editable thresholds, and produces a
`.docx` memo plus a follow-up Q&A over the file.

**It drafts and checks. It does not decide.** There is no approve/reject verdict
and no credit score anywhere in the output — see [Why there is no
verdict](#why-there-is-no-verdict).

---

## Read this first: local mode vs demo mode

This is the one architectural decision everything else is arranged around.

| | **Local mode** (default) | **Demo mode** (opt-in) |
|---|---|---|
| Backend | Ollama, on this machine | OpenAI API |
| Document content leaves the machine | **No** | **Yes** |
| Intended input | Real borrower files | The synthetic files in `sample_data/` only |
| How to select | nothing to do — it is the default | `LLM_BACKEND=openai`, explicitly |

The rationale is simple: a loan file contains a named business's balance sheet,
bank conduct and PAN. The worst thing this system could do is send that to a
third party by accident. So:

- `LLM_BACKEND` defaults to `ollama`. **An `OPENAI_API_KEY` sitting in your
  environment does not switch backends** — there is no silent fallback to the
  cloud path, and there is a test asserting exactly that
  (`test_an_openai_key_alone_never_switches_the_backend`).
- Starting in demo mode prints a loud, unmissable warning to stdout and the log.
- The dashboard shows the active backend in the header on every screen.
- `.gitignore` excludes `cases/`, `uploads/`, `data/local/`, `*.pdf`, `*.xlsx`,
  `*.csv`, `*.docx` and the SQLite DB, so a real borrower file is structurally
  hard to commit by accident.
- LLM call logs record token counts and timings — never prompts, because prompts
  contain document content.

### Before it goes near a real borrower file

Local-only execution makes this a much easier conversation, but it is still a
conversation to have: confirm with whoever owns IT/compliance policy at the bank
that running a local tool over loan documents is permitted. The code cannot
settle that question.

---

## What it does

```
                    ┌─────────────┐
  borrower file ──▶ │   Intake    │  classify each file, parse PDFs + workbooks
  (PDF/XLSX/CSV)    └──────┬──────┘  scanned PDF? say so, don't return empty
                           ▼
                    ┌─────────────┐
                    │  Checklist  │  runs FIRST — present / missing / stale
                    └──────┬──────┘  (config/checklist_*.yaml, no code change)
                           ▼
                    ┌─────────────┐
                    │ Extraction  │  LLM → typed schema, every figure cited
                    └──────┬──────┘  arithmetic verified, uncited figures dropped
                           │
              nothing analysable ──▶ ┌──────────────────┐
                           │         │ Human escalation │ stop, ask a specific question
                           ▼         └──────────────────┘
                    ┌─────────────┐
                    │  Analysis   │  DETERMINISTIC Python. The LLM is not involved.
                    └──────┬──────┘  ratios + trends + risk rules (YAML thresholds)
                           ▼
                    ┌─────────────┐
                    │  Narrative  │  the only thing the LLM writes: prose about
                    └──────┬──────┘  numbers it was handed
                           ▼
                    ┌─────────────┐
                    │    Memo     │  .docx: checklist, financials, ratios with
                    └──────┬──────┘  formulas, flags, questions, citation appendix
                           ▼
                    ┌─────────────┐
                    │  Reviewer   │  deterministic pre-delivery checks
                    └─────────────┘
```

## Why the analysis is deterministic and the LLM only explains

Every ratio is computed in plain Python in `src/analysis/ratios.py`. The model is
never asked to do arithmetic, because an LLM arithmetic slip inside a credit memo
is a catastrophic failure mode — it is confident, plausible, and lands in front of
a committee.

The division of labour:

- **The LLM reads.** It locates line items in a document and reports them with a
  citation. That is a language task.
- **Python computes.** Ratios, trends, subtotal checks, threshold rules.
- **The LLM explains.** It writes prose about numbers it was handed, under a
  prompt that forbids introducing or restating any figure.

Three guarantees fall out of this, and each has a test:

1. **Every figure carries provenance.** `Figure` cannot be constructed without a
   source file, a page or sheet, and the row label. After extraction, each
   citation is re-checked against the actual document text — a row label that
   doesn't appear on the cited page means the figure is *dropped*, not caveated.
2. **A missing line item is `None`, never a guess.** Ratios that depend on it
   report `insufficient data` and name the missing input.
3. **A balance sheet that doesn't balance is a finding, not a bug.** Nothing is
   silently corrected; the discrepancy is reported with both numbers.

## Ratios

Each result carries its value, the exact inputs used, the formula, and a
data-completeness flag. The formula is printed in the memo so a credit officer can
re-derive it by hand without reading this repo.

| Category | Ratio | Formula |
|---|---|---|
| Coverage | DSCR | (PAT + Depreciation + Finance cost) / (Finance cost + Principal repayment + Proposed EMI) |
| Coverage | Interest coverage | EBITDA / Finance cost |
| Leverage | Debt / equity | (Long-term + Short-term borrowings) / Net worth |
| Leverage | TOL / TNW | (Total assets − Net worth) / (Net worth − Intangible assets) |
| Liquidity | Current ratio | Total current assets / Total current liabilities |
| Liquidity | Quick ratio | (Total current assets − Inventory) / Total current liabilities |
| Profitability | Gross margin | Gross profit / Revenue × 100 |
| Profitability | Net margin | PAT / Revenue × 100 |
| Profitability | ROCE | (EBITDA − Depreciation) / (Net worth + Long-term borrowings) × 100 |
| Efficiency | Inventory days | Inventory / COGS × 365 |
| Efficiency | Receivable days | Trade receivables / Revenue × 365 |
| Efficiency | Payable days | Trade payables / COGS × 365 |
| Efficiency | Working capital cycle | Inventory days + Receivable days − Payable days |
| Bank conduct | Average monthly balance, inward cheque returns, peak limit utilisation | as summarised from the statement |

Trends across the available periods report direction (improving / deteriorating /
flat / insufficient data) and magnitude for every metric.

## Customising the checklist and the thresholds

Both live in YAML, because credit policy changes and differs by loan type, and
that should never need a developer.

**`config/checklist_business_instalment_loan.yaml`** — required documents. Each
item sets `doc_type`, `min_count`, optional `max_age_days` (staleness, judged on
the newest document) and `mandatory`. The shipped file is a *starting template*
for a business instalment loan — expect to correct it against your own policy.

**`config/risk_rules.yaml`** — risk thresholds. Three rule types:

```yaml
- id: dscr_thin
  type: ratio_threshold      # compare a computed ratio against a threshold
  metric: DSCR
  operator: lt               # lt | lte | gt | gte
  threshold: 1.25
  period: latest             # latest | any
  severity: high
  label: DSCR below policy minimum
  message: "Debt service coverage of {value} is below the {threshold}x minimum."
```

`figure_threshold` compares a raw extracted figure (e.g. negative net worth);
`trend_direction` fires on a metric moving the wrong way by at least
`min_change_pct`. A rule never fires on a ratio that could not be computed —
silence, not a guess.

Both files are mounted into the container, so edits take effect without a rebuild.

## Setup

### Local mode (default, offline)

```bash
git clone https://github.com/<you>/credit-memo-agent.git
cd credit-memo-agent
python -m venv .venv && .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Ollama is a host dependency: https://ollama.com
ollama serve
ollama pull llama3.1:8b        # or qwen2.5:7b — set OLLAMA_MODEL

cp .env.example .env           # defaults are already correct for local mode

python sample_data/generate.py             # synthetic borrower files
python -m src.api.main                     # API on 127.0.0.1:8000
streamlit run dashboard/app.py             # dashboard on 127.0.0.1:8501
```

### Demo mode (synthetic data only)

```bash
export LLM_BACKEND=openai      # explicit. Required. The key alone does nothing.
export OPENAI_API_KEY=sk-...
python -m src.api.main         # prints a large warning banner at startup
```

### Docker

```bash
ollama serve                   # on the host — see the note in docker-compose.yml
docker compose up
# API       http://127.0.0.1:8000
# Dashboard http://127.0.0.1:8501
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | status + which backend is active |
| `POST` | `/cases` | upload a borrower file, returns `case_id`, processes in the background |
| `GET` | `/cases` | case history (local SQLite) |
| `GET` | `/cases/{id}` | full case record: checklist, financials, ratios, flags, escalations |
| `POST` | `/cases/{id}/ask` | follow-up question, answered only from cited case state |
| `GET` | `/cases/{id}/memo.docx` | the drafted memo |

Binds to `127.0.0.1` by default. Change that deliberately, not by accident.

## Sample data

`sample_data/generate.py` writes three complete synthetic borrower files as
digital PDFs + Excel, in Indian SME format (Schedule III presentation, lakhs, FY
labelling). Dates are generated relative to today so the demo never rots into
"everything is stale".

| Borrower | Demonstrates |
|---|---|
| Suryodaya Engineering Works | Healthy — complete file, clean ratios, no material flags |
| Vaishnavi Polymers | Stressed — revenue down 21%, DSCR 0.26x, negative PAT, 4 cheque returns, limits fully drawn |
| Meenakshi Traders | Incomplete — no GST returns, financials 4+ years stale, **a balance sheet that deliberately does not balance**, and depreciation missing from the P&L |

Meenakshi Traders is the honesty test: the tool must report the broken balance
sheet as a finding, and report DSCR as *insufficient data* rather than inventing
the missing depreciation.

## Why there is no verdict

The memo carries a header stating it is machine-generated, requires human
verification, and is not a credit decision. It presents evidence — figures,
formulas, citations, gaps, fired rules — and stops there.

That is deliberate on two counts. It is honest: nothing in this pipeline knows the
borrower's promoter, the sector view, the security cover or the relationship
history, and a confident verdict without those is worse than no verdict. And it is
the only design that survives an audit: "the tool recommended it" is not a defence
a credit officer can stand behind, whereas "the tool surfaced these figures and I
assessed them" is. The reviewer agent enforces this — it fails the memo if
decision-like language appears in the prose.

## Tests

```bash
pytest tests/ -v
```

90 tests, all LLM calls mocked — no model, no key, no network required.

- Ratio engine against hand-computed fixtures, to exact values
- Arithmetic verification catches the non-balancing sheet and does not repair it
- Missing line item → `insufficient data`, never an invented number
- Checklist identifies missing, partially-supplied, stale and undated documents
- Extraction rejects a figure whose citation doesn't resolve
- Excel parser handles the offset-header, merged-header and multi-sheet cases
- Scanned PDF is detected and reported, not silently empty
- `LLMBackend` defaults to Ollama with no key present; a key alone never switches it
- Full LangGraph pipeline over the three generated borrower files, with a stub
  backend that reads the same document text a real model would

## Design decisions

**Deterministic classification, not an LLM call.** Document type is decided by
keyword scoring over filename and content. It is cheap, auditable, and a
misclassification silently poisons everything downstream. Weak evidence returns
`UNKNOWN` with a question rather than a confident guess.

**Checklist before extraction.** No point spending minutes extracting from an
incomplete file, and the gap list alone is most of the day-one value.

**Provenance verified, not trusted.** The model is asked for citations, and then
the citations are checked against the document. An unverifiable figure is worse
than a missing one — it looks trustworthy.

**Backend injected through the graph state.** Not a module singleton. That is what
lets the tests drive the whole pipeline against a stub, and what keeps a case
pinned to the backend it started on.

**Freshness from the document, not the filesystem.** Copying a file does not make
it recent. Financial statements are dated by their financial year; everything else
by the newest date printed on it.

## Limitations

- **No OCR.** Scanned PDFs are detected and reported, not processed. Digital PDFs
  and spreadsheets only.
- **No core banking / bureau integration.** No CIBIL pull, no account aggregator,
  no existing-exposure lookup. Everything comes from the submitted file.
- **Bank statement analysis is summary-level.** It reads a summary sheet rather
  than reconstructing conduct from a full transaction ledger.
- **Draft, not decision.** Stated everywhere for a reason.
- **Extraction quality tracks the local model.** A 7–8B model on a dense Schedule
  III statement will miss line items; the design makes that visible ("insufficient
  data") instead of silent, but visible is not the same as extracted.
- **Single-entity.** No group/associate-concern consolidation.

## What I'd improve with more time

- **A labelled extraction benchmark.** A set of real-format statements with
  hand-keyed ground truth, and per-line-item precision/recall per model — right
  now "does the local model read this well enough" is a judgement call, not a
  number.
- **Page-image click-through in the dashboard.** The citation is exact; the UI
  currently shows it as text rather than jumping to the rendered page.
- **Transaction-level bank statement analysis** — actual monthly balance curves,
  bounce clustering, and detection of circular/related-party flows.
- **A second extraction pass with a different prompt strategy**, cross-checked
  field by field, escalating only where the two disagree.
- **Peer benchmarking** — the ratios mean much more against a sector median than
  against a fixed threshold.
- **An audit trail export**: every prompt, every rejection, every rule evaluated,
  in a form a bank's model-risk function could review.
