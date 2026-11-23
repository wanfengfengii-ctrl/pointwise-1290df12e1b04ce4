from dataclasses import dataclass

from retest.revisions.model import Revision


@dataclass(frozen=True)
class ScanEdge:
    """A public branch of the scan automaton.

    Private state names never appear here: internal targets are opaque node
    ids, while exit targets keep their public exit names.
    """

    source: str
    target: str
    is_exit: bool
    probability: str
    ticks: int
    scan: str | None


@dataclass(frozen=True)
class ScanGraph:
    exits: tuple[str, ...]
    entries: tuple[tuple[str, str], ...]
    nodes: tuple[str, ...]
    edges: tuple[ScanEdge, ...]


def _reachable_states(revision: Revision) -> list[str]:
    rows = dict(revision.transitions)
    roots = {state for _, state in revision.entries}
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        for edge in rows[state]:
            if not edge.is_exit and edge.target not in seen:
                pending.append(edge.target)
    return sorted(seen)


def compile_scan_graph(revision: Revision) -> ScanGraph:
    """Project a revision onto its public scan automaton."""
    rows = dict(revision.transitions)
    states = _reachable_states(revision)
    node_id = {state: f'n{index}' for index, state in enumerate(states)}
    entries = tuple(sorted((entrance, node_id[state]) for entrance, state in revision.entries))
    edges = []
    for state in states:
        for edge in rows[state]:
            target = edge.target if edge.is_exit else node_id[edge.target]
            edges.append(ScanEdge(node_id[state], target, edge.is_exit,
                                  str(edge.probability), edge.ticks, edge.scan))
    return ScanGraph(tuple(sorted(revision.exits)), entries, tuple(node_id[state] for state in states),
                     tuple(edges))
