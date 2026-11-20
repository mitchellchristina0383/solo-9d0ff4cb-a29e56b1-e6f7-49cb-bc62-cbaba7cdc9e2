"""Cgroup cpuset re-pinning for the online and batch synthesis groups.

Two workload groups (``online`` and ``batch``) live under one simulated parent
cgroup on the local file system.  The parent owns at most eight usable CPUs.
A re-pin request names the target CPU set of *both* groups; the controller
works out a sequence of single-group cpuset writes that keeps both groups
running throughout:

* only one group's ``cpuset.cpus`` is changed per step;
* after every step each group still holds at least its minimum CPU count;
* the two groups never share a CPU;
* idle parent CPUs may be borrowed temporarily to make room for a swap;
* if no legal sequence reaches the targets, the existing allocation is kept
  untouched and the request is rejected.

Durability model
----------------
Each adjustment gets a monotonically increasing *operation id* and a JSON
journal entry (written atomically, ``tmp + rename``).  A simulated cpuset
write that is interrupted leaves the previous file in place (only a stale
``*.tmp`` file may remain), so after a crash the controller can re-read the
*actual* allocation, re-plan from there to the remembered targets and
continue under the same operation id.  While an operation is unfinished every
other adjustment is rejected.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# constants and errors
# --------------------------------------------------------------------------- #

ONLINE = "online"
BATCH = "batch"
GROUPS: Tuple[str, ...] = (ONLINE, BATCH)

MAX_PARENT_CPUS = 8
DEFAULT_PARENT_CPUS: FrozenSet[int] = frozenset(range(8))
DEFAULT_MIN_CPUS: Dict[str, int] = {ONLINE: 2, BATCH: 1}
DEFAULT_INITIAL_CPUS: Dict[str, FrozenSet[int]] = {
    ONLINE: frozenset({0, 1, 2}),
    BATCH: frozenset({3}),
}

PARENT_DIRNAME = "parent"
JOURNAL_DIRNAME = "journal"
OPS_DIRNAME = "ops"
LOCK_FILENAME = "lock"
EFFECTIVE_CPUS_FILENAME = "cpuset.cpus.effective"
CPUSET_FILENAME = "cpuset.cpus"

# A state is (online cpus, batch cpus); a move is (group name, new cpu set).
State = Tuple[FrozenSet[int], FrozenSet[int]]
Move = Tuple[str, FrozenSet[int]]


class CpusetError(Exception):
    """Base class for all cpuset controller errors."""


class InvalidSpecError(CpusetError):
    """A requested CPU specification is semantically invalid (HTTP 400)."""


class NoPlanError(CpusetError):
    """No legal step sequence can reach the requested targets (HTTP 409)."""


class AdjustmentInProgressError(CpusetError):
    """Another operation is unfinished and blocks this request (HTTP 409)."""


class OperationNotFoundError(CpusetError):
    """The referenced operation id does not exist (HTTP 404)."""


class CgroupWriteFault(CpusetError):
    """Raised by the fault-injection hook to simulate an interrupted write."""


# --------------------------------------------------------------------------- #
# cpuset list formatting helpers (cpuset(7) style: "0-3,5")
# --------------------------------------------------------------------------- #

def parse_cpus(text: str) -> FrozenSet[int]:
    """Parse a cpuset list specification such as ``0-2,5`` into a set."""
    cpus: set[int] = set()
    text = text.strip()
    if not text:
        raise InvalidSpecError("empty cpuset specification")
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise InvalidSpecError(f"malformed cpuset specification {text!r}")
        if "-" in chunk:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise InvalidSpecError(f"malformed cpu range {chunk!r}") from exc
            if lo < 0 or hi < lo:
                raise InvalidSpecError(f"invalid cpu range {chunk!r}")
            cpus.update(range(lo, hi + 1))
        else:
            try:
                cpu = int(chunk)
            except ValueError as exc:
                raise InvalidSpecError(f"malformed cpu id {chunk!r}") from exc
            if cpu < 0:
                raise InvalidSpecError(f"cpu ids must be non-negative, got {cpu}")
            cpus.add(cpu)
    return frozenset(cpus)


def format_cpus(cpus: Iterable[int]) -> str:
    """Render a CPU set as a canonical cpuset list ("0-2,5")."""
    ordered = sorted(cpus)
    if not ordered:
        raise InvalidSpecError("a cpuset must contain at least one CPU")
    ranges: List[str] = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = cpu
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CpusetConfig:
    """Static topology of the simulated parent cgroup."""

    parent_cpus: FrozenSet[int] = DEFAULT_PARENT_CPUS
    min_cpus: Dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_MIN_CPUS)
    )
    initial_cpus: Dict[str, FrozenSet[int]] = field(
        default_factory=lambda: {k: frozenset(v) for k, v in DEFAULT_INITIAL_CPUS.items()}
    )

    def __post_init__(self) -> None:
        if len(self.parent_cpus) > MAX_PARENT_CPUS:
            raise InvalidSpecError(
                f"the parent cgroup owns at most {MAX_PARENT_CPUS} CPUs, "
                f"got {len(self.parent_cpus)}"
            )
        if not self.parent_cpus:
            raise InvalidSpecError("the parent cgroup must own at least one CPU")
        for group in GROUPS:
            if group not in self.min_cpus or group not in self.initial_cpus:
                raise InvalidSpecError(f"missing configuration for group {group!r}")
            if self.min_cpus[group] < 1:
                raise InvalidSpecError(f"minimum cpu count for {group!r} must be >= 1")
            cpus = self.initial_cpus[group]
            if not cpus <= self.parent_cpus:
                raise InvalidSpecError(
                    f"initial cpus of {group!r} exceed the parent cpuset"
                )
            if len(cpus) < self.min_cpus[group]:
                raise InvalidSpecError(
                    f"initial allocation of {group!r} is below its minimum"
                )
        overlap = self.initial_cpus[ONLINE] & self.initial_cpus[BATCH]
        if overlap:
            raise InvalidSpecError(
                f"initial allocations overlap on CPU(s) {sorted(overlap)}"
            )

    def group_index(self, group: str) -> int:
        try:
            return GROUPS.index(group)
        except ValueError as exc:
            raise InvalidSpecError(f"unknown group {group!r}") from exc

    def validate_state(self, state: State) -> None:
        """Reject a state that breaks the standing invariants."""
        for i, group in enumerate(GROUPS):
            cpus = state[i]
            if not cpus <= self.parent_cpus:
                raise CpusetError(
                    f"{group} holds CPU(s) outside the parent cpuset: "
                    f"{sorted(cpus - self.parent_cpus)}"
                )
            if len(cpus) < self.min_cpus[group]:
                raise CpusetError(
                    f"{group} holds {len(cpus)} CPU(s), below its minimum "
                    f"of {self.min_cpus[group]}"
                )
        overlap = state[0] & state[1]
        if overlap:
            raise CpusetError(
                f"the groups share CPU(s): {sorted(overlap)}"
            )

    def validate_targets(self, targets: State) -> None:
        """Validate a user-supplied target pair."""
        for i, group in enumerate(GROUPS):
            cpus = targets[i]
            if not cpus:
                raise InvalidSpecError(
                    f"{group}_cpus must be a non-empty list without duplicates"
                )
            outside = cpus - self.parent_cpus
            if outside:
                raise InvalidSpecError(
                    f"CPU(s) {sorted(outside)} are not available in the parent "
                    f"cpuset {format_cpus(self.parent_cpus)}"
                )
            if len(cpus) < self.min_cpus[group]:
                raise InvalidSpecError(
                    f"{group} requires at least {self.min_cpus[group]} CPU(s), "
                    f"target has {len(cpus)}"
                )
        overlap = targets[0] & targets[1]
        if overlap:
            raise InvalidSpecError(
                f"targets overlap on CPU(s) {sorted(overlap)}; the two groups "
                "must never share a CPU"
            )


# --------------------------------------------------------------------------- #
# planner: shortest legal sequence of single-group cpuset changes (BFS)
# --------------------------------------------------------------------------- #

def _subsets_at_least(
    universe: FrozenSet[int], minimum: int
) -> Iterable[FrozenSet[int]]:
    """All subsets of ``universe`` whose size is at least ``minimum``."""
    items = sorted(universe)
    for size in range(minimum, len(items) + 1):
        for combo in combinations(items, size):
            yield frozenset(combo)


def legal_moves(state: State, config: CpusetConfig) -> Iterable[Move]:
    """Yield every legal single-group move from ``state``."""
    for i, group in enumerate(GROUPS):
        current = state[i]
        other = state[1 - i]
        # A new cpuset has to be disjoint from the other group, live inside
        # the parent and satisfy this group's minimum.
        available = config.parent_cpus - other
        for candidate in _subsets_at_least(available, config.min_cpus[group]):
            if candidate != current:
                yield group, candidate


def plan_moves(
    current: State, target: State, config: CpusetConfig
) -> List[Move]:
    """Return a shortest legal move sequence from ``current`` to ``target``.

    Raises :class:`NoPlanError` when the targets are unreachable, and
    :class:`InvalidSpecError` when the targets themselves violate the rules.
    The caller's state is never modified.
    """
    config.validate_state(current)
    config.validate_targets(target)
    if current == target:
        return []

    queue: List[Tuple[State, List[Move]]] = [(current, [])]
    seen = {current}
    while queue:
        state, path = queue.pop(0)
        for group, candidate in legal_moves(state, config):
            idx = config.group_index(group)
            next_state = (
                (candidate, state[1]) if idx == 0 else (state[0], candidate)
            )
            if next_state in seen:
                continue
            next_path = path + [(group, candidate)]
            if next_state == target:
                return next_path
            seen.add(next_state)
            queue.append((next_state, next_path))
    raise NoPlanError(
        "no legal step sequence reaches the requested cpuset targets; the "
        "existing allocation is left unchanged"
    )


# --------------------------------------------------------------------------- #
# file-system simulation of the cgroup hierarchy
# --------------------------------------------------------------------------- #

def _atomic_write(path: str, payload: bytes) -> None:
    """Write ``payload`` to ``path`` atomically (tmp file + rename + fsync).

    The temporary file carries a unique suffix so concurrent writers in the
    same process (or different processes sharing a pid namespace) can never
    rename each other's staging file out from under them.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    with open(tmp_path, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class CgroupSim:
    """A parent cgroup with two child groups backed by plain files.

    Directory layout under ``root``::

        parent/cpuset.cpus.effective
        parent/online/cpuset.cpus
        parent/batch/cpuset.cpus
    """

    def __init__(
        self,
        root: str,
        config: CpusetConfig,
        fail_on_write: Optional[int] = None,
    ) -> None:
        self.root = root
        self.config = config
        # When set, the Nth cpuset write is interrupted mid-way to simulate a
        # crash during a write.  Production runs leave this at ``None``.
        self.fail_on_write = fail_on_write
        self.write_count = 0
        self.parent_dir = os.path.join(root, PARENT_DIRNAME)
        self._ensure_layout()

    def _path(self, group: Optional[str] = None) -> str:
        if group is None:
            return os.path.join(self.parent_dir, EFFECTIVE_CPUS_FILENAME)
        if group not in GROUPS:
            raise InvalidSpecError(f"unknown group {group!r}")
        return os.path.join(self.parent_dir, group, CPUSET_FILENAME)

    def _ensure_layout(self) -> None:
        parent_file = self._path()
        if not os.path.exists(parent_file):
            _atomic_write(
                parent_file, format_cpus(self.config.parent_cpus).encode()
            )
        for group in GROUPS:
            path = self._path(group)
            if not os.path.exists(path):
                _atomic_write(
                    path,
                    format_cpus(self.config.initial_cpus[group]).encode(),
                )

    def read_parent_cpus(self) -> FrozenSet[int]:
        with open(self._path(), "r", encoding="utf-8") as handle:
            return parse_cpus(handle.read())

    def read_cpus(self, group: str) -> FrozenSet[int]:
        with open(self._path(group), "r", encoding="utf-8") as handle:
            return parse_cpus(handle.read())

    def read_all(self) -> State:
        return self.read_cpus(ONLINE), self.read_cpus(BATCH)

    def write_cpus(self, group: str, cpus: FrozenSet[int]) -> None:
        """Rewrite one group's cpuset; may simulate an interrupted write."""
        path = self._path(group)
        self.write_count += 1
        payload = format_cpus(cpus).encode()
        if self.fail_on_write is not None and self.write_count == self.fail_on_write:
            # Simulate a process killed while the write is in flight: a
            # partially written tmp file remains, the real cpuset file is
            # untouched because the rename never happens.
            tmp_path = f"{path}.tmp.{os.getpid()}.interrupted"
            with open(tmp_path, "wb") as handle:
                handle.write(payload[: max(1, len(payload) // 2)])
                handle.flush()
                os.fsync(handle.fileno())
            raise CgroupWriteFault(
                f"simulated write interruption while updating {group!r} "
                f"to {format_cpus(cpus)} (write #{self.write_count})"
            )
        _atomic_write(path, payload)


# --------------------------------------------------------------------------- #
# operation journal
# --------------------------------------------------------------------------- #

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Operation:
    """One re-pin adjustment and its durable execution record."""

    operation_id: str
    status: str  # 'pending' | 'in_progress' | 'done'
    target: State
    plan: List[Move] = field(default_factory=list)
    records: List[Dict[str, object]] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    def records_steps(self) -> List[Move]:
        """Reconstruct the already-committed moves from the durable record."""
        return [
            (str(record["group"]), frozenset(record["cpus"]))
            for record in self.records
        ]

    def to_json(self) -> Dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "target": {
                ONLINE: sorted(self.target[0]),
                BATCH: sorted(self.target[1]),
            },
            "plan": [
                {"step": i, "group": group, "cpus": sorted(cpus)}
                for i, (group, cpus) in enumerate(self.plan)
            ],
            "records": self.records,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: Dict[str, object]) -> "Operation":
        target_raw = data["target"]
        return cls(
            operation_id=str(data["operation_id"]),
            status=str(data["status"]),
            target=(
                frozenset(target_raw[ONLINE]),
                frozenset(target_raw[BATCH]),
            ),
            plan=[
                (str(step["group"]), frozenset(step["cpus"]))
                for step in data.get("plan", [])
            ],
            records=list(data.get("records", [])),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


class Journal:
    """JSON-file backed operation store, one file per operation id."""

    def __init__(self, state_dir: str) -> None:
        self.ops_dir = os.path.join(state_dir, JOURNAL_DIRNAME, OPS_DIRNAME)
        self.lock_path = os.path.join(state_dir, JOURNAL_DIRNAME, LOCK_FILENAME)
        os.makedirs(self.ops_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        # Create the lock file up front so flock has a stable inode.
        with open(self.lock_path, "a", encoding="utf-8"):
            pass

    def _op_path(self, operation_id: str) -> str:
        return os.path.join(self.ops_dir, f"{operation_id}.json")

    def lock(self) -> "JournalLock":
        return JournalLock(self.lock_path)

    def save(self, operation: Operation) -> None:
        operation.updated_at = _utcnow()
        _atomic_write(
            self._op_path(operation.operation_id),
            json.dumps(operation.to_json(), indent=2, sort_keys=True).encode(),
        )

    def load(self, operation_id: str) -> Operation:
        path = self._op_path(operation_id)
        if not os.path.exists(path):
            raise OperationNotFoundError(
                f"operation {operation_id!r} does not exist"
            )
        with open(path, "r", encoding="utf-8") as handle:
            return Operation.from_json(json.load(handle))

    def list_operations(self) -> List[Operation]:
        if not os.path.isdir(self.ops_dir):
            return []
        entries = [
            name for name in os.listdir(self.ops_dir) if name.endswith(".json")
        ]
        operations = [
            self.load(name[: -len(".json")]) for name in sorted(entries)
        ]
        operations.sort(key=lambda op: op.operation_id)
        return operations

    def next_operation_id(self) -> str:
        existing = self.list_operations()
        next_number = 1
        if existing:
            next_number = int(existing[-1].operation_id) + 1
        return f"{next_number:06d}"

    def unfinished(self) -> Optional[Operation]:
        for operation in self.list_operations():
            if operation.status in ("pending", "in_progress"):
                return operation
        return None


class JournalLock:
    """Context manager wrapping an exclusive advisory flock."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._handle: Optional[int] = None

    def __enter__(self) -> "JournalLock":
        self._handle = os.open(self._path, os.O_RDWR)
        fcntl.flock(self._handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._handle is not None
        fcntl.flock(self._handle, fcntl.LOCK_UN)
        os.close(self._handle)
        self._handle = None


# --------------------------------------------------------------------------- #
# controller: the façade used by the HTTP layer
# --------------------------------------------------------------------------- #

class CpusetController:
    """Coordinates planning, cgroup writes, journaling and recovery."""

    def __init__(
        self,
        state_dir: str,
        config: Optional[CpusetConfig] = None,
        fail_on_write: Optional[int] = None,
    ) -> None:
        self.state_dir = state_dir
        self.config = config or CpusetConfig()
        os.makedirs(state_dir, exist_ok=True)
        self.journal = Journal(state_dir)
        self.sim = CgroupSim(
            os.path.join(state_dir, "cgroup"),
            self.config,
            fail_on_write=fail_on_write,
        )
        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # read-only views
    # ------------------------------------------------------------------ #
    def allocation(self) -> Dict[str, object]:
        online_cpus, batch_cpus = self.sim.read_all()
        unfinished = self.journal.unfinished()
        return {
            "parent_cpus": sorted(self.config.parent_cpus),
            "groups": {
                ONLINE: {
                    "cpus": sorted(online_cpus),
                    "min_cpus": self.config.min_cpus[ONLINE],
                },
                BATCH: {
                    "cpus": sorted(batch_cpus),
                    "min_cpus": self.config.min_cpus[BATCH],
                },
            },
            "in_progress_operation": (
                unfinished.operation_id if unfinished is not None else None
            ),
        }

    def get_operation(self, operation_id: str) -> Operation:
        return self.journal.load(operation_id)

    def list_operations(self) -> List[Operation]:
        return self.journal.list_operations()

    # ------------------------------------------------------------------ #
    # mutations
    # ------------------------------------------------------------------ #
    def rebind(
        self, online_cpus: List[int], batch_cpus: List[int]
    ) -> Operation:
        """Start and execute a new re-pin adjustment."""
        target = self._coerce_target(online_cpus, batch_cpus)
        with self._thread_lock, self.journal.lock():
            blocked = self.journal.unfinished()
            if blocked is not None:
                raise AdjustmentInProgressError(
                    f"operation {blocked.operation_id} is not finished; other "
                    "adjustments are rejected until it completes"
                )
            current = self.sim.read_all()
            # Plan before touching anything: an unreachable target leaves the
            # existing allocation and the journal untouched.
            plan = plan_moves(current, target, self.config)
            operation = Operation(
                operation_id=self.journal.next_operation_id(),
                status="pending",
                target=target,
                plan=plan,
            )
            self.journal.save(operation)
            self._run(operation)
            return self.journal.load(operation.operation_id)

    def resume(self, operation_id: str) -> Operation:
        """Continue an unfinished operation from the actual allocation."""
        with self._thread_lock, self.journal.lock():
            operation = self.journal.load(operation_id)
            if operation.status == "done":
                return operation
            actual = self.sim.read_all()
            # Re-plan against the *actual* on-disk allocation: a previously
            # interrupted write never renamed its tmp file, so the actual
            # state reflects every completed step and nothing more.
            remaining = plan_moves(actual, operation.target, self.config)
            operation.plan = operation.records_steps() + remaining
            operation.status = "in_progress"
            self.journal.save(operation)
            self._run(operation, skip_completed=len(operation.records))
            return self.journal.load(operation_id)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _coerce_target(
        self, online_cpus: List[int], batch_cpus: List[int]
    ) -> State:
        if not isinstance(online_cpus, list) or not isinstance(batch_cpus, list):
            raise InvalidSpecError("online_cpus and batch_cpus must be lists")
        if any(not isinstance(cpu, int) or isinstance(cpu, bool) for cpu in online_cpus):
            raise InvalidSpecError("online_cpus must contain integer cpu ids")
        if any(not isinstance(cpu, int) or isinstance(cpu, bool) for cpu in batch_cpus):
            raise InvalidSpecError("batch_cpus must contain integer cpu ids")
        if len(online_cpus) != len(set(online_cpus)):
            raise InvalidSpecError("online_cpus contains duplicate cpu ids")
        if len(batch_cpus) != len(set(batch_cpus)):
            raise InvalidSpecError("batch_cpus contains duplicate cpu ids")
        target = (frozenset(online_cpus), frozenset(batch_cpus))
        self.config.validate_targets(target)
        return target

    def _run(
        self, operation: Operation, skip_completed: int = 0
    ) -> None:
        """Execute the operation's planned moves one cpuset write at a time."""
        operation.status = "in_progress"
        self.journal.save(operation)
        for step_index, (group, cpus) in enumerate(operation.plan):
            if step_index < skip_completed:
                continue
            # Re-check every invariant immediately before the write so a
            # corrupted on-disk state can never produce an illegal cpuset.
            actual = self.sim.read_all()
            idx = self.config.group_index(group)
            simulated = (
                (cpus, actual[1]) if idx == 0 else (actual[0], cpus)
            )
            self.config.validate_state(simulated)
            self.sim.write_cpus(group, cpus)
            operation.records.append(
                {
                    "step": step_index,
                    "group": group,
                    "cpus": sorted(cpus),
                    "committed_at": _utcnow(),
                }
            )
            self.journal.save(operation)
        final = self.sim.read_all()
        if final != operation.target:
            raise CpusetError(
                "internal error: final allocation does not match the target"
            )
        operation.status = "done"
        self.journal.save(operation)
