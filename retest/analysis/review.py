from dataclasses import dataclass
from fractions import Fraction

from retest.analysis.automaton import build_joint_automaton
from retest.analysis.deadline import calculate_deadline_probability
from retest.analysis.handoff import resolve_handoff
from retest.analysis.noncompletion import calculate_noncompletion_probability
from retest.analysis.scans import evaluate_scan_window
from retest.evidence.encoding import StationScanEvidence
from retest.reports.ledger import IssuedReport, ReportLedger
from retest.reports.publish import publish
from retest.reports.snapshot import build_snapshot
from retest.wiring.ports import PortRef
from retest.wiring.storage import ManifestStore


@dataclass(frozen=True)
class ReviewRequest:
    manifest_id: str
    start: PortRef
    deadline: int


@dataclass(frozen=True)
class ScanReviewRequest:
    manifest_id: str
    start: PortRef
    observed_through: int
    scans: tuple[tuple[int, str], ...] | list[tuple[int, str], ...]
    deadline: int


@dataclass(frozen=True)
class Judgment:
    on_time: Fraction
    never_complete: Fraction

    def __post_init__(self) -> None:
        if self.on_time < 0 or self.never_complete < 0 or self.on_time + self.never_complete > 1:
            raise ValueError('inconsistent completion probabilities')


def _validate_scan_request(request: ScanReviewRequest) -> tuple[int, int, tuple[tuple[int, str], ...]]:
    if type(request.observed_through) is not int or isinstance(request.observed_through, bool):
        raise ValueError('observed_through must be a non-boolean integer tick')
    if type(request.deadline) is not int or isinstance(request.deadline, bool):
        raise ValueError('deadline must be a non-boolean integer tick')
    if request.observed_through < 0 or request.deadline < 0:
        raise ValueError('review ticks must be nonnegative')
    if request.observed_through > request.deadline:
        raise ValueError('observed_through must not exceed the deadline')
    if isinstance(request.scans, (str, bytes)) or not isinstance(request.scans, (list, tuple)):
        raise ValueError('scans must be a sequence of tick and name pairs')
    normalized = []
    previous_tick = 0
    first = True
    for item in request.scans:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError('each scan must be a tick and name pair')
        tick, name = item
        if type(tick) is not int or isinstance(tick, bool) or not 0 <= tick <= request.observed_through:
            raise ValueError('scan tick must be a non-boolean integer inside the window')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('scan name must be a nonblank string')
        if not first and tick < previous_tick:
            raise ValueError('scans must be ordered by tick')
        normalized.append((tick, name))
        previous_tick = tick
        first = False
    return request.observed_through, request.deadline, tuple(normalized)

def review(manifests: ManifestStore, ledger: ReportLedger,
           request: ReviewRequest | ScanReviewRequest) -> IssuedReport:
    if isinstance(request, ScanReviewRequest):
        return _review_scan(manifests, ledger, request)
    if not isinstance(request, ReviewRequest) or type(request.deadline) is not int or request.deadline < 0:
        raise ValueError('review requires a nonnegative integer deadline')
    _manifest, received = manifests.load(request.manifest_id)
    if any(isinstance(item.evidence, StationScanEvidence) for item in received):
        raise ValueError('the classic review request requires version 1 station evidence')
    handoff = resolve_handoff(manifests, request.manifest_id, request.start)
    judgment = Judgment(calculate_deadline_probability(handoff, request.deadline),
                        calculate_noncompletion_probability(handoff))
    snapshot = build_snapshot(handoff.manifest, handoff.origins, handoff.start, request.deadline,
                              judgment.on_time, judgment.never_complete)
    return publish(ledger, snapshot)


def _review_scan(manifests: ManifestStore, ledger: ReportLedger,
                 request: ScanReviewRequest) -> IssuedReport:
    observed_through, deadline, scans = _validate_scan_request(request)
    automaton = build_joint_automaton(manifests, request.manifest_id)
    if request.start not in automaton.entrances:
        raise ValueError('scan review start must be a public entrance of the pinned pieces')
    start_node = automaton.entrances[request.start]
    judgment = evaluate_scan_window(automaton, start_node, observed_through, deadline, scans)
    snapshot = build_snapshot(automaton.manifest, automaton.origins, request.start, deadline,
                              judgment.on_time, judgment.never_complete,
                              observed_through, scans, judgment.support)
    return publish(ledger, snapshot)
