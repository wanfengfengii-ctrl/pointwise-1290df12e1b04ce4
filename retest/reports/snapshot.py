import json
from dataclasses import asdict, dataclass
from fractions import Fraction

from retest.evidence.encoding import EvidenceOrigin
from retest.wiring.manifest import Manifest
from retest.wiring.ports import PortRef


@dataclass(frozen=True)
class ReportSnapshot:
    manifest: Manifest
    origins: tuple[EvidenceOrigin, EvidenceOrigin]
    start: PortRef
    deadline: int
    on_time: Fraction | None
    never_complete: Fraction | None
    observed_through: int | None = None
    scans: tuple[tuple[int, str], ...] | None = None
    support: Fraction | None = None


def build_snapshot(manifest: Manifest, origins: tuple[EvidenceOrigin, EvidenceOrigin], start: PortRef,
                   deadline: int, on_time: Fraction | None, never_complete: Fraction | None,
                   observed_through: int | None = None,
                   scans: tuple[tuple[int, str], ...] | None = None,
                   support: Fraction | None = None) -> ReportSnapshot:
    if len(origins) != 2 or start.piece_id not in manifest.parents:
        raise ValueError('report requires its pinned pair and starting entrance')
    if type(deadline) is not int or deadline < 0:
        raise ValueError('invalid snapshot deadline')
    is_scan = observed_through is not None or scans is not None or support is not None
    if is_scan:
        if observed_through is None or scans is None or support is None:
            raise ValueError('scan snapshot requires the window, observations and support')
        if type(observed_through) is not int or observed_through < 0 or observed_through > deadline:
            raise ValueError('invalid scan observation window')
        if not isinstance(support, Fraction) or support < 0 or support > 1:
            raise ValueError('invalid scan support')
        if (on_time is None) != (never_complete is None):
            raise ValueError('conditional probabilities must be present or absent together')
        if on_time is not None:
            if not isinstance(on_time, Fraction) or not isinstance(never_complete, Fraction):
                raise ValueError('conditional probabilities must be exact')
            if on_time < 0 or never_complete < 0 or on_time + never_complete > 1:
                raise ValueError('invalid conditional probability judgment')
    else:
        if not isinstance(on_time, Fraction) or not isinstance(never_complete, Fraction):
            raise ValueError('report probabilities must be exact')
        if on_time < 0 or never_complete < 0 or on_time + never_complete > 1:
            raise ValueError('invalid probability judgment')
    return ReportSnapshot(manifest, origins, start, deadline, on_time, never_complete,
                          observed_through, scans, support)


def encode_snapshot(snapshot: ReportSnapshot) -> str:
    payload = {
        'manifest': snapshot.manifest.to_record(),
        'origins': [asdict(origin) for origin in snapshot.origins],
        'start': snapshot.start.to_record(),
        'deadline': snapshot.deadline,
        'on_time': None if snapshot.on_time is None else str(snapshot.on_time),
        'never_complete': None if snapshot.never_complete is None else str(snapshot.never_complete),
    }
    if snapshot.observed_through is not None:
        payload['observed_through'] = snapshot.observed_through
        payload['scans'] = [[tick, name] for tick, name in snapshot.scans]
        payload['support'] = str(snapshot.support)
    return json.dumps(payload, sort_keys=True, separators=(',', ':'))


def decode_snapshot(body: str) -> ReportSnapshot:
    data = json.loads(body)
    first, second = data['origins']
    on_time = None if data['on_time'] is None else Fraction(data['on_time'])
    never_complete = None if data['never_complete'] is None else Fraction(data['never_complete'])
    if 'observed_through' in data:
        scans = tuple((tick, name) for tick, name in data['scans'])
        return build_snapshot(Manifest.from_record(data['manifest']),
                              (EvidenceOrigin(**first), EvidenceOrigin(**second)),
                              PortRef.from_record(data['start']), data['deadline'],
                              on_time, never_complete,
                              data['observed_through'], scans, Fraction(data['support']))
    return build_snapshot(Manifest.from_record(data['manifest']),
                          (EvidenceOrigin(**first), EvidenceOrigin(**second)),
                          PortRef.from_record(data['start']), data['deadline'],
                          on_time, never_complete)
