#!/usr/bin/env python3
"""Four 160-GiB Cache2 NUMA/HugeTLB cases. Default: print the plan only.

Run in the Linux NPU environment containing an installed PR #1609 build.
Only idle 2-MiB HugeTLB pools are changed; original counts are restored on exit.

Use --commands to print the actual root-shell commands for inspection, zeroing
and per-node allocation. Examples for 160GiB / 8 ranks / nodes 0..7:

    cat /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
    cat /sys/devices/system/node/node0/hugepages/hugepages-2048kB/free_hugepages
    echo 0 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_overcommit_hugepages
    echo 0 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
    echo 10240 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages

10240 * 2MiB = 20GiB on this node. The runner computes counts from the node map.
"""

import argparse
import collections
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

GIB = 1 << 30
PAGE = 2 << 20
CAPACITY = 160 * GIB
SYSFS = Path("/sys/devices/system/node")
HUGE = Path("/sys/kernel/mm/hugepages")


def read_int(path):
    return int(path.read_text().strip())


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def progress(message):
    try:
        print(f"[{datetime.datetime.now():%H:%M:%S}] {message}", flush=True)
    except (OSError, ValueError):
        # Console errors must not prevent restoring the host's HugeTLB pools.
        pass


def make_plans(worker_nodes):
    workers = len(worker_nodes)
    nodes = sorted(set(worker_nodes))
    if workers < 2 or workers > 128 or len(nodes) < 2:
        raise ValueError("use 2..128 workers across at least two NUMA nodes")
    if min(nodes) < 0 or CAPACITY % (workers * (1 << 20)):
        raise ValueError("nodes must be nonnegative; workers must divide 160GiB in MiB")
    rank_pages = (CAPACITY // workers + PAGE - 1) // PAGE
    enough = {
        node: count * rank_pages
        for node, count in collections.Counter(worker_nodes).items()
    }
    selected = set(nodes[: max(1, len(nodes) // 2)])
    # Enough global reservations for >= one rank, but no node can fill one rank.
    small = min(
        rank_pages - 1,
        max(4 * GIB // PAGE, (rank_pages + len(nodes) - 1) // len(nodes)),
    )
    return [
        {"name": "01-enough", "pages": enough},
        {"name": "02-no-huge", "pages": {node: 0 for node in nodes}},
        {
            "name": "03-partial-nodes",
            "pages": {node: enough[node] if node in selected else 0 for node in nodes},
        },
        {"name": "04-local-insufficient", "pages": {node: small for node in nodes}},
    ]


class Pools:
    def __init__(self, node_root=SYSFS, huge_root=HUGE):
        self.huge_root = huge_root
        self.files = {
            int(path.parent.parent.parent.name[4:]): path
            for path in node_root.glob(
                "node[0-9]*/hugepages/hugepages-2048kB/nr_hugepages"
            )
        }
        self.overcommit = huge_root / "hugepages-2048kB/nr_overcommit_hugepages"
        if not self.files or not self.overcommit.is_file():
            raise RuntimeError("Linux per-node 2MiB HugeTLB sysfs is required")

    def check_idle(self):
        for node, path in self.files.items():
            if read_int(path) != read_int(path.with_name("free_hugepages")):
                raise RuntimeError(f"node{node}: existing 2MiB huge pages are in use")
        for size in (2048, 1048576):
            root = self.huge_root / f"hugepages-{size}kB"
            for name in ("resv_hugepages", "surplus_hugepages"):
                path = root / name
                if path.exists() and read_int(path):
                    raise RuntimeError(
                        f"nonzero {path}; do not change active HugeTLB pools"
                    )
        # A populated 1GiB pool would hide the 2MiB fallback scenarios.
        root = self.huge_root / "hugepages-1048576kB"
        for name in ("nr_hugepages", "nr_overcommit_hugepages"):
            path = root / name
            if path.exists() and read_int(path):
                raise RuntimeError(
                    f"{path} must be zero for this 2MiB-only matrix; left unchanged"
                )

    def snapshot(self):
        return {
            "pages": {str(node): read_int(path) for node, path in self.files.items()},
            "overcommit": read_int(self.overcommit),
        }

    @staticmethod
    def write_count(path, count):
        started = time.monotonic()
        progress(f"SET {path}: {read_int(path)} -> {count}")
        path.write_text(str(count))
        actual = read_int(path)
        free = path.with_name("free_hugepages")
        detail = f", free={read_int(free)}" if free.exists() else ""
        progress(
            f"DONE {path}: actual={actual}{detail}, "
            f"elapsed={time.monotonic() - started:.2f}s"
        )
        if actual != count:
            raise RuntimeError(
                f"could not arrange scenario: {path}: requested={count}, actual={actual}"
            )

    def set_pages(self, requested):
        requested = {int(node): int(count) for node, count in requested.items()}
        if not set(requested) <= self.files.keys() or any(
            count < 0 for count in requested.values()
        ):
            raise ValueError("invalid node/count in pool configuration")
        self.check_idle()
        for path in self.files.values():
            self.write_count(path, 0)
        for node, count in requested.items():
            if count:
                self.write_count(self.files[node], count)

    def restore(self, saved):
        if set(map(int, saved["pages"])) != set(self.files):
            raise ValueError("backup NUMA nodes do not match this host")
        if int(saved["overcommit"]) < 0:
            raise ValueError("invalid overcommit count in backup")
        self.set_pages(saved["pages"])
        self.write_count(self.overcommit, saved["overcommit"])
        if self.snapshot() != saved:
            raise RuntimeError("restored HugeTLB counts do not match backup")


def benchmark_command(args):
    return [
        sys.executable,
        "-u",
        str(args.repo / "ucm/store/test/e2e/cache_bw.py"),
        "--mode",
        "cache-hit",
        "--cache-core",
        "v2",
        "--model",
        "custom",
        "--device",
        "npu",
        "--workers",
        str(len(args.nodes)),
        "--worker-mode",
        "mla",
        "--tensor-sizes",
        "1048576",
        "--layers",
        "1",
        "--layout",
        "whole-block",
        "--cache-capacity-gb",
        "160",
        "--cache-copy",
        "plain",
        "--cache-streams",
        "4",
        "--blocks",
        "8",
        "--epochs",
        "1",
        "--warmup",
        "0",
        "--epoch-interval-ms",
        "0",
        "--timeout-ms",
        "60000",
        "--process-timeout",
        str(args.timeout),
        "--cache-numa-nodes",
        ",".join(map(str, args.nodes)),
        "--verify",
    ]


def shell_commands(plans):
    lines = [
        "#!/usr/bin/env bash",
        "# Run these commands as root. --commands itself never executes them.",
        "# Manual commands do not restore pools; --run saves and restores them.",
        "# Check 1GiB pools too: they must be zero for this 2MiB-only matrix.",
        "show_hugepages() {",
        "  for size in 2048 1048576; do",
        "    for p in /sys/devices/system/node/node*/hugepages/hugepages-${size}kB/{nr_hugepages,free_hugepages}; do",
        '      [ ! -f "$p" ] || { printf "%s=" "$p"; cat "$p"; }',
        "    done",
        "    for p in /sys/kernel/mm/hugepages/hugepages-${size}kB/{resv_hugepages,surplus_hugepages,nr_overcommit_hugepages}; do",
        '      [ ! -f "$p" ] || { printf "%s=" "$p"; cat "$p"; }',
        "    done",
        "  done",
        "}",
        "zero_2m_pages() {",
        "  echo 0 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_overcommit_hugepages",
        "  for p in /sys/devices/system/node/node*/hugepages/hugepages-2048kB/nr_hugepages; do",
        '    echo 0 > "$p"',
        "  done",
        "}",
        "show_hugepages",
    ]
    for plan in plans:
        lines.extend(["", f"# {plan['name']}", "zero_2m_pages"])
        for node, count in sorted(plan["pages"].items()):
            if count:
                lines.append(
                    f"echo {count} > /sys/devices/system/node/node{node}/hugepages/hugepages-2048kB/nr_hugepages"
                )
        lines.append("show_hugepages")
        lines.append(
            "# Run the benchmark and wait for it to exit before preparing the next case."
        )
    return "\n".join(lines)


def stop_group(proc):
    # This group was created by this invocation; never pkill other benchmarks.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run_benchmark(command, repo, log_path, timeout):
    env = dict(
        os.environ,
        PYTHONFAULTHANDLER="1",
        UCM_LOG_RATE_LIMIT_ENABLE="0",
        UCM_LOG_TO_FILE="0",
    )
    progress(f"START benchmark; log={log_path}")
    progress("Command: " + shlex.join(command))
    with (
        log_path.open("w", encoding="utf-8") as log,
        log_path.open("r", encoding="utf-8", errors="replace") as live,
    ):
        proc = subprocess.Popen(
            command,
            cwd=repo,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.monotonic()
        deadline = started + timeout + 60
        next_heartbeat = started + 30

        def show_output():
            output = live.read()
            if output:
                print(output, end="", flush=True)

        try:
            while True:
                show_output()
                now = time.monotonic()
                if now >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout + 60)
                try:
                    exit_code = proc.wait(timeout=min(0.2, deadline - now))
                    break
                except subprocess.TimeoutExpired:
                    if now >= next_heartbeat:
                        progress(
                            f"benchmark still running: elapsed={now - started:.0f}s; "
                            f"log={log_path}"
                        )
                        next_heartbeat = now + 30
        except subprocess.TimeoutExpired:
            progress(
                f"TIMEOUT benchmark after {timeout + 60}s; stopping its process group"
            )
            stop_group(proc)
            show_output()
            return "timeout"
        except BaseException:
            stop_group(proc)
            show_output()
            raise
        if exit_code != 0:
            stop_group(proc)
        show_output()
        progress(
            f"END benchmark: exit={exit_code}, elapsed={time.monotonic() - started:.2f}s"
        )
        return exit_code


def assess(plan, text, exit_code, worker_nodes):
    backing = {}
    for owner, kind in re.findall(
        r"memfd page backing ready: owner=(\d+).*?actual=(hugetlb-2m|hugetlb-1g|base)\b",
        text,
    ):
        backing[int(owner)] = kind
    issues = []
    if exit_code != 0:
        issues.append(f"benchmark exit={exit_code}")
    if "Load summary" not in text:
        issues.append("missing completed load summary")
    if set(backing) != set(range(len(worker_nodes))):
        issues.append(
            "missing per-rank page-backing logs; check installed native build"
        )
    name = plan["name"]
    for rank, kind in backing.items():
        if rank >= len(worker_nodes):
            continue
        expected = "hugetlb-2m" if name == "01-enough" else "base"
        if name == "03-partial-nodes" and plan["pages"].get(worker_nodes[rank], 0):
            if kind not in ("hugetlb-2m", "base"):
                issues.append(f"rank{rank}: unexpected {kind}")
        elif kind != expected:
            issues.append(f"rank{rank}: expected {expected}, got {kind}")
    failures = text.count("fallocate memfd failed")
    if name == "04-local-insufficient" and not failures:
        issues.append(
            "local fallocate failure not observed; original NUMA failure path was not exercised"
        )
    return {
        "status": "PASS" if not issues else "FAIL",
        "issues": issues,
        "backing": backing,
        "counts": dict(collections.Counter(backing.values())),
        "fallocate_failures": failures,
        "mixed_backing_observed": len(set(backing.values())) > 1,
    }


def execute(args, plans, pools, runner=run_benchmark):
    pools.check_idle()
    if not set(args.nodes) <= pools.files.keys():
        raise ValueError("--numa-nodes contains nodes without a 2MiB HugeTLB pool")
    saved = pools.snapshot()
    args.output.mkdir(parents=True, exist_ok=False)
    save_json(args.output / "hugepage-backup.json", saved)
    progress(f"Results: {args.output}")
    progress(f"Hugepage backup: {args.output / 'hugepage-backup.json'}")
    command = benchmark_command(args)
    report = {
        "capacity_gib": 160,
        "worker_nodes": args.nodes,
        "command": command,
        "cases": [],
    }
    try:
        report["source_commit"] = subprocess.check_output(
            ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        report["source_commit"] = "unknown"
    summary = args.output / "summary.json"
    try:
        pools.write_count(pools.overcommit, 0)
        for plan in plans:
            case = {"name": plan["name"], "requested_pages": plan["pages"]}
            report["cases"].append(case)
            save_json(summary, report)
            progress(f"RUN {plan['name']}: configuring 2MiB HugeTLB pools")
            started = time.monotonic()
            try:
                pools.set_pages(plan["pages"])
                case["actual_pool"] = pools.snapshot()
                progress(
                    f"{plan['name']}: pools ready: {json.dumps(case['actual_pool'])}"
                )
                log = args.output / f"{plan['name']}.log"
                case["exit"] = runner(command, args.repo, log, args.timeout)
                case.update(
                    assess(
                        plan,
                        log.read_text(encoding="utf-8", errors="replace"),
                        case["exit"],
                        args.nodes,
                    )
                )
                pools.check_idle()
            except Exception as error:
                case.update(status="FAIL", error=str(error))
                case["actual_pool"] = pools.snapshot()
                progress(f"{plan['name']}: FAIL: {error}")
            case["elapsed_s"] = round(time.monotonic() - started, 2)
            save_json(summary, report)
            print(json.dumps(case), flush=True)
    finally:
        try:
            progress("Restoring original HugeTLB pools...")
            pools.restore(saved)
            report["restored"] = True
            print("Original HugeTLB pools restored.", flush=True)
        except Exception as error:
            report.update(restored=False, restore_error=str(error))
            print(
                f"RESTORE FAILED: {error}; backup: {args.output / 'hugepage-backup.json'}",
                file=sys.stderr,
            )
        save_json(summary, report)
    return (
        0
        if report.get("restored")
        and all(case.get("status") == "PASS" for case in report["cases"])
        else 1
    )


def interrupted(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="UCM source directory"
    )
    parser.add_argument(
        "--numa-nodes",
        default="0,1,2,3,4,5,6,7",
        help="one NUMA node per NPU/rank; repeats are allowed",
    )
    parser.add_argument(
        "--timeout", type=int, default=900, help="benchmark process timeout in seconds"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "cache2-hugetlb-160g-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run",
        action="store_true",
        help="actually change idle 2MiB pools and run all four NPU cases",
    )
    mode.add_argument(
        "--commands",
        action="store_true",
        help="print root-shell detection/zeroing/allocation commands without executing",
    )
    mode.add_argument(
        "--restore",
        type=Path,
        help="restore a hugepage-backup.json after an uncatchable interruption",
    )
    args = parser.parse_args(argv)
    if args.restore:
        Pools().restore(json.loads(args.restore.read_text(encoding="utf-8")))
        print("Original HugeTLB pools restored.")
        return 0
    try:
        args.nodes = [int(node) for node in args.numa_nodes.split(",")]
        plans = make_plans(args.nodes)
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
    except ValueError as error:
        parser.error(str(error))
    if args.commands:
        print(shell_commands(plans))
        return 0
    args.repo = args.repo.resolve()
    args.output = args.output.resolve()
    print(
        f"160GiB total, {len(args.nodes)} ranks, {160 / len(args.nodes):g}GiB/rank; node map={args.nodes}"
    )
    for plan in plans:
        print(
            plan["name"],
            {
                f"node{node}": f"{count * PAGE / GIB:g}GiB"
                for node, count in plan["pages"].items()
            },
        )
    print("Command:", shlex.join(benchmark_command(args)))
    if not args.run:
        print("Plan only: pass --run in the Linux NPU environment to execute.")
        return 0
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("--run requires Linux root with writable HugeTLB sysfs")
    if not (args.repo / "ucm/store/test/e2e/cache_bw.py").is_file():
        parser.error("--repo must contain ucm/store/test/e2e/cache_bw.py")
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGTERM, interrupted)
    return execute(args, plans, Pools())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted; see summary.json for restoration status.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
