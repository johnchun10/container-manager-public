import requests
from typing import Optional, Dict, Any
import os
import time
from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def test_fork_performance(task_name: str, num_forks: int) -> Dict[str, Any]:
    """
    Start a task and create N forks simultaneously, measuring fork times.
    """
    client = TerminalBenchServerClient()
    terminal_id = None
    forked_terminals = []

    try:
        print(f"\n{'='*60}\nTask: {task_name}\n{'='*60}")

        # Start task
        start_time = time.perf_counter()
        terminal_id = client.start(task_name=task_name)
        start_duration = time.perf_counter() - start_time
        print(f"✓ Started {terminal_id} in {start_duration:.2f}s")
        time.sleep(5)

        # Fork all simultaneously
        print(f"\nForking {num_forks} times simultaneously...")
        fork_start = time.perf_counter()

        def fork_task(i):
            t0 = time.perf_counter()
            new_tid = client.fork(terminal_id)
            duration = time.perf_counter() - t0
            return i, new_tid, duration

        with ThreadPoolExecutor(max_workers=num_forks) as executor:
            futures = [executor.submit(fork_task, i) for i in range(num_forks)]
            fork_times = []

            for future in as_completed(futures):
                i, new_tid, duration = future.result()
                forked_terminals.append(new_tid)
                fork_times.append(duration)
                print(f"  Fork {i+1}/{num_forks}: {new_tid} - {duration:.2f}s for task: {task_name}")

        total_fork_time = time.perf_counter() - fork_start

        # Stats
        avg_fork_time = sum(fork_times) / len(fork_times)
        print(f"Total: {total_fork_time:.2f}s | Avg: {avg_fork_time:.2f}s | "
              f"Min: {min(fork_times):.2f}s | Max: {max(fork_times):.2f}s")

        # Cleanup in parallel
        all_terminals = forked_terminals + [terminal_id]
        st = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(all_terminals)) as executor:
            futures = [executor.submit(client.stop, tid) for tid in all_terminals]
            for future in as_completed(futures):
                try:
                    future.result()
                except:
                    pass
        et = time.perf_counter()
        print(f" Time taken to delete containers of task {task_name} is {et - st}")

        return {
            "task_name": task_name,
            "status": "success",
            "start_duration": start_duration,
            "num_forks": num_forks,
            "fork_times": fork_times,
            "total_fork_time": total_fork_time,
            "avg_fork_time": avg_fork_time
        }

    except Exception as e:
        print(f"Error: {e}")
        all_terminals = forked_terminals + ([terminal_id] if terminal_id else [])
        with ThreadPoolExecutor(max_workers=len(all_terminals)) as executor:
            futures = [executor.submit(client.stop, tid) for tid in all_terminals]
            for future in as_completed(futures):
                try:
                    future.result()
                except:
                    pass
        return {"task_name": task_name, "status": "error", "error": str(e)}


if __name__ == "__main__":
    host_tasks_dir = Path(__file__).parent / "host_tasks"
    num_forks_per_task = 64
    max_tasks = 4

    task_names = sorted([d for d in os.listdir(host_tasks_dir) if (host_tasks_dir / d).is_dir()])
    if max_tasks > 0:
        task_names = task_names[:max_tasks]

    print(f"Testing {len(task_names)} tasks with {num_forks_per_task} forks each (ALL IN PARALLEL)\n")

    overall_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=len(task_names)) as executor:
        futures = [executor.submit(test_fork_performance, task, num_forks_per_task) for task in task_names]
        results = [future.result() for future in as_completed(futures)]

    overall_duration = time.perf_counter() - overall_start

    # Summary
    successful = [r for r in results if r["status"] == "success"]
    all_fork_times = [t for r in successful for t in r.get("fork_times", [])]

    print(f"{'='*60}SUMMARY{'='*60}")
    print(f"Completed: {len(successful)}/{len(results)}")
    print(f"Total duration: {overall_duration:.2f}s")
    if all_fork_times:
        print(f"All forks - Total: {len(all_fork_times)} | Avg: {sum(all_fork_times)/len(all_fork_times):.2f}s")

    with open("fork_performance_results_sync.json", "w") as f:
        json.dump({"num_forks_per_task": num_forks_per_task, "results": results}, f, indent=2)
    print(f"Saved to fork_performance_results_sync.json")
