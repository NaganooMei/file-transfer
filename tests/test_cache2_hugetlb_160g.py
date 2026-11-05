"""CPU-only checks. All sysfs writes target temporary fake files, never the host."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "hugetlb_matrix",
    Path(__file__).resolve().parents[1] / "scripts/cache2_hugetlb_160g.py",
)
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def test_eight_rank_plan_exercises_global_reservation_but_local_shortage():
    plans = matrix.make_plans(list(range(8)))
    assert len(plans) == 4
    assert set(plans[0]["pages"].values()) == {10240}
    assert set(plans[1]["pages"].values()) == {0}
    assert list(plans[2]["pages"].values()) == [10240] * 4 + [0] * 4
    assert set(plans[3]["pages"].values()) == {2048}
    assert sum(plans[3]["pages"].values()) * matrix.PAGE == 32 * matrix.GIB


@pytest.mark.parametrize("nodes", [[0, 0, 1, 1, 2, 2, 3, 3], [0] * 4 + [1] * 4])
def test_multiple_ranks_per_node_have_correct_capacity_and_shortage(nodes):
    enough, _, _, insufficient = matrix.make_plans(nodes)
    assert sum(enough["pages"].values()) * matrix.PAGE == matrix.CAPACITY
    rank_pages = (matrix.CAPACITY // len(nodes)) // matrix.PAGE
    assert all(pages < rank_pages for pages in insufficient["pages"].values())
    assert sum(insufficient["pages"].values()) >= rank_pages


def backing_log(kinds, *, fallocate=False):
    lines = [
        f"memfd page backing ready: owner={rank} device={rank} actual={kind} page_bytes=2097152"
        for rank, kind in enumerate(kinds)
    ]
    if fallocate:
        lines.append("fallocate memfd failed: errno=28")
    return "\n".join(lines + ["Load summary"])


def test_assessment_requires_backing_evidence_and_local_allocation_failure():
    nodes = list(range(8))
    plans = matrix.make_plans(nodes)
    assert (
        matrix.assess(plans[0], backing_log(["hugetlb-2m"] * 8), 0, nodes)["status"]
        == "PASS"
    )
    assert (
        matrix.assess(plans[0], backing_log(["base"] * 8), 0, nodes)["status"] == "FAIL"
    )
    assert matrix.assess(plans[1], "Load summary", 0, nodes)["status"] == "FAIL"
    assert (
        matrix.assess(plans[3], backing_log(["base"] * 8), 0, nodes)["status"] == "FAIL"
    )
    assert (
        matrix.assess(plans[3], backing_log(["base"] * 8, fallocate=True), 0, nodes)[
            "status"
        ]
        == "PASS"
    )
    assert (
        matrix.assess(plans[3], backing_log(["base"] * 8, fallocate=True), 1, nodes)[
            "status"
        ]
        == "FAIL"
    )


def test_partial_case_accepts_safe_global_reservation_contention():
    nodes = list(range(8))
    plan = matrix.make_plans(nodes)[2]
    result = matrix.assess(plan, backing_log(["base"] * 8), 0, nodes)
    assert result["status"] == "PASS"
    assert result["mixed_backing_observed"] is False
    assert (
        matrix.assess(plan, backing_log(["hugetlb-2m"] * 4 + ["base"] * 4), 0, nodes)[
            "status"
        ]
        == "PASS"
    )
    assert (
        matrix.assess(plan, backing_log(["hugetlb-2m"] * 8), 0, nodes)["status"]
        == "FAIL"
    )


@pytest.fixture
def pools(tmp_path, monkeypatch):
    nodes, huge = tmp_path / "nodes", tmp_path / "huge"
    for node in range(2):
        parent = nodes / f"node{node}/hugepages/hugepages-2048kB"
        parent.mkdir(parents=True)
        (parent / "nr_hugepages").write_text("3")
        (parent / "free_hugepages").write_text("3")
    for size in (2048, 1048576):
        parent = huge / f"hugepages-{size}kB"
        parent.mkdir(parents=True)
        for name in (
            "nr_hugepages",
            "nr_overcommit_hugepages",
            "resv_hugepages",
            "surplus_hugepages",
        ):
            (parent / name).write_text("0")
    obj = matrix.Pools(nodes, huge)
    obj.overcommit.write_text("7")
    original_write = obj.write_count

    def write(path, count):
        original_write(path, count)
        if path in obj.files.values():
            path.with_name("free_hugepages").write_text(str(count))

    monkeypatch.setattr(obj, "write_count", write)
    return obj


@pytest.mark.parametrize("active", ["used", "reserved", "1g"])
def test_refuses_active_pools_or_masking_1g_pool_before_mutation(pools, active):
    before = pools.snapshot()
    if active == "used":
        pools.files[0].with_name("free_hugepages").write_text("2")
    elif active == "reserved":
        (pools.huge_root / "hugepages-2048kB/resv_hugepages").write_text("1")
    else:
        (pools.huge_root / "hugepages-1048576kB/nr_hugepages").write_text("1")
    with pytest.raises(RuntimeError):
        pools.set_pages({0: 0, 1: 0})
    assert pools.snapshot() == before


@pytest.mark.parametrize("interrupt", [False, True])
def test_restores_original_pools_and_overcommit_after_failure(
    pools, tmp_path, monkeypatch, interrupt
):
    saved = pools.snapshot()
    args = SimpleNamespace(
        nodes=[0, 1], repo=tmp_path, output=tmp_path / "results", timeout=5
    )
    monkeypatch.setattr(
        matrix.subprocess, "check_output", lambda *a, **kw: "test-commit\n"
    )

    def runner(command, repo, log, timeout):
        if interrupt:
            raise KeyboardInterrupt()
        log.write_text("injected worker failure")
        return 1

    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            matrix.execute(args, matrix.make_plans(args.nodes), pools, runner)
    else:
        assert matrix.execute(args, matrix.make_plans(args.nodes), pools, runner) == 1
    assert pools.snapshot() == saved
    assert json.loads((args.output / "summary.json").read_text())["restored"] is True


def test_commands_include_detection_clear_and_per_node_allocation():
    commands = matrix.shell_commands(matrix.make_plans(list(range(8))))
    assert "free_hugepages" in commands
    assert 'echo 0 > "$p"' in commands
    assert "echo 10240 > /sys/devices/system/node/node0/" in commands
    assert "echo 2048 > /sys/devices/system/node/node7/" in commands
    assert "nr_overcommit_hugepages" in commands


def test_restore_rejects_a_different_host_backup_without_mutation(pools):
    before = pools.snapshot()
    wrong = {"pages": {"99": 3}, "overcommit": 7}
    with pytest.raises(ValueError, match="backup NUMA nodes"):
        pools.restore(wrong)
    assert pools.snapshot() == before


@pytest.mark.parametrize("outcome", [0, 1, "timeout", "interrupt"])
def test_process_cleanup_on_error_timeout_or_interrupt(tmp_path, monkeypatch, outcome):
    stopped = []

    def wait(timeout):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(["benchmark"], timeout)
        if outcome == "interrupt":
            raise KeyboardInterrupt()
        return outcome

    proc = SimpleNamespace(wait=wait)
    monkeypatch.setattr(matrix.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(matrix, "stop_group", lambda process: stopped.append(process))
    if outcome == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            matrix.run_benchmark(["benchmark"], tmp_path, tmp_path / "run.log", 1)
    else:
        assert (
            matrix.run_benchmark(["benchmark"], tmp_path, tmp_path / "run.log", 1)
            == outcome
        )
    assert stopped == ([] if outcome == 0 else [proc])
