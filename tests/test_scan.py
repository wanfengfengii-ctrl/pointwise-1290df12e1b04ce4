import json
import sqlite3
from contextlib import closing
from fractions import Fraction as F

from retest.analysis.automaton import build_joint_automaton
from retest.analysis.review import ReviewRequest, ScanReviewRequest, review
from retest.analysis.scans import evaluate_scan_window
from retest.evidence.encoding import (StationEvidence, StationScanEvidence, decode_evidence,
                                      encode_scan_evidence)
from retest.evidence.issue import issue
from retest.evidence.registry import EvidenceRegistry
from retest.reports.ledger import ReportLedger
from retest.reports.retrieve import get
from retest.revisions.model import Transition
from retest.revisions.storage import RevisionStore
from retest.wiring.binding import bind
from retest.wiring.ports import PortRef
from retest.wiring.storage import ManifestStore
from tests.scan_support import (FIXTURE, bind_case, build_revision, expected, issue_all,
                                piece_ports, routes_for, scan_request)
from tests.support import Workbench, draft


class ScanEvidenceTests(Workbench):
    def test_v2_envelope_and_private_names_kept_at_station(self):
        bodies = issue_all(2)
        body = bodies['fast']
        evidence = decode_evidence(body)
        self.assertIsInstance(evidence, StationScanEvidence)
        self.assertEqual(evidence.origin.revision_id, 'fast')
        payload = json.loads(body)
        self.assertEqual((payload['version'], payload['kind']), (2, 'station-evidence'))
        self.assertEqual(payload['id'], evidence.piece_id)
        # Private state names must never leave the station.
        self.assertNotIn('bench', body)
        self.assertNotIn('private', body)
        self.assertEqual(len(evidence.nodes), len(FIXTURE['models']['fast']['rows']))
        self.assertEqual(dict(evidence.entries), {'in': evidence.nodes[0]})

    def test_v2_decode_roundtrip_and_offline_identity(self):
        for body in issue_all(2).values():
            evidence = decode_evidence(body)
            canonical = encode_scan_evidence(evidence)
            self.assertEqual(canonical, body)
            self.assertEqual(evidence.piece_id, decode_evidence(canonical).piece_id)
            # Every internal node target resolves to a known node.
            node_set = set(evidence.nodes)
            for edge in evidence.edges:
                self.assertIn(edge.source, node_set)
                if not edge.is_exit:
                    self.assertIn(edge.target, node_set)

    def test_v1_and_v2_have_distinct_pieces_but_same_origin(self):
        v1 = issue_all(1)
        v2 = issue_all(2)
        for name in v1:
            first, second = decode_evidence(v1[name]), decode_evidence(v2[name])
            self.assertIsInstance(first, StationEvidence)
            self.assertIsInstance(second, StationScanEvidence)
            self.assertNotEqual(first.piece_id, second.piece_id)
            self.assertEqual(first.origin, second.origin)

    def test_scan_mark_roundtrip_through_revision_store(self):
        marked = draft('A', edges=(Transition('out', 1, 1, True, scan='R'),))
        body = self.issued(marked)
        with self.source('A') as db:
            revision = RevisionStore(db).read('A', 'r1')
        self.assertEqual(revision.transitions[0][1][0].scan, 'R')
        v2 = self.issued_v2(marked)
        evidence = decode_evidence(v2)
        self.assertEqual(evidence.edges[0].scan, 'R')

    def issued_v2(self, revision):
        # The revision was already registered by the workbench; only re-issue.
        with self.source(revision.station) as db:
            revisions, registry = RevisionStore(db), EvidenceRegistry(db)
            revisions.initialize()
            registry.initialize()
            return issue(revisions, registry, revision.station, revision.revision_id, version=2)

    def test_invalid_version_rejected(self):
        revision = draft('A')
        with self.source('A') as db:
            revisions, registry = RevisionStore(db), EvidenceRegistry(db)
            revisions.initialize()
            registry.initialize()
            from retest.revisions.registry import register
            register(revisions, revision)
            for bad in (0, 3, True, '2', 1.0):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    issue(revisions, registry, 'A', 'r1', version=bad)

    def test_tampered_v2_body_rejected(self):
        body = issue_all(2)['fast']
        tampered = json.loads(body)
        tampered['body']['nodes'].append('forged-node')
        with self.assertRaises(ValueError):
            decode_evidence(json.dumps(tampered))
        with self.assertRaises(ValueError):
            decode_evidence(body.replace('"version":2', '"version":3'))

    def test_old_revision_without_scan_marks_reads_as_none(self):
        legacy = draft('A')  # no scan argument
        stored = self.issued(legacy)
        with self.source('A') as db:
            revision = RevisionStore(db).read('A', 'r1')
        self.assertIsNone(revision.transitions[0][1][0].scan)
        self.assertIsInstance(decode_evidence(stored), StationEvidence)

    def test_explicit_reselect_and_reopen_keep_scan_marks(self):
        from retest.revisions.history import select_revision
        marked = draft('A', 'r2', predecessor='r1',
                       edges=(Transition('out', '1/2', 0, True, scan='R'),
                              Transition('out', '1/2', 2, True)))
        self.issued(draft('A'))
        with self.source('A') as db:
            revisions, registry = RevisionStore(db), EvidenceRegistry(db)
            revisions.initialize()
            registry.initialize()
            from retest.revisions.registry import register
            register(revisions, marked)
            body = issue(revisions, registry, 'A', 'r2', version=2)
            piece_id = decode_evidence(body).piece_id
        # Reopen the station library and confirm the mark survives the round trip.
        with self.source('A') as db:
            revisions, registry = RevisionStore(db), EvidenceRegistry(db)
            revisions.initialize()
            registry.initialize()
            reread = select_revision(revisions, 'A', 'r2')
            self.assertEqual(reread.transitions[0][1][0].scan, 'R')
            self.assertIsNone(reread.transitions[0][1][1].scan)
            self.assertEqual(registry.read(piece_id), body)


class ScanBindingTests(Workbench):
    def test_mixed_versions_rejected(self):
        v1 = issue_all(1)
        v2 = issue_all(2)
        pair = v2['fast'], v1['confirmation']
        identities = piece_ports(pair)
        case = expected('fast-order')
        routes = routes_for(case, identities)
        with self.center() as db:
            with self.assertRaisesRegex(ValueError, 'same evidence version'):
                bind(ManifestStore(db), 'mixed', 'night', pair, routes)
            self.assertEqual(self.center_counts(), (0, 0, 0))

    def test_v2_pair_binds_and_reloads_offline(self):
        bodies = issue_all(2)
        case = expected('fast-order')
        pair = bodies[case['a']], bodies['confirmation']
        identities = piece_ports(pair)
        routes = routes_for(case, identities)
        with self.center() as db:
            manifest = bind(ManifestStore(db), case['id'], 'night', pair, routes)
            loaded, received = ManifestStore(db).load(case['id'])
        self.assertEqual(loaded, manifest)
        self.assertTrue(all(isinstance(item.evidence, StationScanEvidence) for item in received))


class ScanReviewTests(Workbench):
    def _center(self):
        db = sqlite3.connect(self.center_path)
        return ManifestStore(db), ReportLedger(db), db

    def test_all_fixture_cases_match_expected_values(self):
        bodies = issue_all(2)
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            for case in FIXTURE['cases']:
                identities = bind_case(manifests, case, bodies)
                report = review(manifests, ledger, scan_request(case, identities))
                snapshot = report.snapshot
                got = [str(snapshot.support),
                       None if snapshot.on_time is None else str(snapshot.on_time),
                       None if snapshot.never_complete is None else str(snapshot.never_complete)]
                self.assertEqual(got, case['v2'], case['id'])
                self.assertEqual(snapshot.observed_through, case['c'])
                self.assertEqual(snapshot.deadline, case['d'])
                self.assertEqual(snapshot.scans, tuple(map(tuple, case['scans'])))

    def test_zero_support_still_issues_report_and_receipt_with_none(self):
        bodies = issue_all(2)
        case = expected('no-support')
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            identities = bind_case(manifests, case, bodies)
            report = review(manifests, ledger, scan_request(case, identities))
            self.assertEqual((report.snapshot.support, report.snapshot.on_time,
                              report.snapshot.never_complete), (F(0), None, None))
            self.assertEqual(report.receipt.status, 'issued')
            self.assertEqual(get(ledger, report.report_id), report)

    def test_unknown_manifest_is_key_error(self):
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            request = ScanReviewRequest('missing', PortRef('piece', 'in'), 0, (), 1)
            with self.assertRaises(KeyError):
                review(manifests, ledger, request)

    def test_v2_rejects_classic_request_and_vice_versa(self):
        bodies = issue_all(2)
        case = expected('fast-order')
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            identities = bind_case(manifests, case, bodies)
            with self.assertRaisesRegex(ValueError, 'version 1'):
                review(manifests, ledger,
                       ReviewRequest(case['id'], PortRef(identities['A'], 'in'), case['d']))
        v1 = issue_all(1)
        with closing(sqlite3.connect(':memory:')) as db:
            manifests = ManifestStore(db)
            manifests.initialize()
            pair = v1[case['a']], v1['confirmation']
            identities = piece_ports(pair)
            bind(manifests, case['id'], 'night', pair, routes_for(case, identities))
            with self.assertRaisesRegex(ValueError, 'version 2'):
                build_joint_automaton(manifests, case['id'])

    def test_bad_start_port_rejected(self):
        bodies = issue_all(2)
        case = expected('fast-order')
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            bind_case(manifests, case, bodies)
            request = ScanReviewRequest(case['id'], PortRef('unknown-piece', 'in'),
                                        case['c'], tuple(map(tuple, case['scans'])), case['d'])
            with self.assertRaises(ValueError):
                review(manifests, ledger, request)

    def test_invalid_scan_requests_rejected(self):
        bodies = issue_all(2)
        case = expected('fast-order')
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            identities = bind_case(manifests, case, bodies)
            start = PortRef(identities['A'], 'in')
            bad_requests = [
                ScanReviewRequest(case['id'], start, -1, (), 1),
                ScanReviewRequest(case['id'], start, 2, (), 1),
                ScanReviewRequest(case['id'], start, True, (), 1),
                ScanReviewRequest(case['id'], start, 0, (), True),
                ScanReviewRequest(case['id'], start, 1, ((1.0, 'R'),), 2),
                ScanReviewRequest(case['id'], start, 1, ((True, 'R'),), 2),
                ScanReviewRequest(case['id'], start, 1, ((2, 'R'),), 2),
                ScanReviewRequest(case['id'], start, 1, ((1, '  '),), 2),
                ScanReviewRequest(case['id'], start, 1, ((1, 'R'), (0, 'X')), 2),
            ]
            for request in bad_requests:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    review(manifests, ledger, request)
            self.assertEqual(self.center_counts(), (1, 0, 0))

    def test_unseen_scan_name_is_still_a_valid_observation(self):
        bodies = issue_all(2)
        case = expected('no-support')
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            identities = bind_case(manifests, case, bodies)
            request = ScanReviewRequest(case['id'], PortRef(identities['A'], 'in'),
                                        case['c'], ((1, 'brand-new-name'),), case['d'])
            report = review(manifests, ledger, request)
            self.assertEqual(report.snapshot.support, F(0))

    def test_receipt_write_failure_leaves_no_half_piece(self):
        bodies = issue_all(2)
        case = expected('fast-order')
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            identities = bind_case(manifests, case, bodies)
            first = review(manifests, ledger, scan_request(case, identities))
            db.execute('''CREATE TRIGGER refuse_scan_receipt BEFORE INSERT ON receipts
                WHEN EXISTS (SELECT 1 FROM reports WHERE report_id = NEW.report_id)
                BEGIN SELECT RAISE(ABORT, 'scan receipt denied'); END''')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'scan receipt denied'):
                review(manifests, ledger, scan_request(case, identities))
            self.assertEqual(self.center_counts(), (1, 1, 1))
            self.assertEqual(get(ledger, first.report_id), first)

    def test_reopen_center_and_retrieve_original_observation(self):
        bodies = issue_all(2)
        case = expected('window-edge')
        report_id = None
        with closing(sqlite3.connect(self.center_path)) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            identities = bind_case(manifests, case, bodies)
            report = review(manifests, ledger, scan_request(case, identities))
            report_id = report.report_id
        self.stop_sources()
        with closing(sqlite3.connect(self.center_path)) as db:
            ledger = ReportLedger(db)
            ledger.initialize()
            retrieved = get(ledger, report_id)
        self.assertEqual(retrieved.snapshot.scans, tuple(map(tuple, case['scans'])))
        self.assertEqual((str(retrieved.snapshot.support), str(retrieved.snapshot.on_time),
                          str(retrieved.snapshot.never_complete)), tuple(case['v2']))
        self.assertEqual([origin.revision_id for origin in retrieved.snapshot.origins],
                         [case['a'], 'confirmation'])
        self.assertEqual(retrieved.receipt.status, 'issued')


class ScanEngineTests(Workbench):
    def test_support_includes_finished_inflight_and_silent_trap_mass(self):
        bodies = issue_all(2)
        case = expected('window-edge')
        with closing(sqlite3.connect(':memory:')) as db:
            manifests = ManifestStore(db)
            manifests.initialize()
            identities = bind_case(manifests, case, bodies)
            automaton = build_joint_automaton(manifests, case['id'])
            start = automaton.entrances[PortRef(identities['A'], 'in')]
            judgment = evaluate_scan_window(automaton, start, 1, 1, ((1, 'R'),))
        self.assertEqual(judgment.support, F(3, 4))

    def test_infinite_in_window_scans_are_incompatible(self):
        # A zero-tick labeled self-loop can never match a finite observation word.
        loop_body = self._loop_evidence()
        with closing(sqlite3.connect(':memory:')) as db:
            manifests = ManifestStore(db)
            manifests.initialize()
            pair = (loop_body, issue_all(2)['confirmation'])
            identities = piece_ports(pair)
            case = expected('window-edge')
            bind(manifests, 'looping', 'night', pair, routes_for(case, identities))
            automaton = build_joint_automaton(manifests, 'looping')
            start = automaton.entrances[PortRef(identities['A'], 'in')]
            judgment = evaluate_scan_window(automaton, start, 0, 0, ((0, 'R'),))
        self.assertEqual(judgment.support, F(0))
        self.assertIsNone(judgment.on_time)

    def _loop_evidence(self):
        from retest.revisions.model import RevisionDraft, TrialSource
        model = FIXTURE['models']['window']
        looping = {
            'station': 'A', 'entries': {'in': 'bench'}, 'exits': ['out'],
            'rows': {'bench': [['bench', '1', 0, False, 'R']]},
        }
        with closing(sqlite3.connect(':memory:')) as db:
            revisions, registry = RevisionStore(db), EvidenceRegistry(db)
            revisions.initialize()
            registry.initialize()
            build_revision(revisions, looping, 'looping')
            return issue(revisions, registry, 'A', 'looping', version=2)

    def test_repeated_and_empty_scan_records_are_accepted(self):
        bodies = issue_all(2)
        case = expected('silent-return')
        with closing(sqlite3.connect(':memory:')) as db:
            manifests, ledger = ManifestStore(db), ReportLedger(db)
            manifests.initialize()
            ledger.initialize()
            identities = bind_case(manifests, case, bodies)
            request = ScanReviewRequest(case['id'], PortRef(identities['A'], 'in'),
                                        0, (), 0)
            report = review(manifests, ledger, request)
            self.assertEqual(report.snapshot.support, F(1))
