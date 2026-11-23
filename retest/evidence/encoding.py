import json
from dataclasses import asdict, dataclass
from fractions import Fraction
from hashlib import sha256

from retest.evidence.scan_graph import ScanEdge, ScanGraph
from retest.evidence.series import EntryExitEvidence, ExitSeries, Polynomial


@dataclass(frozen=True)
class EvidenceOrigin:
    station: str
    revision_id: str
    work_order: str
    round_id: str
    trial_id: str
    predecessor: str | None


@dataclass(frozen=True)
class StationEvidence:
    origin: EvidenceOrigin
    exits: tuple[str, ...]
    entries: tuple[tuple[str, EntryExitEvidence], ...]

    @property
    def piece_id(self) -> str:
        return _identity(_payload(self))


@dataclass(frozen=True)
class StationScanEvidence:
    """Version 2 evidence: public scan automaton with no private detail."""

    origin: EvidenceOrigin
    exits: tuple[str, ...]
    entries: tuple[tuple[str, str], ...]
    nodes: tuple[str, ...]
    edges: tuple[ScanEdge, ...]

    @property
    def piece_id(self) -> str:
        return _identity(_scan_payload(self))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _identity(payload: dict) -> str:
    return sha256(_json(payload).encode('ascii')).hexdigest()


def _name(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('blank or invalid public identity')


def _probability(value: Fraction) -> Fraction:
    if not 0 <= value <= 1:
        raise ValueError('evidence mass outside [0, 1]')
    return value


def _polynomial(value: Polynomial) -> list[list]:
    return [[degree, str(coefficient)] for degree, coefficient in value]


def _payload(evidence: StationEvidence) -> dict:
    origin = asdict(evidence.origin)
    for key, value in origin.items():
        if key != 'predecessor' or value is not None:
            _name(value)
    if origin['predecessor'] == origin['revision_id']:
        raise ValueError('invalid evidence predecessor')
    for name in evidence.exits:
        _name(name)
    exit_set = set(evidence.exits)
    if not exit_set or len(exit_set) != len(evidence.exits) or not evidence.entries:
        raise ValueError('evidence requires distinct exits and at least one entrance')
    entries = {}
    for entrance, evidence_row in evidence.entries:
        _name(entrance)
        if entrance in entries:
            raise ValueError('duplicate entrance evidence')
        series_by_name = {}
        total = _probability(evidence_row.never_exit)
        for name, series in evidence_row.exits:
            if name in series_by_name:
                raise ValueError('duplicate exit evidence')
            mass = _probability(series.total_mass())
            immediate = series.coefficients_through(0)[0]
            if not 0 <= immediate <= mass or (not mass and series):
                raise ValueError('exit series contradicts its total mass')
            total += mass
            series_by_name[name] = {
                'numerator': _polynomial(series.numerator),
                'denominator': _polynomial(series.denominator),
            }
        if series_by_name.keys() != exit_set or total != 1:
            raise ValueError('entrance evidence is missing exits or does not conserve mass')
        entries[entrance] = {'exits': series_by_name, 'never_exit': str(evidence_row.never_exit)}
    return {'version': 1, 'kind': 'station-evidence', 'origin': origin,
            'body': {'exits': list(sorted(exit_set)), 'entries': entries}}


def encode_evidence(evidence: StationEvidence) -> str:
    payload = _payload(evidence)
    return _json({**payload, 'id': _identity(payload)})


def _edge_record(edge: ScanEdge) -> dict:
    target = {'exit': edge.target} if edge.is_exit else {'node': edge.target}
    return {'source': edge.source, 'target': target, 'probability': edge.probability,
            'ticks': edge.ticks, 'scan': edge.scan}


def _scan_payload(evidence: StationScanEvidence) -> dict:
    origin = asdict(evidence.origin)
    for key, value in origin.items():
        if key != 'predecessor' or value is not None:
            _name(value)
    if origin['predecessor'] == origin['revision_id']:
        raise ValueError('invalid evidence predecessor')
    exits = tuple(evidence.exits)
    for name in exits:
        _name(name)
    exit_set = set(exits)
    if not exit_set or len(exit_set) != len(exits):
        raise ValueError('scan evidence requires distinct exits')
    nodes = tuple(evidence.nodes)
    node_set = set(nodes)
    if not node_set or len(node_set) != len(nodes):
        raise ValueError('scan evidence requires distinct nodes')
    for node in nodes:
        _name(node)
    entry_names = set()
    entry_targets = set()
    for entrance, node in evidence.entries:
        _name(entrance)
        if entrance in entry_names:
            raise ValueError('duplicate entrance in scan evidence')
        if node not in node_set:
            raise ValueError('scan entrance targets an unknown node')
        entry_names.add(entrance)
        entry_targets.add(node)
    if not entry_names:
        raise ValueError('scan evidence requires at least one entrance')
    outgoing: dict[str, list[ScanEdge]] = {node: [] for node in nodes}
    edge_records = []
    for edge in evidence.edges:
        if edge.source not in node_set:
            raise ValueError('scan edge leaves an unknown node')
        if edge.is_exit:
            if edge.target not in exit_set:
                raise ValueError('scan edge exits at an unknown public exit')
        elif edge.target not in node_set:
            raise ValueError('scan edge targets an unknown node')
        if type(edge.ticks) is not int or edge.ticks < 0:
            raise ValueError('scan edge ticks must be nonnegative integers')
        if edge.scan is not None:
            _name(edge.scan)
        mass = _probability(Fraction(edge.probability))
        outgoing[edge.source].append(edge)
        edge_records.append(_edge_record(edge))
    reachable = set(entry_targets)
    pending = list(reachable)
    while pending:
        node = pending.pop()
        for edge in outgoing[node]:
            if not edge.is_exit and edge.target not in reachable and Fraction(edge.probability) >= 0:
                reachable.add(edge.target)
                pending.append(edge.target)
    if reachable != node_set:
        raise ValueError('scan evidence contains nodes unreachable from a public entrance')
    for node, branches in outgoing.items():
        if not branches:
            raise ValueError(f'scan node {node!r} has no outgoing branches')
        if sum((Fraction(edge.probability) for edge in branches), Fraction(0)) != 1:
            raise ValueError(f'scan node {node!r} branch mass must sum to one')
    edge_records.sort(key=lambda record: (
        record['source'], 0 if 'node' in record['target'] else 1,
        record['target'].get('node', record['target'].get('exit')), record['ticks'],
        '' if record['scan'] is None else record['scan'], record['probability']))
    body = {'exits': list(sorted(exit_set)),
            'entries': [[name, node] for name, node in sorted(evidence.entries)],
            'nodes': list(nodes), 'edges': edge_records}
    return {'version': 2, 'kind': 'station-evidence', 'origin': origin, 'body': body}


def encode_scan_evidence(evidence: StationScanEvidence) -> str:
    payload = _scan_payload(evidence)
    return _json({**payload, 'id': _identity(payload)})


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field in evidence JSON')
        result[key] = value
    return result


def _fields(value: object, *keys: str) -> dict:
    if not isinstance(value, dict) or value.keys() != set(keys):
        raise ValueError('invalid evidence fields')
    return value


def _fraction(value: object) -> Fraction:
    if not isinstance(value, str):
        raise ValueError('evidence rational must be a string')
    return Fraction(value)


def _read_polynomial(value: object) -> Polynomial:
    if not isinstance(value, list):
        raise ValueError('evidence polynomial must be a list of terms')
    terms = []
    for term in value:
        if not isinstance(term, list) or len(term) != 2:
            raise ValueError('invalid evidence polynomial term')
        terms.append((term[0], _fraction(term[1])))
    return tuple(terms)


def decode_evidence(body: str) -> StationEvidence | StationScanEvidence:
    if not isinstance(body, str):
        raise ValueError('evidence must be JSON text')
    try:
        raw = _fields(json.loads(body, object_pairs_hook=_unique_object), 'version', 'kind', 'id', 'origin', 'body')
        if type(raw['version']) is not int or raw['kind'] != 'station-evidence':
            raise ValueError('unsupported evidence envelope')
        if raw['version'] == 1:
            evidence = _decode_v1(raw)
        elif raw['version'] == 2:
            evidence = _decode_v2(raw)
        else:
            raise ValueError('unsupported evidence envelope')
        if _json(raw) != _canonical(evidence):
            raise ValueError('station evidence identity or canonical values do not match')
        return evidence
    except (ZeroDivisionError, RecursionError) as error:
        raise ValueError('invalid evidence body') from error


def _canonical(evidence: StationEvidence | StationScanEvidence) -> str:
    if isinstance(evidence, StationScanEvidence):
        payload = _scan_payload(evidence)
    else:
        payload = _payload(evidence)
    return _json({**payload, 'id': _identity(payload)})


def _decode_v1(raw: dict) -> StationEvidence:
    if raw['version'] != 1:
        raise ValueError('unsupported evidence envelope')
    origin = EvidenceOrigin(**_fields(raw['origin'], 'station', 'revision_id', 'work_order', 'round_id', 'trial_id', 'predecessor'))
    data = _fields(raw['body'], 'exits', 'entries')
    if not isinstance(data['exits'], list) or not isinstance(data['entries'], dict):
        raise ValueError('invalid public port evidence')
    entries = []
    for name, row in sorted(data['entries'].items()):
        row = _fields(row, 'exits', 'never_exit')
        if not isinstance(row['exits'], dict):
            raise ValueError('invalid exit evidence map')
        exits = []
        for exit_name, item in sorted(row['exits'].items()):
            item = _fields(item, 'numerator', 'denominator')
            exits.append((exit_name, ExitSeries(_read_polynomial(item['numerator']), _read_polynomial(item['denominator']))))
        entries.append((name, EntryExitEvidence(tuple(exits), _fraction(row['never_exit']))))
    return StationEvidence(origin, tuple(data['exits']), tuple(entries))


def _decode_v2(raw: dict) -> StationScanEvidence:
    origin = EvidenceOrigin(**_fields(raw['origin'], 'station', 'revision_id', 'work_order', 'round_id', 'trial_id', 'predecessor'))
    data = _fields(raw['body'], 'exits', 'entries', 'nodes', 'edges')
    exits, nodes, entries_raw, edges_raw = (data['exits'], data['nodes'], data['entries'], data['edges'])
    if not (isinstance(exits, list) and isinstance(nodes, list)
            and isinstance(entries_raw, list) and isinstance(edges_raw, list)):
        raise ValueError('invalid scan evidence body')
    entries = []
    for item in entries_raw:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise ValueError('invalid scan entrance record')
        entries.append((item[0], item[1]))
    edges = []
    for item in edges_raw:
        item = _fields(item, 'source', 'target', 'probability', 'ticks', 'scan')
        target = item['target']
        if not isinstance(target, dict) or len(target) != 1 or not (set(target) == {'node'} or set(target) == {'exit'}):
            raise ValueError('invalid scan edge target')
        is_exit = 'exit' in target
        if not isinstance(item['source'], str) or not isinstance(target[next(iter(target))], str):
            raise ValueError('invalid scan edge endpoints')
        if type(item['ticks']) is not int or item['ticks'] < 0:
            raise ValueError('invalid scan edge ticks')
        if item['scan'] is not None and (not isinstance(item['scan'], str) or not item['scan'].strip()):
            raise ValueError('invalid scan edge label')
        fraction = _fraction(item['probability'])
        edges.append(ScanEdge(item['source'], target[next(iter(target))], is_exit,
                              str(fraction), item['ticks'], item['scan']))
    evidence = StationScanEvidence(origin, tuple(exits), tuple(entries), tuple(nodes), tuple(edges))
    # Full structural validation happens inside the canonical payload builder.
    _scan_payload(evidence)
    return evidence
