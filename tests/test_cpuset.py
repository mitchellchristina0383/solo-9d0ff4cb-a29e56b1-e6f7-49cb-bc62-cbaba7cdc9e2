"""Unit tests for the cpuset planner, cgroup simulation and journal recovery.

These tests exercise the core module directly (no HTTP layer) with real files
in temporary directories, including simulated write interruptions and
"restart" by constructing a fresh controller over the same state directory.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from app.cpuset import (
    BATCH,
    CPUSET_FILENAME,
    ONLINE,
    AdjustmentInProgressError,
    CgroupSim,
    CgroupWriteFault,
    CpusetConfig,
    CpusetController,
    InvalidSpecError,
    NoPlanError,
    OperationNotFoundError,
    _atomic_write,
    format_cpus,
    legal_moves,
    parse_cpus,
    plan_moves,
)


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #

def test_parse_and_format_cpus():
    assert parse_cpus("0-2,5,7") == frozenset({0, 1, 2, 5, 7})
    assert format_cpus({5, 0, 2, 1, 7}) == "0-2,5,7"
    assert format_cpus(parse_cpus("3")) == "3"
    assert parse_cpus(" 1 , 2-3 ") == frozenset({1, 2, 3})


@pytest.mark.parametrize("bad", ["", "x", "3-1", "-1", "1,,2", "1-", "-"])
def test_parse_cpus_rejects_garbage(bad):
    with pytest.raises(InvalidSpecError):
        parse_cpus(bad)


def test_format_rejects_empty_set():
    with pytest.raises(InvalidSpecError):
        format_cpus(frozenset())


# --------------------------------------------------------------------------- #
# configuration validation
# --------------------------------------------------------------------------- #

def test_config_rejects_more_than_eight_cpus():
    with pytest.raises(InvalidSpecError):
        CpusetConfig(parent_cpus=frozenset(range(9)))


def test_config_rejects_overlapping_initial_allocation():
    with pytest.raises(InvalidSpecError):
        CpusetConfig(
            parent_cpus=frozenset(range(4)),
            initial_cpus={ONLINE: frozenset({0, 1}), BATCH: frozenset({1, 2})},
        )


def test_config_rejects_initial_below_minimum():
    with pytest.raises(InvalidSpecError):
        CpusetConfig(
            min_cpus={ONLINE: 3, BATCH: 1},
            initial_cpus={ONLINE: frozenset({0, 1}), BATCH: frozenset({3})},
        )


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #

def test_identical_target_needs_no_moves():
    cfg = CpusetConfig()
    state = (frozenset({0, 1, 2}), frozenset({3}))
    assert plan_moves(state, state, cfg) == []


def test_legal_moves_keep_minimum_and_disjointness():
    cfg = CpusetConfig()
    state = (frozenset({0, 1}), frozenset({2}))
    for group, cpus in legal_moves(state, cfg):
        if group == ONLINE:
            assert len(cpus) >= cfg.min_cpus[ONLINE]
            assert not (cpus & state[1])
        else:
            assert len(cpus) >= cfg.min_cpus[BATCH]
            assert not (cpus & state[0])
        assert cpus <= cfg.parent_cpus


def test_every_move_changes_exactly_one_group():
    cfg = CpusetConfig()
    current = (frozenset({0, 1, 2}), frozenset({3}))
    target = (frozenset({4, 5}), frozenset({0, 1}))
    plan = plan_moves(current, target, cfg)
    assert plan  # non-empty
    state = current
    for group, cpus in plan:
        before = state
        idx = 0 if group == ONLINE else 1
        state = (cpus, state[1]) if idx == 0 else (state[0], cpus)
        # exactly one group changes per step
        assert (before[1 - idx] == state[1 - idx])
        cfg.validate_state(state)
    assert state == target


def test_borrow_idle_cpu_to_swap_full_allocation():
    # Parent 0..5; online holds 0,1,2 and batch holds 3; targets fully swap.
    cfg = CpusetConfig()
    current = (frozenset({0, 1, 2}), frozenset({3}))
    target = (frozenset({3, 4}), frozenset({0, 1, 2}))
    plan = plan_moves(current, target, cfg)
    state = current
    borrowed = False
    for group, cpus in plan:
        idx = 0 if group == ONLINE else 1
        state = (cpus, state[1]) if idx == 0 else (state[0], cpus)
        cfg.validate_state(state)
        if cpus & (cfg.parent_cpus - current[0] - current[1]):
            borrowed = True
    assert state == target
    assert borrowed, "the swap must pass through an idle borrowed CPU"


def test_no_plan_when_parent_is_full_and_neither_group_can_shrink():
    cfg = CpusetConfig(
        parent_cpus=frozenset({0, 1, 2, 3}),
        min_cpus={ONLINE: 2, BATCH: 2},
        initial_cpus={ONLINE: frozenset({0, 1}), BATCH: frozenset({2, 3})},
    )
    with pytest.raises(NoPlanError):
        plan_moves(
            (frozenset({0, 1}), frozenset({2, 3})),
            (frozenset({2, 3}), frozenset({0, 1})),
            cfg,
        )


def test_target_outside_parent_rejected():
    cfg = CpusetConfig(parent_cpus=frozenset(range(4)))
    with pytest.raises(InvalidSpecError):
        plan_moves(
            (frozenset({0, 1}), frozenset({2})),
            (frozenset({9}), frozenset({2})),
            cfg,
        )


def test_overlapping_targets_rejected():
    cfg = CpusetConfig()
    with pytest.raises(InvalidSpecError):
        plan_moves(
            (frozenset({0, 1}), frozenset({2})),
            (frozenset({4, 5}), frozenset({5})),
            cfg,
        )


# --------------------------------------------------------------------------- #
# cgroup file simulation
# --------------------------------------------------------------------------- #

def test_cgroup_sim_creates_and_reads_layout(tmp_path):
    cfg = CpusetConfig()
    sim = CgroupSim(str(tmp_path), cfg)
    assert sim.read_parent_cpus() == cfg.parent_cpus
    assert sim.read_cpus(ONLINE) == frozenset({0, 1, 2})
    assert sim.read_cpus(BATCH) == frozenset({3})
    assert os.path.exists(
        os.path.join(str(tmp_path), "parent", ONLINE, CPUSET_FILENAME)
    )


def test_cgroup_sim_write_is_atomic_and_durable(tmp_path):
    cfg = CpusetConfig()
    sim = CgroupSim(str(tmp_path), cfg)
    sim.write_cpus(ONLINE, frozenset({4, 5}))
    # No stale tmp files after a clean write.
    for root, _dirs, files in os.walk(str(tmp_path)):
        assert not any(name.endswith(".tmp") or ".tmp." in name for name in files)
    assert sim.read_cpus(ONLINE) == frozenset({4, 5})


def test_interrupted_write_keeps_previous_cpuset(tmp_path):
    cfg = CpusetConfig()
    sim = CgroupSim(str(tmp_path), cfg, fail_on_write=1)
    with pytest.raises(CgroupWriteFault):
        sim.write_cpus(ONLINE, frozenset({4, 5}))
    # The real cpuset file must still contain the previous allocation.
    assert sim.read_cpus(ONLINE) == frozenset({0, 1, 2})
    # Evidence of the interrupted write remains on disk.
    leftovers = [
        name
        for _, _, files in os.walk(str(tmp_path))
        for name in files
        if name.endswith(".interrupted")
    ]
    assert leftovers


def test_concurrent_atomic_writes_never_clobber_each_other(tmp_path):
    # Regression: same-process concurrent writers used to share one tmp name
    # (pid only), so a rename could delete another writer's staging file.
    target = os.path.join(str(tmp_path), "cpuset.cpus")
    errors = []

    def writer(value: int) -> None:
        try:
            for _ in range(50):
                _atomic_write(target, str(value).encode())
        except OSError as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(i,)) for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert os.path.exists(target)
    assert int(open(target, encoding="utf-8").read()) in range(8)
    # No staging files are left behind.
    assert not [
        name
        for _, _, files in os.walk(str(tmp_path))
        for name in files
        if ".tmp." in name and not name.endswith(".interrupted")
    ]


# --------------------------------------------------------------------------- #
# controller: happy paths
# --------------------------------------------------------------------------- #

def make_controller(tmp_path, **kwargs):
    return CpusetController(str(tmp_path), CpusetConfig(), **kwargs)


def test_rebind_executes_plan_and_records_every_step(tmp_path):
    controller = make_controller(tmp_path)
    operation = controller.rebind([4, 5], [0, 1])
    assert operation.status == "done"
    assert len(operation.records) == len(operation.plan)
    online_cpus, batch_cpus = controller.sim.read_all()
    assert online_cpus == frozenset({4, 5})
    assert batch_cpus == frozenset({0, 1})
    # The journal file is valid JSON on disk.
    path = os.path.join(
        str(tmp_path), "journal", "ops", f"{operation.operation_id}.json"
    )
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["status"] == "done"
    assert payload["target"][ONLINE] == [4, 5]


def test_rebind_swap_through_borrowed_cpus(tmp_path):
    controller = make_controller(tmp_path)
    operation = controller.rebind([3, 4], [0, 1, 2])
    state = (frozenset({0, 1, 2}), frozenset({3}))
    for group, cpus in operation.plan:
        idx = 0 if group == ONLINE else 1
        state = (cpus, state[1]) if idx == 0 else (state[0], cpus)
        controller.config.validate_state(state)
    assert state == (frozenset({3, 4}), frozenset({0, 1, 2}))
    assert controller.sim.read_all() == state


def test_noop_rebind_completes_with_empty_plan(tmp_path):
    controller = make_controller(tmp_path)
    operation = controller.rebind([0, 1, 2], [3])
    assert operation.status == "done"
    assert operation.plan == []
    assert operation.records == []


def test_unreachable_target_preserves_allocation_and_journal(tmp_path):
    cfg = CpusetConfig(
        parent_cpus=frozenset({0, 1, 2, 3}),
        min_cpus={ONLINE: 2, BATCH: 2},
        initial_cpus={ONLINE: frozenset({0, 1}), BATCH: frozenset({2, 3})},
    )
    controller = CpusetController(str(tmp_path), cfg)
    with pytest.raises(NoPlanError):
        controller.rebind([2, 3], [0, 1])
    assert controller.sim.read_all() == (
        frozenset({0, 1}),
        frozenset({2, 3}),
    )
    assert controller.list_operations() == []


def test_invalid_targets_are_rejected(tmp_path):
    controller = make_controller(tmp_path)
    with pytest.raises(InvalidSpecError):
        controller.rebind([9], [3])  # outside parent
    with pytest.raises(InvalidSpecError):
        controller.rebind([0], [3])  # below online minimum
    with pytest.raises(InvalidSpecError):
        controller.rebind([4, 5], [5])  # overlap
    with pytest.raises(InvalidSpecError):
        controller.rebind([4, 4], [3])  # duplicates
    assert controller.list_operations() == []


def test_second_adjustment_blocked_while_unfinished(tmp_path):
    controller = make_controller(tmp_path, fail_on_write=1)
    with pytest.raises(CgroupWriteFault):
        controller.rebind([4, 5], [0, 1])
    with pytest.raises(AdjustmentInProgressError):
        controller.rebind([6, 7], [2])


def test_operation_ids_are_monotonic(tmp_path):
    controller = make_controller(tmp_path)
    first = controller.rebind([4, 5], [0, 1])
    second = controller.rebind([6, 7], [2])
    assert second.operation_id > first.operation_id
    assert [op.operation_id for op in controller.list_operations()] == [
        first.operation_id,
        second.operation_id,
    ]


# --------------------------------------------------------------------------- #
# write interruption + restart recovery
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fail_on_write", [1, 2, 3])
def test_restart_resumes_from_actual_allocation(tmp_path, fail_on_write):
    # This swap needs three writes (online borrows idle CPU 4 first), so a
    # fault can be injected at the first, middle or last write.
    faulting = make_controller(tmp_path, fail_on_write=fail_on_write)
    with pytest.raises(CgroupWriteFault):
        faulting.rebind([3, 4], [0, 1, 2])
    unfinished = faulting.journal.unfinished()
    assert unfinished is not None
    op_id = unfinished.operation_id
    actual_after_fault = faulting.sim.read_all()
    # The faulted step never landed: fewer records than planned steps and the
    # on-disk allocation matches the records exactly.
    assert len(unfinished.records) == fail_on_write - 1

    # Simulate a process restart: a brand-new controller over the same files.
    restarted = make_controller(tmp_path)
    assert restarted.journal.unfinished().operation_id == op_id
    operation = restarted.resume(op_id)

    assert operation.status == "done"
    assert restarted.sim.read_all() == (frozenset({3, 4}), frozenset({0, 1, 2}))
    # Committed records are a strict prefix continuation of the old journal.
    prior_steps = len(unfinished.records)
    assert len(operation.records) >= prior_steps
    for record in unfinished.records:
        assert record in operation.records
    # The actual allocation at crash time was never rolled back or replayed.
    assert actual_after_fault[0].issubset({0, 1, 2, 3, 4, 5})
    assert actual_after_fault[1].issubset({0, 1, 2, 3})


def test_resume_unknown_operation_raises(tmp_path):
    controller = make_controller(tmp_path)
    with pytest.raises(OperationNotFoundError):
        controller.resume("999999")


def test_resume_is_idempotent_when_already_done(tmp_path):
    controller = make_controller(tmp_path)
    operation = controller.rebind([4, 5], [0, 1])
    again = controller.resume(operation.operation_id)
    assert again.status == "done"
    assert again.records == operation.records


def test_fault_on_last_write_still_completes_after_resume(tmp_path):
    # The three-step swap fails on its final write.
    faulting = make_controller(tmp_path, fail_on_write=3)
    with pytest.raises(CgroupWriteFault):
        faulting.rebind([3, 4], [0, 1, 2])
    op_id = faulting.journal.unfinished().operation_id
    assert len(faulting.journal.unfinished().records) == 2
    restarted = make_controller(tmp_path)
    operation = restarted.resume(op_id)
    assert operation.status == "done"
    assert restarted.sim.read_all() == (frozenset({3, 4}), frozenset({0, 1, 2}))


def test_allocation_view_reports_in_progress_operation(tmp_path):
    faulting = make_controller(tmp_path, fail_on_write=1)
    with pytest.raises(CgroupWriteFault):
        faulting.rebind([4, 5], [0, 1])
    view = faulting.allocation()
    assert view["in_progress_operation"] == "000001"
    assert set(view["groups"]) == {ONLINE, BATCH}
    assert view["parent_cpus"] == list(range(8))


def test_parallel_controller_bootstrap_on_empty_dir(tmp_path):
    # Regression for the first-request race: many threads constructing a
    # controller over the same empty directory must produce one consistent
    # cgroup layout and no errors.
    results = []
    errors = []

    def build() -> None:
        try:
            results.append(CpusetController(str(tmp_path), CpusetConfig()))
        except OSError as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 12
    for controller in results:
        assert controller.sim.read_all() == (
            frozenset({0, 1, 2}),
            frozenset({3}),
        )
