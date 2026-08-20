#!/usr/bin/env python3
"""Behavioural regression suite for a deployed analytics-mcp server.

Every assertion here encodes a behaviour that was established by hand, and that
a later change could silently undo. Manual verification in a chat window does
not survive the chat window; this does.

    python3 tests/regression.py                          # against localhost:8000
    python3 tests/regression.py --url http://host:8010
    python3 tests/regression.py --slow                   # include timeout tests

Exit code 0 = all pass, 1 = at least one regression.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


# --------------------------------------------------------------------------- io
def _post(url: str, payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url + "/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def call(url: str, tool: str, args: dict | None = None, timeout: int = 180) -> tuple[str, bool]:
    """Returns (text, is_error)."""
    d = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": tool, "arguments": args or {}}}, timeout)
    res = d.get("result") or {}
    if "error" in d:
        return json.dumps(d["error"]), True
    text = (res.get("content") or [{}])[0].get("text", "")
    return text, bool(res.get("isError"))


def tools(url: str) -> list[dict]:
    return _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]


def get(url: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url + path, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---------------------------------------------------------------- assertions
def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED.append(name) if ok else FAILED.append((name, detail)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--slow", action="store_true", help="include timeout-override tests")
    a = ap.parse_args()
    url = a.url.rstrip("/")
    print(f"\nanalytics-mcp regression suite → {url}\n")

    # ---- contract surface -------------------------------------------------
    print("contract")
    tl = tools(url)
    names = {t["name"] for t in tl}
    for required in ("execute_sql", "explain_query", "list_views", "describe_view"):
        check(f"tool present: {required}", required in names, f"have: {sorted(names)}")

    es = next((t for t in tl if t["name"] == "execute_sql"), {})
    params = set((es.get("inputSchema") or {}).get("properties", {}))
    check("execute_sql accepts limit/offset/timeout_ms",
          {"limit", "offset", "timeout_ms"} <= params, f"params: {sorted(params)}")

    lv, err = call(url, "list_views")
    check("list_views announces CONTRACT VERSION", "CONTRACT VERSION:" in lv and not err)
    check("list_views reports DATA FRESHNESS", "DATA FRESHNESS:" in lv)
    check("list_views reports OBJECT KINDS", "OBJECT KINDS:" in lv)
    check("schema block is generated (enums present)", "ENUM VALUES" in lv)

    # ---- SQL that must work ----------------------------------------------
    print("\nsql capability")
    txt, err = call(url, "execute_sql", {"sql": "select 1 as ok"})
    check("simple select", not err and '"ok": 1' in txt, txt[:160])

    txt, err = call(url, "execute_sql", {
        "sql": "select percentile_cont(0.5) within group (order by amount) as median "
               "from donations where donated_at >= now() - interval '3 months'"})
    check("percentile_cont / median", not err and "median" in txt, txt[:160])

    txt, err = call(url, "execute_sql", {
        "sql": "select now() at time zone 'UTC' as t"})
    check("AT TIME ZONE is not blocked", not err and '"t"' in txt, txt[:160])

    txt, err = call(url, "explain_query", {
        "sql": "select count(*) from donations where donated_at > now() - interval '30 days'"})
    check("explain_query returns a plan with costs", not err and "cost=" in txt, txt[:160])

    # ---- truncation honesty ----------------------------------------------
    print("\ntruncation and paging")
    txt, _ = call(url, "execute_sql", {"sql": "select id from donations"})
    check("truncation reports rows_returned", "rows_returned=" in txt, txt[-200:])
    check("truncation reports rows_total as a lower bound", "rows_total>=" in txt, txt[-200:])
    check("truncation says TRUNCATED", "TRUNCATED" in txt, txt[-200:])

    txt, _ = call(url, "execute_sql", {"sql": "select name from treasures"})
    check("complete result is labelled complete",
          "complete" in txt and "rows_total>=" not in txt, txt[-200:])

    txt, _ = call(url, "execute_sql", {"sql": "select id from donations", "limit": 2})
    check("explicit limit still detects more rows", "rows_total>=" in txt, txt[-200:])

    txt, _ = call(url, "execute_sql", {"sql": "select id from donations", "limit": 2})
    check("unordered query refuses to recommend offset paging",
          "add a total ORDER BY" in txt and "page with offset=" not in txt, txt[-260:])

    txt, _ = call(url, "execute_sql",
                  {"sql": "select id from donations order by donated_at desc", "limit": 2})
    check("ordered query offers offset AND states the totality caveat",
          "offset=" in txt and "total" in txt, txt[-260:])

    # ---- the boundary: writes and PII ------------------------------------
    print("\nprivacy and write boundary")
    # Two independent guards, and which one fires depends on the view's shape.
    # Simple views hit the role's missing grant; views with joins are not
    # auto-updatable and are rejected earlier. Assert the guarantee (refused),
    # then assert the permission guard specifically where it is reachable.
    for stmt in ("update donations set amount = 0 where false",
                 "delete from donations where false",
                 "insert into treasures (name) values ('probe')"):
        txt, err = call(url, "execute_sql", {"sql": stmt})
        check(f"write refused: {stmt.split()[0].upper()} {stmt.split()[1]}", err, txt[:160])

    txt, err = call(url, "execute_sql", {"sql": "update customers set city = 'x' where false"})
    check("role grant denies writes on a directly-updatable view",
          err and "permission denied" in txt.lower(), txt[:160])

    txt, err = call(url, "execute_sql", {"sql": "create table analytics.regression_probe (i int)"})
    check("DDL is refused (read-only transaction)",
          err and "read-only" in txt.lower(), txt[:160])

    txt, err = call(url, "execute_sql", {"sql": "select count(*) from public.donations"})
    check("public schema is unreachable", err and "permission denied" in txt.lower(), txt[:160])

    txt, err = call(url, "execute_sql", {"sql": "select display_name from customers limit 1"})
    check("PII column is absent", err and "does not exist" in txt.lower(), txt[:160])

    check("error includes the real column list",
          "Columns actually available" in txt or "Objects available" in txt, txt[:200])

    code, body = get(url, "/selftest")
    try:
        st = json.loads(body)
        check(f"/selftest passes ({st.get('checked')} assertions)",
              code == 200 and st.get("pass") is True,
              "; ".join(a["sql"] for a in st.get("assertions", []) if not a["pass"]))
    except json.JSONDecodeError:
        check("/selftest passes", False, body[:160])

    # ---- derived-tool stability ------------------------------------------
    print("\nderived tools")
    seen = {}
    for months in (6, 12):
        txt, err = call(url, "normal_daily_average", {"months": months})
        row = next((l for l in txt.splitlines() if '"2026-03"' in l), None)
        if row:
            seen[months] = json.loads(row)
    if len(seen) == 2:
        check("normal_daily_average is stable across window sizes",
              seen[6]["normal_daily_avg"] == seen[12]["normal_daily_avg"],
              f"months=6 → {seen[6]['normal_daily_avg']}, months=12 → {seen[12]['normal_daily_avg']}")
        check("normal_daily_average reports its baseline window",
              "threshold_baseline_from" in seen[6], str(seen[6])[:160])
    else:
        check("normal_daily_average is stable across window sizes", False,
              "reference month not present in output")

    txt, err = call(url, "monthly_trend", {"months": 999})
    check("query-tool params are bounds-checked", err and "must be <=" in txt, txt[:160])

    # ---- timeout override (slow) -----------------------------------------
    if a.slow:
        print("\ntimeout override (slow)")
        t0 = time.time()
        txt, err = call(url, "execute_sql", {"sql": "select pg_sleep(2) as s", "timeout_ms": 1000})
        check("timeout_ms below query duration cancels it",
              err and "timeout" in txt.lower(), f"{time.time()-t0:.1f}s {txt[:120]}")

        txt, err = call(url, "execute_sql", {"sql": "select pg_sleep(2) as s"})
        check("same query succeeds under the default ceiling", not err, txt[:120])

        txt, err = call(url, "execute_sql",
                        {"sql": "select current_setting('statement_timeout') as t"})
        check("override does not leak onto the pooled connection",
              not err and '"20s"' in txt, txt[:120])

    # ---- report -----------------------------------------------------------
    total = len(PASSED) + len(FAILED)
    print(f"\n{'-' * 62}\n  {len(PASSED)}/{total} passed")
    if FAILED:
        print("\n  REGRESSIONS:")
        for name, detail in FAILED:
            print(f"    - {name}\n        {detail}")
        return 1
    print("  no regressions\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
