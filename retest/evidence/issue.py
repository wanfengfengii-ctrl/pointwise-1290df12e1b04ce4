from retest.evidence.encoding import (EvidenceOrigin, StationEvidence, StationScanEvidence,
                                       encode_evidence, encode_scan_evidence)
from retest.evidence.first_exit import compile_first_exits
from retest.evidence.registry import EvidenceRegistry
from retest.evidence.scan_graph import compile_scan_graph
from retest.revisions.history import select_revision
from retest.revisions.storage import RevisionStore


def issue(revisions: RevisionStore, registry: EvidenceRegistry, station: str, revision_id: str,
          *, version: int = 1) -> str:
    if type(version) is not int or isinstance(version, bool) or version not in (1, 2):
        raise ValueError('evidence version must be integer 1 or 2')
    revision = select_revision(revisions, station, revision_id)
    source = revision.source
    origin = EvidenceOrigin(revision.station, revision.revision_id, source.work_order,
                            source.round_id, source.trial_id, revision.predecessor)
    if version == 1:
        evidence = StationEvidence(origin, revision.exits, compile_first_exits(revision))
        body = encode_evidence(evidence)
    else:
        graph = compile_scan_graph(revision)
        evidence = StationScanEvidence(origin, graph.exits, graph.entries, graph.nodes, graph.edges)
        body = encode_scan_evidence(evidence)
    registry.record(body)
    return body
