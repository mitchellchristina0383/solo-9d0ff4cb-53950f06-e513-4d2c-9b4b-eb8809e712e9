"""End-to-end self-test for the Dependency Alert API.

Runs entirely on the Python standard library so it works inside the slim
Docker container as well as on a host.  Point it at a running server:

    python tests/selftest.py            # default http://127.0.0.1:8000
    BASE_URL=http://host:8000 python tests/selftest.py

Exits non-zero on any failed check.  Covers graph validation/replacement,
report sequence semantics, suppression rules, recovery reclassification and
key concurrent interleavings.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def reset() -> None:
    status, _ = request("POST", "/admin/reset")
    assert status == 200, status


def alerts_partition_consistent(body: dict) -> bool:
    """Every node is in exactly one class; down == actionable | suppressed."""
    nodes = set(body["services"])
    down = set(body["down_services"])
    unrep = set(body["unreported_services"])
    healthy = set(body["healthy_services"])
    expired = {e["service"] for e in body["expired_services"]}
    groups = (down, unrep, healthy, expired)
    if set().union(*groups) != nodes:
        return False
    if sum(len(g) for g in groups) != len(nodes):  # pairwise disjoint
        return False
    action = {f["service"] for f in body["actionable_faults"]}
    supp = {f["service"] for f in body["suppressed_faults"]}
    if action | supp != down or action & supp:
        return False
    # Suppression paths may only traverse currently-down services.
    for fault in body["suppressed_faults"]:
        for path in fault["paths"]:
            if not set(path) <= down:
                return False
    return True


# ---------------------------------------------------------------------------
# Test graphs
# ---------------------------------------------------------------------------

# A -> B -> D, A -> C -> D, plus X -> Y  (edges stored as "downstream deps on upstream")
GRAPH_V1 = {
    "version": "v1",
    "services": [
        {"name": "A", "dependencies": ["B", "C"]},
        {"name": "B", "dependencies": ["D"]},
        {"name": "C", "dependencies": ["D"]},
        {"name": "D", "dependencies": []},
        {"name": "X", "dependencies": ["Y"]},
        {"name": "Y", "dependencies": []},
    ],
}

# v2: same A/B/C/D topology (states preserved), Y removed, Z added (X->Z)
GRAPH_V2 = {
    "version": "v2",
    "services": [
        {"name": "A", "dependencies": ["B", "C"]},
        {"name": "B", "dependencies": ["D"]},
        {"name": "C", "dependencies": ["D"]},
        {"name": "D", "dependencies": []},
        {"name": "X", "dependencies": ["Z"]},
        {"name": "Z", "dependencies": []},
    ],
}

CYCLIC_GRAPH = {
    "services": [
        {"name": "A", "dependencies": ["B"]},
        {"name": "B", "dependencies": ["C"]},
        {"name": "C", "dependencies": ["A"]},
    ]
}

UNKNOWN_REF_GRAPH_BAD = {
    "services": [{"name": "A", "dependencies": ["GHOST"]}]
}


def test_graph_validation() -> None:
    print("[T1] graph validation & atomic replacement")
    reset()

    status, body = request("GET", "/alerts")
    check("alerts before any graph -> 409", status == 409, str(body))

    status, body = request("POST", "/graph", CYCLIC_GRAPH)
    check("cyclic graph rejected 422", status == 422 and body["error"] == "invalid_graph", str(body))

    status, body = request("POST", "/graph", UNKNOWN_REF_GRAPH_BAD)
    check("unknown reference rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", {
        "services": [
            {"name": "A", "dependencies": ["B"]},
            {"name": "A", "dependencies": []},
            {"name": "B", "dependencies": []},
        ]
    })
    check("duplicate service rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", {
        "services": [
            {"name": "A", "dependencies": ["B", "B"]},
            {"name": "B", "dependencies": []},
        ]
    })
    check("duplicate edge rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", {
        "services": [{"name": "A", "dependencies": ["A"]}]
    })
    check("self dependency rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", GRAPH_V1)
    check("valid graph accepted", status == 200 and body["revision"] == 1, str(body))
    check("edge count correct", body["edge_count"] == 5, str(body))

    # Rejected replacement must keep the old version.
    status, _ = request("POST", "/graph", CYCLIC_GRAPH)
    status, body = request("GET", "/graph")
    check(
        "old graph retained after rejected replacement",
        status == 200 and body["revision"] == 1 and body["version"] == "v1",
        str(body),
    )

    status, body = request("POST", "/graph", GRAPH_V1)
    check("re-commit increments revision", status == 200 and body["revision"] == 2, str(body))


def test_report_semantics() -> None:
    print("[T2] report sequence semantics")
    reset()
    request("POST", "/graph", GRAPH_V1)

    status, body = request("POST", "/reports", {"service": "NOPE", "seq": 1, "status": "fault"})
    check("unknown service report -> 404", status == 404, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 0, "status": "fault"})
    check("non-positive seq -> 422", status == 422, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    check("first fault applied", status == 200 and body["applied"] is True, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    check(
        "same seq same content idempotent retry",
        status == 200 and body["applied"] is False and body["duplicate"] is True,
        str(body),
    )

    status, body = request("POST", "/reports", {"service": "D", "seq": 1, "status": "recover"})
    check("same seq different content -> 409", status == 409, str(body))

    # State must not have been changed by the conflicting retry.
    status, alerts = request("GET", "/alerts")
    d_down = "D" in alerts["down_services"]
    check("conflict did not change state", d_down, str(alerts))

    status, body = request("POST", "/reports", {"service": "D", "seq": 3, "status": "recover"})
    check("higher seq applies (gaps allowed)", status == 200 and body["applied"] is True, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 2, "status": "fault"})
    check("late lower seq -> 409", status == 409, str(body))

    status, alerts = request("GET", "/alerts")
    check("late report never rolls state back", "D" not in alerts["down_services"], str(alerts))

    status, body = request("POST", "/reports", {"service": "D", "seq": 3, "status": "fault"})
    check("retry of old seq after newer state -> 409", status == 409, str(body))


def test_suppression() -> None:
    print("[T3] suppression: only all-faulty paths suppress; healthy/unreported cut paths")
    reset()
    request("POST", "/graph", GRAPH_V1)

    def fault(svc: str, seq: int):
        return request("POST", "/reports", {"service": svc, "seq": seq, "status": "fault"})

    fault("D", 1)
    fault("B", 1)
    fault("A", 1)

    status, alerts = request("GET", "/alerts")
    actionable = [f["service"] for f in alerts["actionable_faults"]]
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check("root D is actionable", actionable == ["D"], str(alerts))
    check("B suppressed by D", "B" in suppressed and suppressed["B"]["root_causes"] == ["D"], str(alerts))
    check(
        "A suppressed only via all-down B-D path (C healthy cuts the other branch)",
        "A" in suppressed and suppressed["A"]["root_causes"] == ["D"],
        str(alerts),
    )
    check(
        "A path list only contains all-down paths",
        suppressed["A"]["paths"] == [["A", "B", "D"]],
        str(suppressed["A"]),
    )

    # C becomes faulty too: second all-down path opens.
    fault("C", 1)
    _, alerts = request("GET", "/alerts")
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check(
        "A gains second all-down path A-C-D",
        suppressed["A"]["paths"] == [["A", "B", "D"], ["A", "C", "D"]],
        str(suppressed["A"]),
    )

    # B recovers: A must stay suppressed via C, B becomes actionable if still down?
    # B is up now -> A's B-branch is cut, C-branch still all down.
    request("POST", "/reports", {"service": "B", "seq": 2, "status": "recover"})
    _, alerts = request("GET", "/alerts")
    actionable = [f["service"] for f in alerts["actionable_faults"]]
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check("after B recovery: D remains actionable", actionable == ["D"], str(alerts))
    check(
        "A remains suppressed via C only; B removed from suppressed (healthy)",
        "A" in suppressed and "B" not in suppressed
        and suppressed["A"]["paths"] == [["A", "C", "D"]],
        str(alerts),
    )

    # D recovers: C is still faulty with no faulty upstream, so C becomes the
    # root (actionable) and A stays suppressed via the all-down A->C path --
    # reclassification happens immediately, no resend needed.
    request("POST", "/reports", {"service": "D", "seq": 2, "status": "recover"})
    _, alerts = request("GET", "/alerts")
    actionable = sorted(f["service"] for f in alerts["actionable_faults"])
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check(
        "D recovery immediately makes C the new actionable root",
        actionable == ["C"],
        str(alerts),
    )
    check(
        "A reclassified to suppressed-by-C without resend",
        "A" in suppressed and suppressed["A"]["root_causes"] == ["C"]
        and suppressed["A"]["paths"] == [["A", "C"]],
        str(alerts),
    )

    # C recovers too: A is still faulty but no all-down path remains at all,
    # so it must immediately become actionable.
    request("POST", "/reports", {"service": "C", "seq": 2, "status": "recover"})
    _, alerts = request("GET", "/alerts")
    actionable = sorted(f["service"] for f in alerts["actionable_faults"])
    check(
        "once all all-down paths are cut, A becomes actionable without resend",
        actionable == ["A"],
        str(alerts),
    )
    check("no suppressed faults remain", alerts["suppressed_faults"] == [], str(alerts))
    check("healthy list includes recovered B, C and D",
          {"B", "C", "D"}.issubset(set(alerts["healthy_services"])), str(alerts))

    # Never-reported nodes cut paths.  Fresh graph: X down, Y never reported.
    reset()
    request("POST", "/graph", GRAPH_V1)
    fault("X", 1)
    _, alerts = request("GET", "/alerts")
    actionable = [f["service"] for f in alerts["actionable_faults"]]
    check("unreported upstream Y means X stays actionable", actionable == ["X"], str(alerts))
    check("Y listed as unreported", "Y" in alerts["unreported_services"], str(alerts))

    # A single isolated fault with no upstream is always actionable.
    fault("Y", 1)
    _, alerts = request("GET", "/alerts")
    actionable = sorted(f["service"] for f in alerts["actionable_faults"])
    suppressed = {f["service"] for f in alerts["suppressed_faults"]}
    check("Y root actionable", "Y" in actionable, str(alerts))
    check("X now suppressed by Y", "X" in suppressed, str(alerts))


def test_graph_replace_preserves_state() -> None:
    print("[T4] graph replacement preserves same-named states; dropped node state hidden")
    reset()
    request("POST", "/graph", GRAPH_V1)
    request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    request("POST", "/reports", {"service": "B", "seq": 7, "status": "fault"})
    request("POST", "/reports", {"service": "Y", "seq": 1, "status": "fault"})

    status, body = request("POST", "/graph", GRAPH_V2)
    check("v2 accepted", status == 200 and body["revision"] == 2, str(body))
    check(
        "same-named service states in the new graph are preserved",
        set(body["preserved_state"]) == {"B", "D"},
        str(body),
    )

    _, alerts = request("GET", "/alerts")
    check("D still down after swap", "D" in alerts["down_services"], str(alerts))
    check("B still down after swap", "B" in alerts["down_services"], str(alerts))
    check("dropped-node state not exposed", "Y" not in alerts["down_services"], str(alerts))
    check("new node Z reported as unreported", "Z" in alerts["unreported_services"], str(alerts))

    # Sequence numbers preserved as well: seq 7 already used for B.
    status, body = request("POST", "/reports", {"service": "B", "seq": 7, "status": "fault"})
    check("seq history preserved across swap (idempotent)", body["duplicate"] is True, str(body))
    status, _ = request("POST", "/reports", {"service": "B", "seq": 6, "status": "recover"})
    check("stale seq after swap still rejected", status == 409)

    # And if Y comes back in a later version, its old state resurfaces.
    status, _ = request("POST", "/graph", GRAPH_V1)
    _, alerts = request("GET", "/alerts")
    check("re-added Y shows its preserved down state", "Y" in alerts["down_services"], str(alerts))


def test_concurrency() -> None:
    print("[T5] concurrent interleavings")
    reset()
    request("POST", "/graph", GRAPH_V1)

    # 5a: parallel reports with distinct seqs for one service.  Under real
    # at processing time must be rejected (409) and state may never roll back.
    seqs = [2, 5, 3, 8, 1]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [
            pool.submit(request, "POST", "/reports",
                        {"service": "A", "seq": s, "status": "fault" if s % 2 else "recover"})
            for s in seqs
        ]
        results = [f.result() for f in futs]
    ok = [b for st, b in results if st == 200]
    stale_pairs = [(st, b) for st, b in results if st == 409 and "stale" in b["detail"]]
    stale_seqs = [s for s in seqs if any(f"seq {s} <" in b["detail"] for _, b in stale_pairs)]
    applied_seqs = sorted(b["seq"] for b in ok if b["applied"])
    check(
        "applied seqs form a strictly increasing chain",
        all(a < b for a, b in zip(applied_seqs, applied_seqs[1:])),
        str(results),
    )
    check(
        "a report rejected as stale is one that arrived out of order",
        all(s < max(applied_seqs) for s in stale_seqs),
        str(results),
    )
    check("no unexpected error codes", all(st in (200, 409) for st, _ in results), str(results))
    _, alerts = request("GET", "/alerts")
    # Final state matches the content of the highest-seq report (seq 8 -> recover),
    # proving a late low seq never rolled the state backwards.
    check(
        "state never rolled back: A reflects highest seq (seq 8 recover -> up)",
        "A" in alerts["healthy_services"],
        str(alerts),
    )

    # 5b: burst of identical (service, seq, content): one apply, rest idempotent.
    reset()
    request("POST", "/graph", GRAPH_V1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(request, "POST", "/reports",
                            {"service": "A", "seq": 1, "status": "fault"})
                for _ in range(8)]
        burst = [f.result() for f in futs]
    applied = sum(1 for st, b in burst if st == 200 and b["applied"])
    dupes = sum(1 for st, b in burst if st == 200 and b["duplicate"])
    check("identical concurrent retries: exactly 1 apply", applied == 1, str(burst))
    check("identical concurrent retries: 7 idempotent", dupes == 7, str(burst))

    # 5c: same seq, different content in parallel: one wins, others 409, no crash.
    reset()
    request("POST", "/graph", GRAPH_V1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(request, "POST", "/reports",
                        {"service": "A", "seq": 1, "status": "fault"}),
            pool.submit(request, "POST", "/reports",
                        {"service": "A", "seq": 1, "status": "recover"}),
        ]
        race = [f.result() for f in futs]
    ok = [b for st, b in race if st == 200]
    conf = [st for st, _ in race if st == 409]
    check("same-seq conflicting race: one accepted, one 409",
          len(ok) == 1 and len(conf) == 1, str(race))

    # 5d: parallel graph replacements: every observed revision is a fully
    # committed graph; no mixed node/edge sets.
    alt_graphs = [
        {"version": f"g{i}", "services": [
            {"name": f"S{j}", "dependencies": [f"S{j-1}"] if j else []}
            for j in range(i + 1)
        ]}
        for i in range(1, 4)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(request, "POST", "/graph", g) for g in alt_graphs for _ in range(2)]
        replacements = [f.result() for f in futs]
    check("all parallel replacements accepted", all(st == 200 for st, _ in replacements),
          str(replacements))
    status, graph = request("GET", "/graph")
    names = sorted(s["name"] for s in graph["services"])
    expected = sorted(
        s["name"]
        for s in next(g for g in alt_graphs if g["version"] == graph["version"])["services"]
    )
    check("final graph internally consistent", names == expected, str(graph))

    # 5e: replace + query interleaving: every alerts snapshot must be
    # internally consistent with exactly one committed revision.
    with ThreadPoolExecutor(max_workers=6) as pool:
        writer = [pool.submit(request, "POST", "/graph", g) for g in alt_graphs for _ in range(3)]
        reader = [pool.submit(request, "GET", "/alerts") for _ in range(30)]
        for f in writer:
            f.result()
        reads = [f.result() for f in reader]

    consistent = True
    for st, body in reads:
        if st != 200 or not alerts_partition_consistent(body):
            consistent = False
            break
    check(
        "every concurrent read is a consistent single-revision snapshot",
        consistent,
        str(reads[:2]),
    )


def test_ttl_expiry() -> None:
    print("[T6] TTL expiry: status unknown, path cut, automatic reclassification")
    reset()
    _, ttl = request("GET", "/ttl")
    check("reset clears ttl configs", ttl["configs"] == [], str(ttl))
    request("POST", "/graph", GRAPH_V1)

    status, body = request("PUT", "/ttl/NOPE", {"ttl_seconds": 60})
    check("ttl for unknown service -> 404", status == 404, str(body))
    status, _ = request("PUT", "/ttl/D", {"ttl_seconds": 0})
    check("non-positive ttl -> 422", status == 422)
    status, body = request("PUT", "/ttl/D", {"ttl_seconds": 2})
    check(
        "ttl set on D (not expired yet)",
        status == 200 and body["ttl_seconds"] == 2 and body["expired"] is False,
        str(body),
    )
    status, body = request("GET", "/ttl")
    check(
        "ttl config listed",
        status == 200 and any(c["service"] == "D" and c["ttl_seconds"] == 2
                              for c in body["configs"]),
        str(body),
    )

    for svc in ("D", "B", "C", "A"):
        request("POST", "/reports", {"service": svc, "seq": 1, "status": "fault"})
    _, alerts = request("GET", "/alerts")
    check(
        "before expiry: D actionable root",
        [f["service"] for f in alerts["actionable_faults"]] == ["D"],
        str(alerts),
    )
    check("before expiry: no expired services", alerts["expired_services"] == [], str(alerts))

    time.sleep(2.3)
    _, alerts = request("GET", "/alerts")
    expired = {e["service"]: e for e in alerts["expired_services"]}
    check(
        "D expired -> status unknown with last status and seq",
        "D" in expired and expired["D"]["last_status"] == "down"
        and expired["D"]["last_seq"] == 1,
        str(alerts),
    )
    check(
        "expired D neither down nor healthy",
        "D" not in alerts["down_services"] and "D" not in alerts["healthy_services"],
        str(alerts),
    )
    actionable = sorted(f["service"] for f in alerts["actionable_faults"])
    check(
        "expired upstream cuts path: B and C immediately actionable",
        actionable == ["B", "C"],
        str(alerts),
    )
    supp = {f["service"]: f for f in alerts["suppressed_faults"]}
    check(
        "A re-suppressed by the live B/C roots only",
        "A" in supp and supp["A"]["root_causes"] == ["B", "C"]
        and supp["A"]["paths"] == [["A", "B"], ["A", "C"]],
        str(alerts),
    )

    # A new valid report (higher seq) revives D; everything reclassifies.
    status, body = request("POST", "/reports", {"service": "D", "seq": 2, "status": "fault"})
    check("higher-seq report applied", status == 200 and body["applied"] is True, str(body))
    _, alerts = request("GET", "/alerts")
    check(
        "after new report: D actionable root again, expiry cleared",
        [f["service"] for f in alerts["actionable_faults"]] == ["D"]
        and alerts["expired_services"] == [],
        str(alerts),
    )
    check(
        "B/C/A suppressed again without resend",
        {f["service"] for f in alerts["suppressed_faults"]} == {"A", "B", "C"},
        str(alerts),
    )

    # Same-seq retries and rejected reports must not refresh the deadline.
    _, ttl_before = request("GET", "/ttl")
    acc_before = next(c for c in ttl_before["configs"] if c["service"] == "D")["accepted_at"]
    status, body = request("POST", "/reports", {"service": "D", "seq": 2, "status": "fault"})
    check("same-seq retry idempotent", status == 200 and body["duplicate"] is True, str(body))
    status, _ = request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    check("stale seq rejected", status == 409)
    _, ttl_after = request("GET", "/ttl")
    acc_after = next(c for c in ttl_after["configs"] if c["service"] == "D")["accepted_at"]
    check(
        "retry/rejected reports do not refresh acceptance time",
        acc_before == acc_after,
        f"{acc_before} vs {acc_after}",
    )
    time.sleep(2.3)
    _, alerts = request("GET", "/alerts")
    check(
        "D expires again (retry did not extend validity)",
        any(e["service"] == "D" for e in alerts["expired_services"]),
        str(alerts),
    )


def test_ttl_adjustment() -> None:
    print("[T7] TTL set/shorten judged immediately; extend never resurrects")
    reset()
    request("POST", "/graph", GRAPH_V1)

    status, body = request("PUT", "/ttl/D", {"ttl_seconds": 60})
    check("set long ttl", status == 200 and body["expired"] is False, str(body))
    request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    time.sleep(1.3)

    # Shortening below the report's age judges expiry immediately, against
    # the acceptance time of the last valid report.
    status, body = request("PUT", "/ttl/D", {"ttl_seconds": 1})
    check(
        "shorten below report age -> expired immediately",
        status == 200 and body["expired"] is True and body["previous_ttl_seconds"] == 60,
        str(body),
    )
    _, alerts = request("GET", "/alerts")
    check(
        "alerts reflect the shortening at once",
        any(e["service"] == "D" for e in alerts["expired_services"]),
        str(alerts),
    )

    # Extending the window must not resurrect the expired report.
    status, body = request("PUT", "/ttl/D", {"ttl_seconds": 3600})
    check(
        "extend does not resurrect expired report",
        status == 200 and body["expired"] is True,
        str(body),
    )
    _, alerts = request("GET", "/alerts")
    check(
        "still expired after extension",
        any(e["service"] == "D" for e in alerts["expired_services"]),
        str(alerts),
    )

    # A same-seq retry does not clear expiry either.
    request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    _, alerts = request("GET", "/alerts")
    check(
        "same-seq retry does not clear expiry",
        any(e["service"] == "D" for e in alerts["expired_services"]),
        str(alerts),
    )

    # Only a higher-seq valid report clears it.
    status, body = request("POST", "/reports", {"service": "D", "seq": 2, "status": "fault"})
    check("higher-seq report clears expiry", status == 200 and body["applied"] is True, str(body))
    _, alerts = request("GET", "/alerts")
    check(
        "D down again after higher-seq report",
        "D" in alerts["down_services"] and alerts["expired_services"] == [],
        str(alerts),
    )

    # Silence after a recovery also expires: last status up becomes unknown.
    request("POST", "/reports", {"service": "D", "seq": 3, "status": "recover"})
    request("PUT", "/ttl/D", {"ttl_seconds": 1})
    _, alerts = request("GET", "/alerts")
    check("recovered D healthy while window valid", "D" in alerts["healthy_services"], str(alerts))
    time.sleep(1.3)
    _, alerts = request("GET", "/alerts")
    expired = {e["service"]: e for e in alerts["expired_services"]}
    check(
        "silent after recover -> unknown with last_status up",
        "D" in expired and expired["D"]["last_status"] == "up"
        and "D" not in alerts["healthy_services"],
        str(alerts),
    )


def test_ttl_graph_swap() -> None:
    print("[T8] graph replacement keeps TTL configs; off-graph services not judged")
    reset()
    request("POST", "/graph", GRAPH_V1)
    request("PUT", "/ttl/Y", {"ttl_seconds": 1})
    request("PUT", "/ttl/D", {"ttl_seconds": 60})
    request("POST", "/reports", {"service": "Y", "seq": 1, "status": "fault"})
    _, alerts = request("GET", "/alerts")
    check("Y down before swap", "Y" in alerts["down_services"], str(alerts))

    # Swap to v2 (Y removed): Y's config is retained but Y is not judged.
    status, _ = request("POST", "/graph", GRAPH_V2)
    check("v2 accepted", status == 200)
    _, alerts = request("GET", "/alerts")
    check(
        "off-graph Y not judged (neither down nor expired)",
        "Y" not in alerts["down_services"]
        and all(e["service"] != "Y" for e in alerts["expired_services"]),
        str(alerts),
    )
    _, ttl = request("GET", "/ttl")
    yconf = next((c for c in ttl["configs"] if c["service"] == "Y"), None)
    check(
        "config retained for off-graph service",
        yconf is not None and yconf["in_graph"] is False and yconf["expired"] is None,
        str(ttl),
    )
    dconf = next((c for c in ttl["configs"] if c["service"] == "D"), None)
    check(
        "same-named config kept across swap",
        dconf is not None and dconf["ttl_seconds"] == 60 and dconf["in_graph"] is True,
        str(ttl),
    )

    # Swap back: config + state resurface; the old report is past its window.
    time.sleep(1.3)
    request("POST", "/graph", GRAPH_V1)
    _, alerts = request("GET", "/alerts")
    expired = {e["service"]: e for e in alerts["expired_services"]}
    check(
        "back in graph: Y judged expired from persisted acceptance time",
        "Y" in expired and expired["Y"]["last_seq"] == 1,
        str(alerts),
    )


def test_ttl_concurrency() -> None:
    print("[T9] concurrent ttl config + reports + swaps + queries stay consistent")
    reset()
    request("POST", "/graph", GRAPH_V1)
    for svc in ("A", "B", "C", "D"):
        request("POST", "/reports", {"service": svc, "seq": 1, "status": "fault"})

    def hammer_ttl():
        return [
            request("PUT", f"/ttl/{svc}", {"ttl_seconds": ttl})
            for svc in "ABCD" for ttl in (1, 2, 3600)
        ]

    def hammer_reports(services: str):
        return [
            request("POST", "/reports", {"service": svc, "seq": i, "status": "fault"})
            for svc in services for i in range(2, 14)
        ]

    def hammer_swaps():
        return [request("POST", "/graph", g) for g in (GRAPH_V2, GRAPH_V1) * 2]

    def hammer_reads():
        return [request("GET", "/alerts") for _ in range(20)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(hammer_ttl) for _ in range(2)]
        futs += [pool.submit(hammer_reports, "AB"), pool.submit(hammer_reports, "CD")]
        futs += [pool.submit(hammer_swaps)]
        futs += [pool.submit(hammer_reads) for _ in range(3)]
        results = [f.result() for f in futs]

    ttl_results = [r for batch in results[0:2] for r in batch]
    report_results = [r for batch in results[2:4] for r in batch]
    swap_results = results[4]
    read_results = [r for batch in results[5:] for r in batch]

    check("all ttl writes accepted", all(st == 200 for st, _ in ttl_results),
          str(ttl_results[:3]))
    check("all reports accepted", all(st == 200 for st, _ in report_results),
          str(report_results[:3]))
    check("all swaps accepted", all(st == 200 for st, _ in swap_results), str(swap_results))
    consistent = all(
        st == 200 and alerts_partition_consistent(body) for st, body in read_results
    )
    check("every interleaved read is a consistent snapshot", consistent,
          str(read_results[:2]))
    st, body = request("GET", "/alerts")
    check("final state consistent", st == 200 and alerts_partition_consistent(body), str(body))


def main() -> int:
    print(f"Self-test against {BASE_URL}\n")
    try:
        urllib.request.urlopen(BASE_URL + "/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"server not reachable: {exc}")
        return 2

    test_graph_validation()
    test_report_semantics()
    test_suppression()
    test_graph_replace_preserves_state()
    test_concurrency()
    test_ttl_expiry()
    test_ttl_adjustment()
    test_ttl_graph_swap()
    test_ttl_concurrency()

    print(f"\n{'-' * 60}\nPASS={PASS} FAIL={FAIL}")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
