# Security

## Threat model

The Credit Memo Agent reads **borrower financial documents** and drafts a credit
memo. Its central security property is **data locality**: by default nothing about
the borrower leaves the machine. It runs a local Ollama model, computes ratios
deterministically in Python, and writes a local `.docx`. The document contents are
sensitive; the whole design keeps them local unless the operator explicitly opts
into a cloud model.

Assumed trusted: the operator, the machine, the config files.
Sensitive: the borrower document contents and extracted financials.

## What is mitigated

| Risk | Status | Where |
|---|---|---|
| **Sending borrower data to a third party by accident** | **Mitigated by design** — default `LLM_BACKEND=ollama` (fully local). The OpenAI backend is opt-in and requires setting `LLM_BACKEND=openai` explicitly; *having* an API key in the environment does not switch backends | `src/config.py`, `src/llm/backend.py:150` |
| Cloud-backend warning | **Mitigated** — `warn_if_cloud()` logs a clear warning when a non-local backend is selected | `src/config.py` |
| Financial-figure fabrication | **Mitigated** — credit ratios are computed **deterministically in Python**, not by the LLM; the model extracts figures (with per-figure citations), arithmetic is code | `src/` (ratio computation) |
| Container running as root | **Fixed** — image now runs as uid 10001 `memo` (was root) | `Dockerfile` |
| Dependency CVEs | **Clean** — `pip-audit`: no known vulnerabilities; versions pinned |
| Secrets in git history | **Clean** — `gitleaks`: 0 findings; `.env` gitignored |
| Code execution / injection | **Not present** — no `eval`/`exec`/`subprocess`/`pickle` in the app path |

## What is NOT mitigated / notes

- **No authentication** on the API/dashboard. Operator-run tool; run it locally.
- **The OpenAI backend sends document text to OpenAI.** This is stated in
  `.env.example` in capitals ("SYNTHETIC DATA ONLY") and is opt-in. If you point it
  at real borrower data over a cloud backend, that is a data-handling decision you
  are making explicitly.
- **Prompt injection via document contents.** A crafted document could try to
  influence the extraction step. The deterministic ratio computation limits the
  blast radius — the *numbers* come from code — but extracted text fields and the
  narrative are model output and should be reviewed, which is the intended workflow
  (it drafts a memo for a human, it does not decide the loan).

## Reporting

Open an issue. Portfolio/demo project, no production deployment, no security SLA.
