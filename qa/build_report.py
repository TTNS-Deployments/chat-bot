"""Build qa/test_cases.md, qa/TEST_REPORT.md and qa/test_report.xlsx from qa/tests/catalog.py and the
results written by the pytest run (qa/results/*.json). Run via qa/run_all.sh."""
from __future__ import annotations

import datetime as dt
import importlib.metadata as md
import json
import platform
import subprocess
import sys
from pathlib import Path

QA = Path(__file__).resolve().parent
REPO = QA.parent
sys.path.insert(0, str(QA / "tests"))
from catalog import DEFECTS, OBSERVATIONS, OUT_OF_SCOPE, TESTS  # noqa: E402

STATUSES = ["Pass", "Fail", "Blocked", "Not Tested"]
LAYERS = ["Backend", "UI", "Front end"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]


def sh(*cmd, cwd=REPO):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:
        return "n/a"


def environment():
    def v(p):
        try:
            return md.version(p)
        except md.PackageNotFoundError:
            return "n/a"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            chromium = b.version
            b.close()
    except Exception as exc:
        chromium = f"n/a ({exc})"
    return {
        "Date": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Repository": "chat-bot", "Branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "Commit": sh("git", "log", "-1", "--format=%h %s"),
        "Working tree (outside qa/)": sh("git", "status", "--porcelain", "--", ".", ":(exclude)qa") or "clean",
        "OS": platform.platform(), "Python": platform.python_version(),
        "Backend": f"FastAPI {v('fastapi')}, Starlette {v('starlette')}, Pydantic {v('pydantic')}, Uvicorn {v('uvicorn')}, openai {v('openai')}",
        "Frontend": "Plain HTML/CSS/JavaScript (no build step)",
        "Test tools": f"pytest {v('pytest')}, Playwright {v('playwright')} + Chromium {chromium}, axe-core 4.10.2, httpx {v('httpx')}, openpyxl {v('openpyxl')}",
        "Node / Docker": f"{sh('node', '--version')} / {sh('docker', '--version')}",
        "Mode": "MOCK/LOCAL: CHATBOT_MODE=local (keyword retriever + extractive stand-in for the LLM); failure tests use the real openai SDK with a mocked "
                "network. No live Azure OpenAI/AI Search calls were made (no credentials; would cost money).",
    }


def load_results():
    res = {}
    for f in sorted((QA / "results").glob("*.json")):
        for e in json.loads(f.read_text()):
            res[e["id"]] = e
    return res


def rows():
    results = load_results()
    defect_of = {}
    for d in DEFECTS:
        for tid in d[7].replace(",", " ").split():
            if "-" in tid and tid[:2] in ("BE", "UI", "FE"):
                defect_of.setdefault(tid, d[0])
    out = []
    for tid, layer, prio, area, steps, expected, script in TESTS:
        r = results.get(tid)
        if r is None:
            status, actual, evidence = "Not Tested", "Not run in the last execution.", ""
        else:
            status = r["status"]
            actual = r["props"].get("actual") or r["message"] or ""
            if status == "Fail" and r["message"] and r["message"] not in actual:
                actual += f"\nAssertion: {r['message'].splitlines()[0][:300]}"
            if status in ("Blocked", "Not Tested") and r["message"]:
                actual = r["message"] + ("\n" + r["props"]["actual"] if r["props"].get("actual") else "")
            nodes = " ".join(f"qa/{n}" if not n.startswith("qa/") else n for n in r["tests"][:1])
            exit_code = {"Pass": 0, "Fail": 1}.get(status, "skipped (5 if run alone)")
            evidence = f"$ .venv/bin/python -m pytest '{nodes.split('[')[0]}'  → exit {exit_code}"
            if r["props"].get("evidence"):
                evidence += f"\n{r['props']['evidence']}"
        out.append(dict(id=tid, layer=layer, priority=prio, area=area, steps=steps, expected=expected, actual=actual.strip(),
                        status=status, evidence=evidence, defect=defect_of.get(tid, ""), script=script))
    return out


def counts(data, key, values):
    return {v: {s: sum(1 for r in data if r[key] == v and r["status"] == s) for s in STATUSES} for v in values}


def verdict(data):
    ch = [r for r in data if r["priority"] in ("Critical", "High")]
    ch_fail = [r for r in ch if r["status"] == "Fail"]
    ch_open = [r for r in ch if r["status"] != "Pass"]
    crit_fail = [r for r in data if r["priority"] == "Critical" and r["status"] == "Fail"]
    fails = [r for r in data if r["status"] == "Fail"]
    if crit_fail or len(ch_fail) > 3:
        v = "Not working"
    elif ch_open or fails:
        v = "Works with issues"
    else:
        v = "Works as expected"
    why = (f"{len(ch) - len(ch_open)} of {len(ch)} Critical/High tests passed. {len(ch_fail)} Critical/High failed and "
           f"{sum(r['status'] == 'Blocked' for r in ch)} are Blocked (they need a live Azure OpenAI model: context recall, grounding quality, prompt-injection "
           f"resistance), so the 'Works as expected' rule (all Critical and High must pass) is not met. The core chat flow, validation, error handling, auth gate, "
           f"uploads, XSS protection, responsiveness and accessibility all work in local/mock mode. {len(fails)} lower-priority tests failed, giving "
           f"{len(DEFECTS)} defects ({', '.join(f'{sum(d[1] == sev for d in DEFECTS)} {sev}' for sev in ('Critical', 'High', 'Medium', 'Low') if any(d[1] == sev for d in DEFECTS))}): token not revoked on logout, rate limit/lockout bypass via X-Forwarded-For, sign-in turned off by an "
           f"AUTH_MODE typo, and a typed question lost while waiting.")
    return v, why


# ------------------------------------------------------------------ Markdown
def esc(s):
    return str(s).replace("|", "\\|").replace("\n", "<br>")


def write_test_cases_md():
    lines = ["# Test cases – AI Based Chatbot", "",
             "Built from what the code actually contains (see `00_discovery.md`). Each test maps to one pytest function `test_<ID>_…` in the script shown. "
             "Run them all with `qa/run_all.sh`.", "",
             "| ID | Layer | Priority | Area | Steps | Expected result | Script |", "|---|---|---|---|---|---|---|"]
    for t in TESTS:
        lines.append("| " + " | ".join(esc(x) for x in t[:6]) + f" | `qa/{t[6]}` |")
    lines += ["", "## Out of scope", "", "| Item | Reason |", "|---|---|"] + [f"| {esc(a)} | {esc(b)} |" for a, b in OUT_OF_SCOPE]
    (QA / "test_cases.md").write_text("\n".join(lines) + "\n")


def write_report_md(env, data):
    v, why = verdict(data)
    by_layer, by_prio = counts(data, "layer", LAYERS), counts(data, "priority", PRIORITIES)
    tot = {s: sum(r["status"] == s for r in data) for s in STATUSES}
    n = len(data)
    executed = tot["Pass"] + tot["Fail"]
    L = ["# Test report – AI Based Chatbot", "", f"## Verdict: **{v}**", "", why, "",
         "## Environment", "", "| Item | Value |", "|---|---|"] + [f"| {k} | {esc(x)} |" for k, x in env.items()]
    L += ["", "## Results", "", "| Layer | Pass | Fail | Blocked | Not Tested | Total |", "|---|---|---|---|---|---|"]
    for k, c in by_layer.items():
        L.append(f"| {k} | " + " | ".join(str(c[s]) for s in STATUSES) + f" | {sum(c.values())} |")
    L.append("| **Total** | " + " | ".join(f"**{tot[s]}**" for s in STATUSES) + f" | **{n}** |")
    L += ["", "| Priority | Pass | Fail | Blocked | Not Tested | Total |", "|---|---|---|---|---|---|"]
    for k, c in by_prio.items():
        L.append(f"| {k} | " + " | ".join(str(c[s]) for s in STATUSES) + f" | {sum(c.values())} |")
    L += ["", f"Pass rate: **{tot['Pass'] / n:.1%}** of all {n} tests; **{tot['Pass'] / executed:.1%}** of the {executed} that ran (Pass + Fail).", ""]
    L += ["## Failed tests", "", "| ID | Priority | Area | Reason | Defect |", "|---|---|---|---|---|"]
    for r in data:
        if r["status"] == "Fail":
            L.append(f"| {r['id']} | {r['priority']} | {esc(r['area'])} | {esc(r['actual'].splitlines()[0][:220])} | {r['defect']} |")
    L += ["", "## Defects", ""]
    for d in DEFECTS:
        L += [f"### {d[0]} ({d[1]}, {d[2]}): {d[3]}", "", f"- **Steps:** {d[4]}", f"- **Expected:** {d[5]}", f"- **Actual:** {d[6]}",
              f"- **Affected tests:** {d[7]}", f"- **Likely cause (analysis only, no code changed):** {d[8]}", ""]
    L += ["## Blocked / Not tested", "", "| ID | Priority | Area | Reason |", "|---|---|---|---|"]
    for r in data:
        if r["status"] in ("Blocked", "Not Tested"):
            L.append(f"| {r['id']} | {r['priority']} | {esc(r['area'])} | {esc(r['actual'].splitlines()[0])} |")
    L += ["", "### Out of scope", "", "| Item | Reason |", "|---|---|"] + [f"| {esc(a)} | {esc(b)} |" for a, b in OUT_OF_SCOPE]
    L += ["", "## Observations (not defects)", ""] + [f"- {o}" for o in OBSERVATIONS]
    L += ["", "## All test results", "", "| ID | Layer | Priority | Area | Expected | Actual | Status | Evidence |", "|---|---|---|---|---|---|---|---|"]
    for r in data:
        L.append(f"| {r['id']} | {r['layer']} | {r['priority']} | {esc(r['area'])} | {esc(r['expected'])} | {esc(r['actual'][:700])} | **{r['status']}** | {esc(r['evidence'])} |")
    L += ["", "## How to reproduce", "", "```bash", "qa/run_all.sh        # runs every test in qa/tests and rebuilds these reports (~5 min, includes the 3-minute BE-037 hang test)",
          "```", "", "Raw evidence: `qa/evidence/pytest_run.txt` (full console log), `qa/evidence/junit.xml`, `qa/evidence/BE-001_existing_pytest.txt`, `qa/results/*.json`."]
    (QA / "TEST_REPORT.md").write_text("\n".join(L) + "\n")
    return v, why, tot


# ------------------------------------------------------------------ Excel
def write_xlsx(env, data, v, why):
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    headfill = PatternFill("solid", fgColor="1F5FBF")
    wrap = Alignment(wrap_text=True, vertical="top")
    fills = {"Pass": "C6EFCE", "Fail": "FFC7CE", "Blocked": "FFEB9C", "Not Tested": "D9D9D9"}

    def table(ws, header, body, widths):
        ws.append(header)
        for c in ws[ws.max_row]:
            c.font, c.fill = head, headfill
        hdr = ws.max_row
        for row in body:
            ws.append(row)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=hdr + 1):
            for c in row:
                c.alignment = wrap
        ws.freeze_panes = ws.cell(row=hdr + 1, column=1)
        ws.auto_filter.ref = f"A{hdr}:{get_column_letter(len(header))}{ws.max_row}"

    # Test Cases
    tc = wb.active
    tc.title = "Test Cases"
    table(tc, ["ID", "Layer", "Priority", "Area", "Steps", "Expected Result", "Actual Result", "Status", "Evidence", "Defect"],
          [[r["id"], r["layer"], r["priority"], r["area"], r["steps"], r["expected"], r["actual"][:3000], r["status"], r["evidence"], r["defect"]] for r in data],
          [9, 10, 10, 24, 50, 45, 70, 11, 55, 9])
    last = tc.max_row
    for s, color in fills.items():
        tc.conditional_formatting.add(f"H2:H{last}", CellIsRule(operator="equal", formula=[f'"{s}"'], fill=PatternFill("solid", fgColor=color)))
    dv = DataValidation(type="list", formula1='"Pass,Fail,Blocked,Not Tested"', allow_blank=False)
    tc.add_data_validation(dv)
    dv.add(f"H2:H{last}")

    # Summary
    sm = wb.create_sheet("Summary", 0)
    sm["A1"] = "AI Based Chatbot – QA Test Report"
    sm["A1"].font = Font(bold=True, size=16)
    r = 3
    sm.cell(r, 1, "Environment").font = Font(bold=True, size=12)
    for k, val in env.items():
        r += 1
        sm.cell(r, 1, k).font = Font(bold=True)
        sm.cell(r, 2, val).alignment = wrap
    rng = lambda col: f"'Test Cases'!${col}$2:${col}${last}"

    def count_block(r, title, key_col, values):
        r += 2
        sm.cell(r, 1, title).font = Font(bold=True, size=12)
        r += 1
        for i, h in enumerate([title.split()[-1].capitalize(), *STATUSES, "Total", "Pass rate"], 1):
            c = sm.cell(r, i, h)
            c.font, c.fill = head, headfill
        first = r + 1
        for val in values:
            r += 1
            sm.cell(r, 1, val)
            for i, s in enumerate(STATUSES, 2):
                sm.cell(r, i, f'=COUNTIFS({rng(key_col)},$A{r},{rng("H")},"{s}")')
            sm.cell(r, 6, f"=SUM(B{r}:E{r})")
            sm.cell(r, 7, f"=IF(F{r}=0,0,B{r}/F{r})").number_format = "0.0%"
        r += 1
        sm.cell(r, 1, "Total").font = Font(bold=True)
        for i in range(2, 7):
            col = get_column_letter(i)
            sm.cell(r, i, f"=SUM({col}{first}:{col}{r - 1})").font = Font(bold=True)
        sm.cell(r, 7, f"=IF(F{r}=0,0,B{r}/F{r})").number_format = "0.0%"
        return r

    r = count_block(r, "Results by layer", "B", LAYERS)
    total_row = r
    r = count_block(r, "Results by priority", "C", PRIORITIES)
    r += 2
    sm.cell(r, 1, "Pass rate (all tests)").font = Font(bold=True)
    sm.cell(r, 2, f"=B{total_row}/F{total_row}").number_format = "0.0%"
    r += 1
    sm.cell(r, 1, "Pass rate (executed: Pass+Fail)").font = Font(bold=True)
    sm.cell(r, 2, f"=B{total_row}/(B{total_row}+C{total_row})").number_format = "0.0%"
    r += 1
    sm.cell(r, 1, "Critical/High not passing").font = Font(bold=True)
    sm.cell(r, 2, f'=COUNTIFS({rng("C")},"Critical",{rng("H")},"<>Pass")+COUNTIFS({rng("C")},"High",{rng("H")},"<>Pass")')
    r += 1
    sm.cell(r, 1, "Rule check").font = Font(bold=True)
    sm.cell(r, 2, f'=IF(B{r - 1}=0,"All Critical and High passed","Not all Critical/High passed: Works as expected is not possible")')
    r += 2
    sm.cell(r, 1, "VERDICT").font = Font(bold=True, size=14)
    c = sm.cell(r, 2, v)
    c.font = Font(bold=True, size=14, color="9C5700")
    r += 1
    sm.cell(r, 1, "Justification").font = Font(bold=True)
    sm.cell(r, 2, why).alignment = wrap
    sm.row_dimensions[r].height = 120
    sm.column_dimensions["A"].width = 30
    sm.column_dimensions["B"].width = 95
    for col in "CDEFG":
        sm.column_dimensions[col].width = 12

    # Defects
    de = wb.create_sheet("Defects")
    table(de, ["ID", "Severity", "Layer", "Title", "Steps to reproduce", "Expected vs Actual", "Affected test IDs", "Likely cause (analysis only)"],
          [[d[0], d[1], d[2], d[3], d[4], f"Expected: {d[5]}\nActual: {d[6]}", d[7], d[8]] for d in DEFECTS],
          [8, 10, 10, 40, 50, 60, 18, 70])

    # Not tested / out of scope  ('/' is not allowed in sheet names)
    nt = wb.create_sheet("Not Tested & Out of Scope")
    body = [[x["id"], x["status"], x["priority"], x["area"], x["actual"].splitlines()[0]] for x in data if x["status"] in ("Blocked", "Not Tested")]
    body += [["—", "Out of scope", "—", a, b] for a, b in OUT_OF_SCOPE]
    body += [["—", "Observation", "—", "Note", o] for o in OBSERVATIONS]
    table(nt, ["ID", "Type / Status", "Priority", "Item", "Reason"], body, [9, 14, 10, 45, 100])

    wb.save(QA / "test_report.xlsx")


def main():
    env = environment()
    data = rows()
    write_test_cases_md()
    v, why, tot = write_report_md(env, data)
    write_xlsx(env, data, v, why)
    print(f"VERDICT: {v}")
    print("Totals: " + ", ".join(f"{s} {tot[s]}" for s in STATUSES) + f" (of {len(data)})")
    for r in data:
        if r["status"] == "Fail":
            print(f"  FAIL {r['id']} [{r['priority']}] {r['area']}: {r['actual'].splitlines()[0][:160]}")


if __name__ == "__main__":
    main()
