from dataclasses import dataclass
from fractions import Fraction

from retest.evidence.encoding import EvidenceOrigin, StationScanEvidence
from retest.wiring.manifest import Manifest
from retest.wiring.ports import PortRef
from retest.wiring.storage import ManifestStore


@dataclass(frozen=True)
class JointEdge:
    ticks: int
    scan: str | None
    probability: Fraction
    complete: bool
    target: int | None


@dataclass(frozen=True)
class JointAutomaton:
    manifest: Manifest
    origins: tuple[EvidenceOrigin, EvidenceOrigin]
    nodes: tuple[tuple[str, str], ...]
    entrances: dict[PortRef, int]
    edges: tuple[tuple[JointEdge, ...], ...]


def build_joint_automaton(store: ManifestStore, manifest_id: str) -> JointAutomaton:
    manifest, pair = store.load(manifest_id)
    first, second = pair
    if not (isinstance(first.evidence, StationScanEvidence)
            and isinstance(second.evidence, StationScanEvidence)):
        raise ValueError('scan review requires version 2 station evidence')
    nodes: list[tuple[str, str]] = []
    node_index: dict[tuple[str, str], int] = {}
    entrances: dict[PortRef, int] = {}
    for received in pair:
        evidence = received.evidence
        piece_id = received.piece_id
        for node in evidence.nodes:
            node_index[(piece_id, node)] = len(nodes)
            nodes.append((piece_id, node))
        for entrance, node in evidence.entries:
            entrances[PortRef(piece_id, entrance)] = node_index[(piece_id, node)]
    connections = dict(manifest.connections)
    edges: list[tuple[JointEdge, ...]] = [()] * len(nodes)
    for received in pair:
        evidence = received.evidence
        piece_id = received.piece_id
        per_node: dict[str, list[JointEdge]] = {node: [] for node in evidence.nodes}
        for edge in evidence.edges:
            mass = Fraction(edge.probability)
            if edge.is_exit:
                destination = connections[PortRef(piece_id, edge.target)]
                if destination is None:
                    joint_edge = JointEdge(edge.ticks, edge.scan, mass, True, None)
                else:
                    joint_edge = JointEdge(edge.ticks, edge.scan, mass, False, entrances[destination])
            else:
                joint_edge = JointEdge(edge.ticks, edge.scan, mass, False,
                                       node_index[(piece_id, edge.target)])
            per_node[edge.source].append(joint_edge)
        for node, node_edges in per_node.items():
            edges[node_index[(piece_id, node)]] = tuple(node_edges)
    if any(not node_edges for node_edges in edges):
        raise ValueError('joint automaton node has no outgoing branches')
    return JointAutomaton(manifest, (first.evidence.origin, second.evidence.origin),
                          tuple(nodes), entrances, tuple(edges))
