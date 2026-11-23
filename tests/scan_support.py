import json
import sqlite3
from contextlib import closing
from pathlib import Path

from retest.analysis.review import ScanReviewRequest
from retest.evidence.encoding import decode_evidence
from retest.evidence.issue import issue
from retest.evidence.registry import EvidenceRegistry
from retest.revisions.model import RevisionDraft, Transition, TrialSource
from retest.revisions.registry import register
from retest.revisions.storage import RevisionStore
from retest.wiring.binding import bind
from retest.wiring.ports import PortRef
from retest.wiring.storage import ManifestStore


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / 'examples' / 'scan.json')
                     .read_text(encoding='utf-8'))


def build_revision(store, model, name):
    rows = {
        state: [Transition(target, probability, ticks, is_exit, scan)
                for target, probability, ticks, is_exit, scan in edges]
        for state, edges in model['rows'].items()
    }
    draft = RevisionDraft(model['station'], name, TrialSource('insulation-42', 'night', name),
                          model['entries'], model['exits'], rows)
    return register(store, draft)


def issue_piece(version, model, name):
    with closing(sqlite3.connect(':memory:')) as db:
        revisions, registry = RevisionStore(db), EvidenceRegistry(db)
        revisions.initialize()
        registry.initialize()
        build_revision(revisions, model, name)
        return issue(revisions, registry, model['station'], name, version=version)


def issue_all(version):
    return {name: issue_piece(version, model, name) for name, model in FIXTURE['models'].items()}


def piece_ports(bodies):
    identities = {}
    for body in bodies:
        evidence = decode_evidence(body)
        identities[evidence.origin.station] = evidence.piece_id
    return identities


def routes_for(case, identities):
    return [
        (PortRef(identities[src[0]], src[1]),
         None if dst is None else PortRef(identities[dst[0]], dst[1]))
        for src, dst in FIXTURE['wiring'][case['wiring']]
    ]


def bind_case(manifests, case, bodies):
    pair = bodies[case['a']], bodies['confirmation']
    identities = piece_ports(pair)
    bind(manifests, case['id'], 'night routing card', pair, routes_for(case, identities))
    return identities


def expected(case_id):
    return next(case for case in FIXTURE['cases'] if case['id'] == case_id)


def scan_request(case, identities):
    return ScanReviewRequest(case['id'], PortRef(identities['A'], 'in'),
                             case['c'], tuple((tick, name) for tick, name in case['scans']),
                             case['d'])
