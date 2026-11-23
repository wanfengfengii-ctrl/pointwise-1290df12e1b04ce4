from dataclasses import dataclass
from fractions import Fraction

from retest.analysis.automaton import JointAutomaton
from retest.analysis.same_tick import _inverse, build_same_tick_closure


ZERO = Fraction(0)
ONE = Fraction(1)


@dataclass(frozen=True)
class Bundle:
    """Per-unit mass split produced by entering a node within one tick.

    ``trap`` is mass confined in a closed zero-tick component at the bundle's
    own level; ``drop`` is mass discarded because a labeled edge could not be
    matched against the remaining observation word. Only level-zero traps are
    compatible; higher-level traps surface as drops at the driver boundary.
    """

    complete: Fraction
    scheduled: tuple[tuple[int, str | None, int | None, Fraction], ...]
    trap: Fraction
    drop: Fraction = ZERO


EMPTY = Bundle(ZERO, (), ZERO, ZERO)
TRAP = Bundle(ZERO, (), ONE, ZERO)
DROPPED = Bundle(ZERO, (), ZERO, ONE)


def scale(bundle: Bundle, factor: Fraction) -> Bundle:
    if not factor:
        return EMPTY
    return Bundle(bundle.complete * factor,
                  tuple((ticks, scan, node, mass * factor) for ticks, scan, node, mass in bundle.scheduled),
                  bundle.trap * factor, bundle.drop * factor)


def combine(left: Bundle, right: Bundle) -> Bundle:
    scheduled: dict[tuple[int, str | None, int | None], Fraction] = {}
    for ticks, scan, node, mass in (*left.scheduled, *right.scheduled):
        key = (ticks, scan, node)
        scheduled[key] = scheduled.get(key, ZERO) + mass
    return Bundle(left.complete + right.complete,
                  tuple((ticks, scan, node, mass)
                        for (ticks, scan, node), mass in sorted(scheduled.items()) if mass),
                  left.trap + right.trap, left.drop + right.drop)


class Transducer:
    """Resolves every same-tick chain for one observed tick word.

    The tick word is the ordered tuple of scans observed this tick; ``None``
    means the tick is outside the observation window, so labels are
    unconstrained. Levels count labels still available to this transducer:
    level L matches the next labeled zero edge against
    ``word[len(word) - L]`` and drops to L - 1, so the top level reads the
    first observation and level zero has none left. Unlabeled zero edges keep
    the level and are resolved as a linear system. Positive-tick edges and
    zero-tick completions leave the tick immediately.
    """

    def __init__(self, automaton: JointAutomaton, word: tuple[str, ...] | None):
        self.automaton = automaton
        self.word = word
        self.checking = word is not None
        self.size = len(automaton.edges)
        self._solutions: list[tuple[Bundle, ...]] = []
        top_level = len(word) if self.checking else 0
        for level in range(top_level + 1):
            self._solutions.append(self._build_level(level))

    def _edge_allowed(self, scan: str | None, level: int) -> tuple[bool, int]:
        """Return (edge survives, level after crossing).

        Level L means L labels remain; the next one to consume is
        ``word[levels - level]``. Outside the window, or for a tick with an
        empty word, nothing was observed so every edge passes without
        consuming a level.
        """
        if scan is None or not self.checking or not self.word:
            return True, level
        if level == 0 or self.word[len(self.word) - level] != scan:
            return False, level
        return True, level - 1

    def _build_level(self, level: int) -> tuple[Bundle, ...]:
        matrix = [[ZERO] * self.size for _ in range(self.size)]
        predecessors: dict[int, set[int]] = {node: set() for node in range(self.size)}
        leaking = [ZERO] * self.size
        for node, edges in enumerate(self.automaton.edges):
            for edge in edges:
                allowed, next_level = self._edge_allowed(edge.scan, level)
                if not allowed:
                    continue
                if edge.ticks == 0 and not edge.complete and next_level == level:
                    matrix[node][edge.target] += edge.probability
                    predecessors[edge.target].add(node)
                else:
                    # Completions, positive-tick edges and level-descending
                    # zero edges leave the same-level subgraph. Same-level
                    # zero edges stay inside it, even when they lead to a
                    # closed component that will trap the mass.
                    leaking[node] += edge.probability
        # A node is transient if it leaks or can reach a leaking node through
        # surviving same-level zero edges; everything else is a closed
        # component, whose mass stays trapped inside the tick.
        transient = {node for node, mass in enumerate(leaking) if mass}
        pending = list(transient)
        while pending:
            newly = predecessors[pending.pop()] - transient
            transient.update(newly)
            pending.extend(newly)
        order = tuple(sorted(transient))
        equations = [[(Fraction(source == target) - matrix[source][target]) for target in order]
                     for source in order]
        inverse = _inverse(equations)
        bases: dict[int, Bundle] = {}
        for node in order:
            complete = ZERO
            scheduled: dict[tuple[int, str | None, int | None], Fraction] = {}
            chained = EMPTY
            for edge in self.automaton.edges[node]:
                allowed, next_level = self._edge_allowed(edge.scan, level)
                if not allowed:
                    # A labeled edge with no matching observed label makes the
                    # whole branch incompatible with this tick word.
                    chained = combine(chained, scale(DROPPED, edge.probability))
                    continue
                if edge.ticks > 0:
                    key = (edge.ticks, edge.scan, None if edge.complete else edge.target)
                    scheduled[key] = scheduled.get(key, ZERO) + edge.probability
                elif edge.complete:
                    if next_level == 0 or not self.checking:
                        # Every observed label this tick has been accounted for.
                        complete += edge.probability
                    else:
                        # The path ends while observed scans remain, so its
                        # scan sequence cannot equal the recorded tick word.
                        chained = combine(chained, scale(DROPPED, edge.probability))
                elif next_level < level:
                    nested = self._solutions[next_level][edge.target]
                    if level > 0 and nested.trap:
                        # A trap reached while observed labels still remain can
                        # never generate the scans the observation requires.
                        nested = Bundle(nested.complete, nested.scheduled, ZERO,
                                        nested.drop + nested.trap)
                    chained = combine(chained, scale(nested, edge.probability))
                elif edge.target not in transient:
                    # A same-level step into a closed component confines the
                    # mass in an infinite zero-tick chain. It is only
                    # compatible when no observed labels remain to match.
                    chained = combine(chained, scale(TRAP if level == 0 else DROPPED, edge.probability))
            bases[node] = combine(Bundle(complete,
                                         tuple((ticks, scan, target, mass)
                                               for (ticks, scan, target), mass in sorted(scheduled.items()) if mass),
                                         ZERO, ZERO), chained)
        results: list[Bundle] = [TRAP if level == 0 else DROPPED] * self.size
        for target_index, node in enumerate(order):
            bundle = EMPTY
            for source_index, source in enumerate(order):
                coefficient = inverse[target_index][source_index]
                if coefficient:
                    bundle = combine(bundle, scale(bases[source], coefficient))
            results[node] = bundle
        return tuple(results)

    def solve(self, node: int, level: int) -> Bundle:
        return self._solutions[level][node]


def never_complete_vector(automaton: JointAutomaton) -> tuple[Fraction, ...]:
    """Mass, per node, that never reaches a wiring completion point."""
    size = len(automaton.edges)
    direct = [ZERO] * size
    continuation = [[ZERO] * size for _ in range(size)]
    predecessors: dict[int, set[int]] = {node: set() for node in range(size)}
    for node, edges in enumerate(automaton.edges):
        for edge in edges:
            if edge.complete:
                direct[node] += edge.probability
            else:
                continuation[node][edge.target] += edge.probability
                predecessors[edge.target].add(node)
    reachable = {node for node, mass in enumerate(direct) if mass}
    pending = list(reachable)
    while pending:
        newly = predecessors[pending.pop()] - reachable
        reachable.update(newly)
        pending.extend(newly)
    result = [ONE] * size
    if reachable:
        order = tuple(sorted(reachable))
        closure = build_same_tick_closure([[continuation[source][target] for target in order] for source in order])
        eventual = closure.solve([direct[source] for source in order])
        for index, node in enumerate(order):
            result[node] = ONE - eventual[index]
    return tuple(result)


@dataclass(frozen=True)
class ScanJudgment:
    support: Fraction
    on_time: Fraction | None
    never_complete: Fraction | None


def evaluate_scan_window(automaton: JointAutomaton, start_node: int,
                         observed_through: int, deadline: int,
                         scans: tuple[tuple[int, str], ...]) -> ScanJudgment:
    words: dict[int, tuple[str, ...]] = {}
    for tick, name in scans:
        words[tick] = words.get(tick, ()) + (name,)
    # Pending edge arrivals: fire tick -> (scan, target, mass).
    pending: dict[int, list[tuple[str | None, int | None, Fraction]]] = {}
    good_trap = ZERO
    on_time_num = ZERO
    q_vector = never_complete_vector(automaton)

    def schedule(fire_tick: int, scan: str | None, target: int | None, mass: Fraction) -> None:
        if mass:
            pending.setdefault(fire_tick, []).append((scan, target, mass))

    for tick in range(deadline + 1):
        checking = tick <= observed_through
        word = words.get(tick, ()) if checking else None
        transducer = Transducer(automaton, word)
        arrivals = pending.pop(tick, [])
        if tick == 0:
            arrivals.append((None, start_node, ONE))
        for scan, target, mass in arrivals:
            if target is None:
                # A scheduled completion event: only its arrival scan remains.
                if checking and scan is not None:
                    if not word or word[0] != scan:
                        continue
                if tick <= deadline:
                    on_time_num += mass
                continue
            if checking and scan is not None:
                if not word or word[0] != scan:
                    continue
                # The arrival consumed the first label; the transducer keeps
                # the rest.
                level = len(word) - 1
            else:
                level = len(word) if checking else 0
            bundle = scale(transducer.solve(target, level), mass)
            if bundle.complete and tick <= deadline:
                on_time_num += bundle.complete
            if bundle.trap and level == 0:
                good_trap += bundle.trap
            # bundle.drop and level-positive traps are incompatible and vanish.
            for ticks, edge_scan, edge_target, edge_mass in bundle.scheduled:
                schedule(tick + ticks, edge_scan, edge_target, edge_mass)
        if tick == observed_through:
            in_transit = sum((mass for later in pending.values()
                              for _scan, _target, mass in later), ZERO)
            support = good_trap + in_transit + on_time_num
            never_num = good_trap + sum(
                mass * (ZERO if target is None else q_vector[target])
                for later in pending.values() for _scan, target, mass in later)

    if support == ZERO:
        return ScanJudgment(ZERO, None, None)
    return ScanJudgment(support, on_time_num / support, never_num / support)
