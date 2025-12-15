import requests
from typing import Optional, Dict, Any
import os
import time
from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse

class TerminalBenchServerClient:
    def __init__(self, base_url: str = "http://localhost:9901"):
        self.base_url = base_url
        self.terminal_id: Optional[str] = None

    def start(self, task_name: str, rollout_id: Optional[str] = None) -> str:
        response = requests.post(
            f"{self.base_url}/start",
            json={"task_name": task_name, "rollout_id": rollout_id} if rollout_id else {"task_name": task_name}
        )
        response.raise_for_status()
        self.terminal_id = response.json()["id"]
        return self.terminal_id

    def fork(self, terminal_id: Optional[str] = None) -> str:
        """Synchronous fork (blocks until complete)"""
        tid = terminal_id or self.terminal_id
        if not tid:
            raise ValueError("No terminal_id provided")

        response = requests.post(
            f"{self.base_url}/fork",
            json={"id": tid}
        )
        response.raise_for_status()
        new_terminal_id = response.json()["id"]
        return new_terminal_id

    def stop(self, terminal_id: Optional[str] = None) -> Dict[str, Any]:
        tid = terminal_id or self.terminal_id
        if not tid:
            raise ValueError("No terminal_id provided")

        response = requests.post(
            f"{self.base_url}/stop",
            json={"id": tid}
        )
        response.raise_for_status()
        return response.json()


def start_task(task_name: str) -> tuple[str, str]:
    """Start a single task and return (task_name, terminal_id)"""
    client = TerminalBenchServerClient()
    terminal_id = client.start(task_name=task_name)
    print(f"✓ Started {task_name}: {terminal_id}")
    return task_name, terminal_id


def fork_task_terminals(task_name: str, terminal_id: str, num_forks: int) -> Dict[str, Any]:
    """Fork a terminal N times and return fork info"""
    client = TerminalBenchServerClient()
    forked_terminals = []

    try:
        print(f"\nForking {task_name} {num_forks} times...")
        fork_start = time.perf_counter()

        def fork_single(i):
            t0 = time.perf_counter()
            new_tid = client.fork(terminal_id)
            duration = time.perf_counter() - t0
            return i, new_tid, duration

        with ThreadPoolExecutor(max_workers=num_forks) as executor:
            futures = [executor.submit(fork_single, i) for i in range(num_forks)]
            fork_times = []

            for future in as_completed(futures):
                i, new_tid, duration = future.result()
                forked_terminals.append(new_tid)
                fork_times.append(duration)
                print(f"  Fork {i+1}/{num_forks}: {new_tid} - {duration:.2f}s for task: {task_name}")

        total_fork_time = time.perf_counter() - fork_start
        avg_fork_time = sum(fork_times) / len(fork_times) if fork_times else 0

        print(f"Task {task_name} - Total: {total_fork_time:.2f}s | Avg: {avg_fork_time:.2f}s")

        return {
            "task_name": task_name,
            "status": "success",
            "original_terminal": terminal_id,
            "forked_terminals": forked_terminals,
            "num_forks": num_forks,
            "fork_times": fork_times,
            "total_fork_time": total_fork_time,
            "avg_fork_time": avg_fork_time
        }

    except Exception as e:
        print(f"Error forking {task_name}: {e}")
        return {
            "task_name": task_name,
            "status": "error",
            "error": str(e),
            "original_terminal": terminal_id,
            "forked_terminals": forked_terminals
        }


def stop_terminal(terminal_id: str) -> bool:
    """Stop a single terminal"""
    try:
        client = TerminalBenchServerClient()
        client.stop(terminal_id)
        return True
    except Exception as e:
        print(f"Error stopping {terminal_id}: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test fork performance across tasks")
    parser.add_argument(
        "--num-forks",
        type=int,
        default=2,
        help="Number of forks per task (default: 2)"
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=4,
        help="Maximum number of tasks to test (default: 4, 0 for all)"
    )
    args = parser.parse_args()

    host_tasks_dir = Path(__file__).parent / "host_tasks"
    num_forks_per_task = args.num_forks
    max_tasks = args.max_tasks

    task_names = sorted([d for d in os.listdir(host_tasks_dir) if (host_tasks_dir / d).is_dir()])
    if max_tasks > 0:
        task_names = task_names[:max_tasks]

    print(f"Testing {len(task_names)} tasks with {num_forks_per_task} forks each (ALL IN PARALLEL)\n")

    overall_start = time.perf_counter()

    # Phase 1: Start all task containers simultaneously
    print(f"{'='*60}\nPhase 1: Starting all containers simultaneously\n{'='*60}")
    start_time = time.perf_counter()
    task_terminals = {}  # task_name -> terminal_id

    with ThreadPoolExecutor(max_workers=len(task_names)) as executor:
        futures = [executor.submit(start_task, task) for task in task_names]
        for future in as_completed(futures):
            task_name, terminal_id = future.result()
            task_terminals[task_name] = terminal_id

    start_duration = time.perf_counter() - start_time
    print(f"\n✓ All {len(task_terminals)} containers started in {start_duration:.2f}s")
    print(f"Container start rate: {len(task_terminals) / start_duration:.2f} containers/second\n")

    # Wait a bit for containers to be ready
    time.sleep(5)

    # Phase 2: Fork all terminals (per task, but tasks run in parallel)
    print(f"{'='*60}\nPhase 2: Forking all terminals\n{'='*60}")
    fork_results = []
    fork_start_time = time.perf_counter()

    with ThreadPoolExecutor(max_workers=len(task_names)) as executor:
        futures = [
            executor.submit(fork_task_terminals, task, terminal_id, num_forks_per_task)
            for task, terminal_id in task_terminals.items()
        ]
        for future in as_completed(futures):
            fork_results.append(future.result())

    fork_duration = time.perf_counter() - fork_start_time

    # Phase 3: Stop all containers simultaneously
    print(f"\n{'='*60}\nPhase 3: Stopping all containers simultaneously\n{'='*60}")
    all_terminals = []
    for result in fork_results:
        all_terminals.append(result.get("original_terminal"))
        all_terminals.extend(result.get("forked_terminals", []))

    stop_time = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(all_terminals)) as executor:
        futures = [executor.submit(stop_terminal, tid) for tid in all_terminals]
        stop_results = [future.result() for future in as_completed(futures)]

    stop_duration = time.perf_counter() - stop_time
    containers_stopped = sum(1 for r in stop_results if r)

    print(f"\n✓ Stopped {containers_stopped}/{len(all_terminals)} containers in {stop_duration:.2f}s")
    print(f"Container stop rate: {containers_stopped / stop_duration:.2f} containers/second\n")

    overall_duration = time.perf_counter() - overall_start

    # Summary
    successful = [r for r in fork_results if r["status"] == "success"]
    all_fork_times = [t for r in successful for t in r.get("fork_times", [])]

    # Calculate total forks and total fork time across all tasks
    total_forks = sum(r.get("num_forks", 0) for r in successful)
    total_fork_time = sum(r.get("total_fork_time", 0) for r in successful)

    print(f"{'='*60}\nSUMMARY\n{'='*60}")
    print(f"Completed: {len(successful)}/{len(fork_results)} tasks")
    print(f"Total duration: {overall_duration:.2f}s\n")

    print(f"Start Phase:")
    print(f"  Containers started: {len(task_terminals)}")
    print(f"  Time: {start_duration:.2f}s")
    print(f"  Rate: {len(task_terminals) / start_duration:.2f} containers/second\n")

    print(f"Fork Phase:")
    print(f"  Total forks created: {total_forks}")
    print(f"  Time: {fork_duration:.2f}s")
    print(f"  Rate: {total_forks / fork_duration:.2f} containers/second")
    if all_fork_times:
        print(f"  Avg fork time: {sum(all_fork_times)/len(all_fork_times):.2f}s")

    print(f"\nStop Phase:")
    print(f"  Containers stopped: {containers_stopped}")
    print(f"  Time: {stop_duration:.2f}s")
    print(f"  Rate: {containers_stopped / stop_duration:.2f} containers/second")

    # Append summary to JSONL file in benchmarks directory
    benchmarks_dir = Path(__file__).parent / "benchmarks"
    benchmarks_dir.mkdir(exist_ok=True)

    summary_record = {
        "timestamp": time.time(),
        "num_forks_per_task": num_forks_per_task,
        "max_tasks": max_tasks,
        "total_tasks": len(task_names),
        "successful_tasks": len(successful),
        "total_forks": total_forks,
        "total_containers": len(all_terminals),
        "containers_started": len(task_terminals),
        "containers_stopped": containers_stopped,
        "start_duration": fork_duration,
        "start_rate_containers_per_sec": total_forks / fork_duration if fork_duration > 0 else 0,
        "stop_duration": stop_duration,
        "stop_rate_containers_per_sec": containers_stopped / stop_duration if stop_duration > 0 else 0,
        "total_fork_time": total_fork_time,
        "overall_duration": overall_duration
    }

    jsonl_file = benchmarks_dir / "fork_performance.jsonl"
    with open(jsonl_file, "a") as f:
        f.write(json.dumps(summary_record) + "\n")

    print(f"\nAppended summary to {jsonl_file}")
