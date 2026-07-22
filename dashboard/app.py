"""Streamlit dashboard. Talks to the local FastAPI service.

The active LLM backend is shown in the header at all times -- the user should
never have to wonder whether this run was local.
"""

from __future__ import annotations

import os
import time

import httpx
import pandas as pd
import streamlit as st

API = os.getenv("API_URL", "http://127.0.0.1:8000")

STATUS_ICON = {"present": "🟢", "stale": "🟡", "missing": "🔴", "not_applicable": "⚪"}
SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}

st.set_page_config(page_title="Credit Memo Agent", layout="wide")


def api(method: str, path: str, **kw):
    return httpx.request(method, f"{API}{path}", timeout=600, **kw)


def header() -> None:
    st.title("Business Loan Credit Memo Agent")
    try:
        health = api("GET", "/health").json()
    except Exception as exc:
        st.error(f"API not reachable at {API}: {exc}")
        st.stop()

    if health["local_only"]:
        st.success(f"🔒 LOCAL MODE — Ollama ({health['model']}). No document content leaves this machine.")
    else:
        st.error(
            f"☁️ DEMO MODE — {health['model']} via OpenAI. Document content is sent to an external API. "
            "Use synthetic files only."
        )
    st.caption(
        "This tool drafts and checks. It does not decide. Every memo it produces is a draft for a "
        "credit officer to verify and own."
    )


def upload_panel() -> str | None:
    st.subheader("New case")
    files = st.file_uploader(
        "Borrower file (digital PDFs, Excel, CSV)",
        type=["pdf", "xlsx", "xls", "csv"],
        accept_multiple_files=True,
    )
    col1, col2 = st.columns(2)
    borrower = col1.text_input("Borrower name (optional)")
    facility = col2.text_input("Facility requested (optional)")

    if st.button("Process file", disabled=not files):
        payload = [("files", (f.name, f.getvalue())) for f in files]
        resp = api(
            "POST",
            "/cases",
            files=payload,
            params={"borrower_name": borrower or None, "facility_requested": facility or None},
        )
        if resp.status_code >= 400:
            st.error(resp.text)
            return None
        return resp.json()["case_id"]
    return None


def wait_for(case_id: str) -> dict:
    progress = st.empty()
    while True:
        data = api("GET", f"/cases/{case_id}").json()
        if data.get("status") == "done":
            progress.empty()
            return data["record"]
        if str(data.get("status", "")).startswith("failed"):
            st.error(data["status"])
            st.stop()
        progress.info(f"Working… ({data.get('status')})")
        time.sleep(2)


def show_progress(record: dict) -> None:
    with st.expander("Agent progress", expanded=False):
        for line in record.get("progress", []):
            st.write("•", line)
        for line in record.get("classifications", []):
            st.caption(f"{line['filename']} → {line['doc_type']} (confidence {line['confidence']})")


def show_checklist(record: dict) -> None:
    st.subheader("Document checklist")
    results = record.get("checklist", {}).get("results", [])
    if not results:
        st.info("No checklist result.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "": STATUS_ICON.get(r["status"], "⚪"),
                    "Requirement": r["label"],
                    "Status": r["status"].upper(),
                    "Reason": r["reason"],
                    "Files": ", ".join(r["matched_files"]) or "-",
                }
                for r in results
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )


def _citation(fig: dict | None) -> str:
    if not fig:
        return ""
    c = fig["citation"]
    where = f"p.{c['page']}" if c.get("page") else f"sheet '{c.get('sheet')}'"
    return f"{c['source_file']}, {where}, row '{c['row_label']}'"


def show_financials(record: dict) -> None:
    st.subheader("Extracted financials")
    st.caption("Every figure below is traceable to a source page. 'not found' means absent, not zero.")

    fin = record.get("financials", {})
    for group, title in (("balance_sheets", "Balance sheet"), ("profit_and_loss", "Profit & loss")):
        for statement in fin.get(group, []):
            label = statement.get("period", {}).get("label", "?")
            with st.expander(f"{title} — {label} ({statement.get('source_file')})"):
                rows = [
                    {
                        "Line item": field.replace("_", " ").capitalize(),
                        "Amount (Rs)": f"{value['value']:,.0f}",
                        "Source": _citation(value),
                    }
                    for field, value in statement.items()
                    if isinstance(value, dict) and "citation" in value
                ]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    findings = record.get("verification", [])
    if findings:
        st.error("Arithmetic verification findings — no figure has been adjusted.")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Check": f["check"],
                        "Statement": f["statement"],
                        "Expected": f"{f['expected']:,.0f}",
                        "As stated": f"{f['actual']:,.0f}",
                        "Difference": f"{f['difference']:,.0f}",
                        "Severity": f["severity"].upper(),
                    }
                    for f in findings
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.success("All arithmetic consistency checks passed.")


def show_ratios(record: dict) -> None:
    st.subheader("Ratios")
    st.caption("Computed in Python, never by the model. Formulas shown so they can be checked by hand.")

    by_period = record.get("analysis", {}).get("by_period", {})
    rows = []
    for period in sorted(by_period):
        for r in by_period[period]:
            rows.append(
                {
                    "Ratio": r["name"],
                    "Period": period,
                    "Value": _display(r),
                    "Formula": r["formula"],
                    "Inputs used": ", ".join(
                        f"{k}={v:,.0f}" if isinstance(v, (int, float)) else f"{k}=n/a"
                        for k, v in r["inputs"].items()
                    ),
                    "Completeness": r["data_completeness"],
                }
            )
    for r in record.get("analysis", {}).get("bank", []):
        rows.append(
            {
                "Ratio": r["name"],
                "Period": r["period"],
                "Value": _display(r),
                "Formula": r["formula"],
                "Inputs used": "",
                "Completeness": r["data_completeness"],
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    trends = record.get("analysis", {}).get("trends", [])
    if trends:
        with st.expander("Trends"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Metric": t["metric"],
                            "Direction": t["direction"],
                            "Change %": None if t["change_pct"] is None else round(t["change_pct"], 1),
                            "Series": ", ".join(
                                f"{k}: {v:,.1f}" if v is not None else f"{k}: n/a"
                                for k, v in sorted(t["series"].items())
                            ),
                        }
                        for t in trends
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )


def _display(r: dict) -> str:
    if r["value"] is None:
        return "insufficient data"
    unit = r["unit"]
    if unit == "%":
        return f"{r['value']:.1f}%"
    if unit == "days":
        return f"{r['value']:.0f} days"
    if unit == "INR":
        return f"Rs {r['value']:,.0f}"
    if unit == "count":
        return f"{r['value']:.0f}"
    return f"{r['value']:.2f}x"


def show_flags(record: dict) -> None:
    st.subheader("Risk flags")
    flags = record.get("flags", [])
    if not flags:
        st.info("No risk rule fired on the computable ratios.")
    for f in flags:
        st.markdown(f"{SEVERITY_ICON.get(f['severity'], '⚪')} **{f['label']}** ({f['period']}) — {f['message']}")
    if record.get("narrative"):
        with st.expander("Narrative"):
            st.write(record["narrative"])
            st.write(record.get("trend_commentary", ""))


def show_outputs(case_id: str, record: dict) -> None:
    st.subheader("Memo")
    if record.get("review_findings"):
        st.warning("Reviewer flagged issues before delivery:")
        for f in record["review_findings"]:
            st.write("•", f)
    memo = api("GET", f"/cases/{case_id}/memo.docx")
    if memo.status_code == 200:
        st.download_button(
            "Download credit memo (.docx)",
            memo.content,
            file_name=f"credit_memo_{case_id}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    if record.get("escalations"):
        with st.expander("Open questions / escalations", expanded=True):
            for e in record["escalations"]:
                st.write("•", e)

    st.subheader("Ask about this file")
    question = st.text_input("Question", placeholder="What's driving the margin drop?")
    if st.button("Ask", disabled=not question):
        answer = api("POST", f"/cases/{case_id}/ask", json={"question": question}).json()
        st.write(answer["answer"])
        if not answer.get("answered_from_documents"):
            st.caption("Not found in the submitted documents.")
        for c in answer.get("citations", []):
            st.caption(c)


def main() -> None:
    header()

    cases = api("GET", "/cases").json().get("cases", [])
    options = {f"{c['borrower'] or 'unnamed'} — {c['id']}": c["id"] for c in cases}

    with st.sidebar:
        st.header("Cases")
        chosen = st.selectbox("Open an existing case", ["(new case)"] + list(options))

    case_id = None
    if chosen == "(new case)":
        case_id = upload_panel()
        if case_id:
            st.session_state["case_id"] = case_id
    else:
        st.session_state["case_id"] = options[chosen]

    case_id = st.session_state.get("case_id")
    if not case_id:
        return

    record = wait_for(case_id)
    show_progress(record)
    show_checklist(record)
    show_financials(record)
    show_ratios(record)
    show_flags(record)
    show_outputs(case_id, record)


main()
