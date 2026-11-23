import json
import sqlite3
from contextlib import closing
from fractions import Fraction
from pathlib import Path

from retest.analysis.review import ReviewRequest, ScanReviewRequest, review
from retest.evidence.encoding import decode_evidence, encode_scan_evidence
from retest.evidence.issue import issue
from retest.evidence.registry import EvidenceRegistry
from retest.reports.ledger import ReportLedger
from retest.reports.retrieve import get
from retest.revisions.model import RevisionDraft, Transition, TrialSource
from retest.revisions.registry import register
from retest.revisions.storage import RevisionStore
from retest.wiring.binding import bind
from retest.wiring.ports import PortRef
from retest.wiring.storage import ManifestStore


FIXTURE = Path(__file__).resolve().parent / "examples" / "scan.json"


def load_cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _draft(name, model):
    rows = {
        state: [Transition(target, probability, ticks, is_exit, scan)
                for target, probability, ticks, is_exit, scan in edges]
        for state, edges in model["rows"].items()
    }
    station = model["station"]
    return station, RevisionDraft(station, name, TrialSource("insulation-42", "night", name),
                                  model["entries"], model["exits"], rows)


def issue_projection(name, model, version):
    station, draft = _draft(name, model)
    with closing(sqlite3.connect(":memory:")) as db:
        revisions, issued = RevisionStore(db), EvidenceRegistry(db)
        revisions.initialize()
        issued.initialize()
        register(revisions, draft)
        return issue(revisions, issued, station, name, version=version)


def issue_v1_projection(name, model):
    return issue_projection(name, model, 1)


def _routes(data, case, identities):
    return [(PortRef(identities[src[0]], src[1]),
             None if dst is None else PortRef(identities[dst[0]], dst[1]))
            for src, dst in data["wiring"][case["wiring"]]]


def verify_v1(data):
    bodies = {name: issue_v1_projection(name, model) for name, model in data["models"].items()}
    fast = decode_evidence(bodies["fast"])
    slow = decode_evidence(bodies["slow"])
    if fast.entries != slow.entries:
        raise AssertionError("the two revisions must have identical unconditional entrance evidence")
    results = {}
    with closing(sqlite3.connect(":memory:")) as center:
        manifests, ledger = ManifestStore(center), ReportLedger(center)
        manifests.initialize()
        ledger.initialize()
        for case in data["cases"]:
            case_id = case["id"]
            pair = bodies[case["a"]], bodies["confirmation"]
            parents = {piece.origin.station: piece.piece_id for piece in map(decode_evidence, pair)}
            routes = _routes(data, case, parents)
            bind(manifests, case_id, "night routing card", pair, routes)
            request = ReviewRequest(case_id, PortRef(parents["A"], "in"), case["d"])
            report = review(manifests, ledger, request)
            actual = report.snapshot.on_time, report.snapshot.never_complete
            if actual != tuple(map(Fraction, case["v1"])) or get(ledger, report.report_id) != report:
                raise AssertionError(f"{case_id}: V1 outcome or stored report differs")
            results[case_id] = list(map(str, actual))
    return results


def verify_v2(data):
    bodies = {name: issue_projection(name, model, 2) for name, model in data["models"].items()}
    for body in bodies.values():
        evidence = decode_evidence(body)
        if body != encode_scan_evidence(evidence):
            raise AssertionError("V2 body is not canonical or fails offline verification")
    results = {}
    with closing(sqlite3.connect(":memory:")) as center:
        manifests, ledger = ManifestStore(center), ReportLedger(center)
        manifests.initialize()
        ledger.initialize()
        for case in data["cases"]:
            case_id = case["id"]
            pair = bodies[case["a"]], bodies["confirmation"]
            identities = {piece.origin.station: piece.piece_id for piece in map(decode_evidence, pair)}
            routes = _routes(data, case, identities)
            bind(manifests, case_id, "night routing card", pair, routes)
            request = ScanReviewRequest(
                case_id, PortRef(identities["A"], "in"), case["c"],
                tuple((tick, name) for tick, name in case["scans"]), case["d"])
            report = review(manifests, ledger, request)
            snapshot = report.snapshot
            actual = [str(snapshot.support),
                      None if snapshot.on_time is None else str(snapshot.on_time),
                      None if snapshot.never_complete is None else str(snapshot.never_complete)]
            if actual != case["v2"]:
                raise AssertionError(f"{case_id}: V2 conditional result differs")
            if get(ledger, report.report_id) != report:
                raise AssertionError(f"{case_id}: stored V2 report or receipt differs")
            results[case_id] = actual
    return results


def verify(data):
    return {"v1": verify_v1(data), "v2": verify_v2(data)}


if __name__ == "__main__":
    print(json.dumps(verify(load_cases()), sort_keys=True))
