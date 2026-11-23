import json
import sqlite3
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from retest.analysis.review import ReviewRequest, ScanReviewRequest, review
from retest.evidence.encoding import decode_evidence
from retest.evidence.issue import issue
from retest.evidence.registry import EvidenceRegistry
from retest.reports.ledger import IssuedReport, ReportLedger
from retest.reports.retrieve import get
from retest.revisions.model import RevisionDraft, Transition, TrialSource
from retest.revisions.registry import register
from retest.revisions.storage import RevisionStore
from retest.wiring.binding import bind
from retest.wiring.ports import PortRef
from retest.wiring.storage import ManifestStore


EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding='utf-8'))


def _install(path: Path, data: dict, *, version: int = 1) -> str:
    draft = RevisionDraft(
        data['station'], data['revision_id'], TrialSource(**data['source']),
        data['entries'], data['exits'],
        {state: [Transition(**item) for item in row] for state, row in data['transitions'].items()},
        data.get('predecessor'),
    )
    with closing(sqlite3.connect(path)) as db:
        revisions, registry = RevisionStore(db), EvidenceRegistry(db)
        revisions.initialize()
        registry.initialize()
        register(revisions, draft)
        return issue(revisions, registry, draft.station, draft.revision_id, version=version)


def _sign(center: Path, settings: dict, bodies: tuple[str, str]) -> tuple[IssuedReport, ...]:
    identities = {}
    for body in bodies:
        evidence = decode_evidence(body)
        identities[evidence.origin.station] = evidence.piece_id

    def port(endpoint: list[str]) -> PortRef:
        station, name = endpoint
        return PortRef(identities[station], name)

    routes = [(port(outlet), port(target) if target is not None else None) for outlet, target in settings['routes']]
    with closing(sqlite3.connect(center)) as db:
        store, ledger = ManifestStore(db), ReportLedger(db)
        store.initialize()
        ledger.initialize()
        manifest = bind(store, settings['manifest_id'], settings['wiring_source'], bodies, routes)
        start = port(settings['start'])
        return tuple(review(store, ledger, ReviewRequest(manifest.manifest_id, start, tick))
                     for tick in settings['deadlines'])


def _result(report: IssuedReport) -> dict:
    snapshot = report.snapshot
    return {'id': report.report_id, 'deadline': snapshot.deadline, 'on_time': str(snapshot.on_time),
            'never_complete': str(snapshot.never_complete), 'manifest': snapshot.manifest.manifest_id,
            'revisions': [item.revision_id for item in snapshot.origins], 'receipt': report.receipt.status}


def _scan_draft(model: dict, name: str) -> RevisionDraft:
    rows = {
        state: [Transition(target, probability, ticks, is_exit, scan)
                for target, probability, ticks, is_exit, scan in edges]
        for state, edges in model['rows'].items()
    }
    return RevisionDraft(model['station'], name, TrialSource('insulation-42', 'night', name),
                         model['entries'], model['exits'], rows)


def _install_scan(path: Path, model: dict, name: str) -> str:
    with closing(sqlite3.connect(path)) as db:
        revisions, registry = RevisionStore(db), EvidenceRegistry(db)
        revisions.initialize()
        registry.initialize()
        register(revisions, _scan_draft(model, name))
        return issue(revisions, registry, model['station'], name, version=2)


def _scan_result(report: IssuedReport) -> dict:
    snapshot = report.snapshot
    return {
        'id': report.report_id, 'manifest': snapshot.manifest.manifest_id,
        'observed_through': snapshot.observed_through, 'deadline': snapshot.deadline,
        'scans': [list(item) for item in snapshot.scans], 'support': str(snapshot.support),
        'on_time': None if snapshot.on_time is None else str(snapshot.on_time),
        'never_complete': None if snapshot.never_complete is None else str(snapshot.never_complete),
        'revisions': [item.revision_id for item in snapshot.origins],
        'piece_ids': list(snapshot.manifest.parents), 'receipt': report.receipt.status,
    }


def _run_scan_demo(root: Path) -> dict:
    data = _load('scan.json')
    a_path, b_path, center = root / 'scan-a.sqlite', root / 'scan-b.sqlite', root / 'scan-center.sqlite'
    chosen = next(case for case in data['cases'] if case['id'] == 'fast-order')
    zero_case = next(case for case in data['cases'] if case['id'] == 'no-support')
    a_piece = _install_scan(a_path, data['models'][chosen['a']], chosen['a'])
    b_piece = _install_scan(b_path, data['models']['confirmation'], 'confirmation')
    # The stations go offline before the center binds or signs anything.
    a_path.unlink()
    b_path.unlink()
    pair = (a_piece, b_piece)
    identities = {decode_evidence(body).origin.station: decode_evidence(body).piece_id for body in pair}

    def routes(case):
        return [(PortRef(identities[src[0]], src[1]),
                 None if dst is None else PortRef(identities[dst[0]], dst[1]))
                for src, dst in data['wiring'][case['wiring']]]

    with closing(sqlite3.connect(center)) as db:
        manifests, ledger = ManifestStore(db), ReportLedger(db)
        manifests.initialize()
        ledger.initialize()
        bind(manifests, chosen['id'], 'night routing card', pair, routes(chosen))
        bind(manifests, zero_case['id'], 'night routing card', pair, routes(zero_case))
        request = ScanReviewRequest(
            chosen['id'], PortRef(identities['A'], 'in'), chosen['c'],
            tuple((tick, name) for tick, name in chosen['scans']), chosen['d'])
        signed = review(manifests, ledger, request)
        zero_request = ScanReviewRequest(
            zero_case['id'], PortRef(identities['A'], 'in'), zero_case['c'],
            tuple((tick, name) for tick, name in zero_case['scans']), zero_case['d'])
        zero_signed = review(manifests, ledger, zero_request)
        signed_id, zero_id = signed.report_id, zero_signed.report_id
    # Reopen the center with the station libraries still absent.
    with closing(sqlite3.connect(center)) as db:
        ledger = ReportLedger(db)
        ledger.initialize()
        retrieved = get(ledger, signed_id)
        zero_retrieved = get(ledger, zero_id)
    if retrieved != signed or zero_retrieved != zero_signed:
        raise RuntimeError('offline scan report retrieval does not match the signed report')
    return {'signed': _scan_result(signed), 'zero_support': _scan_result(zero_signed),
            'retrieved_by_id': signed_id}


def main() -> None:
    day = _load('day.json')
    correction = _load('correction.json')
    with TemporaryDirectory(prefix='retest-demo-') as work:
        root = Path(work)
        a_path, b_path, center = root / 'a.sqlite', root / 'b.sqlite', root / 'center.sqlite'
        a = _install(a_path, day['stations'][0])
        b = _install(b_path, day['stations'][1])
        b_path.unlink()
        initial = _sign(center, day, (a, b))
        corrected_a = _install(a_path, correction['station'])
        a_path.unlink()
        updated = _sign(center, {**day, **correction['wiring']}, (corrected_a, b))
        with closing(sqlite3.connect(center)) as db:
            ledger = ReportLedger(db)
            ledger.initialize()
            retrieved = tuple(get(ledger, previous.report_id) for previous in initial)
        if ([item.snapshot.on_time for item in initial] != [Fraction(1, 2), Fraction(3, 4)]
                or [item.snapshot.on_time for item in updated] != [0, Fraction(3, 4)]
                or any(item.snapshot.never_complete for item in (*initial, *updated))
                or retrieved != initial):
            raise RuntimeError('demo calculation or report retrieval does not match the sample')
        scan_demo = _run_scan_demo(root)
        print(json.dumps({'initial': [_result(report) for report in initial],
                          'corrected': [_result(report) for report in updated],
                          'retrieved': [_result(report) for report in retrieved],
                          'scan_window': scan_demo}, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
