#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# The evidence plan is fixed by the selected Fisher points. All company data
# enters through the provided LangChain sql_query tool, never through file I/O.
CHOSEN_POINTS: list[int] = [1, 5, 6]

EVIDENCE_SQL = """
SELECT
    a.r23 AS revenue_2023, a.r25 AS revenue_2025,
    b.c23 AS costs_2023, b.c25 AS costs_2025,
    100.0 * (a.r25 / a.r23 - 1.0) AS growth_pct,
    m.revenue_growth_2y_pct AS industry_growth_pct,
    100.0 * (a.r23 - b.c23) / a.r23 AS margin_2023_pct,
    100.0 * (a.r25 - b.c25) / a.r25 AS margin_2025_pct,
    m.operating_margin_pct AS industry_margin_pct,
    100.0 * ((a.r25 - b.c25) / a.r25
           - (a.r23 - b.c23) / a.r23) AS margin_change_pp,
    CASE WHEN 100.0 * (a.r25 / a.r23 - 1.0)
                   >= m.revenue_growth_2y_pct THEN 1 ELSE 0 END AS pass_1,
    CASE WHEN 100.0 * (a.r25 - b.c25) / a.r25
                   >= m.operating_margin_pct THEN 1 ELSE 0 END AS pass_5,
    CASE WHEN (a.r25 - b.c25) / a.r25
                   >= (a.r23 - b.c23) / a.r23 THEN 1 ELSE 0 END AS pass_6,
    a.months23 AS sales_months_2023, a.months25 AS sales_months_2025,
    b.months23 AS cost_months_2023, b.months25 AS cost_months_2025
FROM (
    SELECT
        1.0 * SUM(CASE WHEN substr(order_date, 1, 4) = '2023'
                      THEN amount ELSE 0 END) AS r23,
        1.0 * SUM(CASE WHEN substr(order_date, 1, 4) = '2025'
                      THEN amount ELSE 0 END) AS r25,
        COUNT(DISTINCT CASE WHEN substr(order_date, 1, 4) = '2023'
                            THEN substr(order_date, 1, 7) END) AS months23,
        COUNT(DISTINCT CASE WHEN substr(order_date, 1, 4) = '2025'
                            THEN substr(order_date, 1, 7) END) AS months25
    FROM orders
    WHERE order_date >= '2023-01-01' AND order_date < '2026-01-01'
) a
CROSS JOIN (
    SELECT
        SUM(CASE WHEN substr(month, 1, 4) = '2023'
                 THEN cost_of_sales + selling_admin + research_development
                 ELSE 0 END) AS c23,
        SUM(CASE WHEN substr(month, 1, 4) = '2025'
                 THEN cost_of_sales + selling_admin + research_development
                 ELSE 0 END) AS c25,
        COUNT(DISTINCT CASE WHEN substr(month, 1, 4) = '2023'
                            THEN month END) AS months23,
        COUNT(DISTINCT CASE WHEN substr(month, 1, 4) = '2025'
                            THEN month END) AS months25
    FROM monthly_costs
    WHERE month >= '2023-01' AND month < '2026-01'
) b
CROSS JOIN company co
JOIN industry_medians m ON m.industry = co.industry
"""

AUDIT_PROMPT = """You are an evidence-grounded Fisher analysis agent.
Use only the supplied SQL evidence; no memory, web research or invented facts.
All arithmetic and comparisons have already been performed in SQLite before
display rounding. The integer pass_1/pass_5/pass_6 flags are authoritative.
Apply these rules to the complete fiscal years, not partial-year 2026:
1: PASS if FY2023-to-FY2025 cumulative revenue growth is at least the company's
own industry median two-year growth. Historical growth is only a proxy for
future market potential, not a forecast or proof of sustained future demand.
5: PASS if FY2025 operating margin is at least the own-industry median margin.
Operating costs include cost of sales, selling/admin AND research/development.
6: PASS if FY2025 operating margin is at least FY2023 operating margin.
A company may pass point 5 and fail point 6, or vice versa: level and trend differ.
Reflect before answering: check all three points separately, verify direction
of each comparison, and avoid interpreting displayed rounding as exact data.
Return only JSON with this shape:
{{"points": {{"1": "PASS or FAIL", "5": "PASS or FAIL", "6": "PASS or FAIL"}},
  "interpretation": "One concise sentence interpreting the pattern of verdicts."}}
The interpretation must contain no numbers, no invented causes, no investment
recommendation, and no claims about management motives or future performance.
"""


def parse_evidence(raw: str) -> dict[str, float]:
    """Validate the tool's one-row table; never treat a tool error as evidence."""
    import math

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 2 or lines[0].startswith("SQL error:"):
        raise ValueError("Expected exactly one aggregated SQL evidence row")
    keys = [s.strip() for s in lines[0].split("|")]
    values = [s.strip() for s in lines[1].split("|")]
    if len(keys) != len(values) or len(set(keys)) != len(keys):
        raise ValueError("Malformed SQL evidence columns")
    result = dict(zip(keys, map(float, values)))
    required = {
        "revenue_2023", "revenue_2025", "costs_2023", "costs_2025",
        "growth_pct", "industry_growth_pct", "margin_2023_pct",
        "margin_2025_pct", "industry_margin_pct", "margin_change_pp",
        "pass_1", "pass_5", "pass_6", "sales_months_2023",
        "sales_months_2025", "cost_months_2023", "cost_months_2025",
    }
    if not required.issubset(result) or not all(map(math.isfinite, result.values())):
        raise ValueError("Missing or non-finite SQL evidence")
    if result["revenue_2023"] <= 0 or result["revenue_2025"] <= 0:
        raise ValueError("Revenue denominator must be positive")
    if any(result[k] != 12 for k in required if "months" in k):
        raise ValueError("Expected twelve months of sales and costs in each fiscal year")
    if any(result[f"pass_{p}"] not in (0, 1) for p in CHOSEN_POINTS):
        raise ValueError("Invalid SQL comparison flag")
    return result


def evidence_summary(e: dict[str, float], points: dict[int, str]) -> str:
    """Keep the numerical explanation faithful even if the model is unavailable."""
    return (
        f"P1 {points[1]}: FY2023-FY2025 revenue growth {e['growth_pct']:.2f}% "
        f"versus industry {e['industry_growth_pct']:.2f}%. "
        f"P5 {points[5]}: FY2025 operating margin {e['margin_2025_pct']:.2f}% "
        f"versus industry {e['industry_margin_pct']:.2f}%. "
        f"P6 {points[6]}: operating margin changed from "
        f"{e['margin_2023_pct']:.2f}% to {e['margin_2025_pct']:.2f}% "
        f"({e['margin_change_pp']:+.2f} percentage points)."
    )


def validate_audit(value: Any, points: dict[int, str]) -> str:
    """A model's interpretation is accepted only alongside matching verdicts."""
    if not isinstance(value, dict):
        raise ValueError("Model response is not an object")
    if value.get("points") != {str(p): v for p, v in points.items()}:
        raise ValueError("Model verdicts disagree with the SQL rules")
    interpretation = value.get("interpretation")
    if not isinstance(interpretation, str) or not interpretation.strip():
        raise ValueError("Missing model interpretation")
    if len(interpretation) > 500 or any(c.isdigit() for c in interpretation):
        raise ValueError("Interpretation must be brief and contain no numbers")
    return " ".join(interpretation.split())


def build_agent() -> Any:
    """Create one LangChain model chain, shared across company workflows."""
    import os
    import sys
    from langchain_core.output_parsers import JsonOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_deepseek import ChatDeepSeek

    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        print("No DEEPSEEK_API_KEY: running SQL rules only; model audit NOT run.",
              file=sys.stderr)
        return {"chain": None, "status": "no API key; model audit not run"}
    try:
        model = ChatDeepSeek(
            model=MODEL_NAME, temperature=0, max_tokens=1000,
            timeout=40, max_retries=1,
        )
        prompt = ChatPromptTemplate.from_messages([
            ("system", AUDIT_PROMPT),
            ("human", "SQL evidence:\n{evidence}\n{feedback}"),
        ])
        return {"chain": prompt | model | JsonOutputParser(), "status": "ready"}
    except Exception as exc:
        # Do not print exception bodies: provider errors can contain credentials.
        return {"chain": None, "status": f"model setup failed ({type(exc).__name__})"}


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Route evidence, apply rules, audit with the LLM, validate, and summarize."""
    semaphore = asyncio.Semaphore(4)

    async def analyze_one(name: str, tools: list[Any]) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                # Selecting by tool name avoids coupling to a reordered tool list.
                sql = next(t for t in tools if t.name == "sql_query")
                raw = await asyncio.wait_for(sql.ainvoke({"query": EVIDENCE_SQL}), 20)
                evidence = parse_evidence(raw)
                points = {p: "PASS" if evidence[f"pass_{p}"] else "FAIL"
                          for p in CHOSEN_POINTS}
                summary = evidence_summary(evidence, points)
            except Exception as exc:
                # Missing evidence is not fabricated into a PASS or FAIL.
                return name, {"points": {}, "summary":
                              f"Evidence unavailable ({type(exc).__name__}); no verdicts issued."}

            chain = agent["chain"]
            if chain is None:
                return name, {"points": points, "summary": summary +
                              f" [SQL-only fallback: {agent['status']}]."}

            feedback = ""
            failure = "unknown error"
            for attempt in range(2):
                try:
                    response = await asyncio.wait_for(
                        chain.ainvoke({"evidence": raw, "feedback": feedback}),
                        timeout=60,
                    )
                    interpretation = validate_audit(response, points)
                    return name, {"points": points,
                                  "summary": summary + " " + interpretation}
                except Exception as exc:
                    failure = type(exc).__name__
                    # One bounded repair call, using the original evidence again.
                    feedback = (
                        "The prior response failed validation or the API failed. "
                        "Return valid JSON, match the SQL pass flags exactly, "
                        "and provide one short interpretation without numbers."
                    )
            return name, {"points": points, "summary": summary +
                          f" [SQL-only fallback: model audit failed ({failure})]."}

    results = await asyncio.gather(*(analyze_one(n, ts) for n, ts in companies.items()))
    return dict(results)


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())
