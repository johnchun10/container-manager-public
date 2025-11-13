import requests
from typing import Optional, Dict, Any
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import uuid
import json

class TerminalBenchServerClient:
    def __init__(self, base_url: str = "http://localhost:9901"):
        self.base_url = base_url
        self.terminal_id: Optional[str] = None
    
    def start(self, task_name: str, rollout_id: Optional[str] = None) -> str:
        response = requests.post(
            f"{self.base_url}/start",
            json={"task_name": task_name}
        )
        response.raise_for_status()
        self.terminal_id = response.json()["id"]
        return self.terminal_id

    def fork(self, terminal_id: Optional[str] = None, rollout_id: Optional[str] = None) -> str:
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


    def checkpoint(self, terminal_id: Optional[str] = None) -> str:
        tid = terminal_id or self.terminal_id
        if not tid:
            raise ValueError("No terminal_id provided")
        
        response = requests.post(
            f"{self.base_url}/fork_checkpoint",
            json={"id": tid}
        )
        response.raise_for_status()
        checkpoint_id = response.json()["id"]
        return checkpoint_id
    
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
    
    def execute(self, command: str, blocking: bool = True, timeout: int = 30, 
                terminal_id: Optional[str] = None) -> Dict[str, Any]:
        tid = terminal_id or self.terminal_id
        if not tid:
            raise ValueError("No terminal_id provided")
        
        response = requests.post(
            f"{self.base_url}/execute",
            json={
                "id": tid,
                "command": command,
                "blocking": blocking,
                "timeout": timeout
            }
        )
        response.raise_for_status()
        return response.json()
    
    def get_logs(self, terminal_id: Optional[str] = None) -> str:
        tid = terminal_id or self.terminal_id
        if not tid:
            raise ValueError("No terminal_id provided")
        
        response = requests.post(
            f"{self.base_url}/get_pane_logs",
            json={"id": tid}
        )
        response.raise_for_status()
        return response.json()["logs"]
    
    def test(self, terminal_id: Optional[str] = None) -> bool:
        tid = terminal_id or self.terminal_id
        if not tid:
            raise ValueError("No terminal_id provided")
        
        response = requests.post(
            f"{self.base_url}/test",
            json={"id": tid}
        )
        response.raise_for_status()
        return response.json()["passed"]
    
    def solve(self, terminal_id: Optional[str] = None) -> str:
        tid = terminal_id or self.terminal_id
        if not tid:
            raise ValueError("No terminal_id provided")
        
        response = requests.post(
            f"{self.base_url}/solve",
            json={"id": tid}
        )
        response.raise_for_status()
        return response.json()

    def hash(self, terminal_id: Optional[str] = None) -> Dict[str, Any]:
        tid = terminal_id or self.terminal_id

        if not tid:
            raise ValueError("No terminal_id provided")
        
        response = requests.post(
            f"{self.base_url}/hash",
            json={"id": tid}
        )
        response.raise_for_status()
        return response.json()

def run_task(task_name: str, tasks_dir: str) -> Dict[str, Any]:
    client = TerminalBenchServerClient()
    terminal_id = None
    
    try:
        print(f"Starting terminal for task: {task_name}")
        st = time.perf_counter()
        terminal_id = client.start(task_name=task_name)
        et = time.perf_counter()
        starting_duration = et - st
        print(f"Terminal started with ID: {terminal_id} for task: {task_name} in {(et - st):.2f} seconds")
        time.sleep(300)
        # st = time.perf_counter()
        # client.solve(terminal_id)
        # et = time.perf_counter()
        # duration = et - st
        # print(f"Task {task_name} solved in {duration:.2f} seconds")
        # new_rollout_id = str(uuid.uuid4())
        # new_terminal_id = client.fork(terminal_id, new_rollout_id)
        # print(f"Forked terminal with ID: {new_terminal_id}")
        # stop the forked terminal
        # st = time.perf_counter()
        # client.solve(new_terminal_id)
        # et = time.perf_counter()
        
        # duration = et - st
        # print(f"Task {task_name} solved in {duration:.2f} seconds")
        # st = time.perf_counter()
        # hash_val = client.hash(new_terminal_id)
        # et = time.perf_counter()
        # print(f"Task {task_name}'s hash is {hash_val} took {(et - st):.2f} seconds to compute")

        st = time.perf_counter()
        new_terminal_id = client.fork(terminal_id)
        et = time.perf_counter()
        duration = et - st
        print(f"Forked terminal with ID: {new_terminal_id} in {duration:.2f} seconds")

        # st = time.perf_counter()
        # new_terminal_id = client.fork(terminal_id)
        # et = time.perf_counter()
        # duration = et - st
        # print(f"checkpointed terminal with ID: {new_terminal_id} in {duration:.2f} seconds")

        print(f"hash of original terminal: {client.hash(terminal_id)}")
        print(f"hash of forked terminal: {client.hash(new_terminal_id)}")
        # client.solve(new_terminal_id)
        # client.test(new_terminal_id)
        # forked the new terminal

        # another_new_tid = client.fork(terminal_id)

        # print(f"Forked again terminal with ID: {another_new_tid}")
        # client.stop(another_new_tid)
        client.stop(new_terminal_id)
        client.stop(terminal_id)

        # print(f"Stopped forked terminal with ID: {new_terminal_id}")
        

        # st = time.perf_counter()
        # client.solve(terminal_id)
        # et = time.perf_counter()
        
        # duration = et - st
        # print(f"Task {task_name} solved in {duration:.2f} seconds")

        # st = time.perf_counter()
        # hash_val = client.hash(terminal_id)
        # et = time.perf_counter()
        # print(client.test(terminal_id))
        # print(f"Task {task_name}'s hash is {hash_val} took {(et - st):.2f} seconds to compute")
        
        return {
            "task_name": task_name,
            "terminal_id": terminal_id,
            "status": "success",
            "forking_duration": duration,
            "starting_duration": starting_duration

        }
    
    except Exception as e:
        print(f"Error while running task {task_name} (terminal_id: {terminal_id}): {e}")
        return {
            "task_name": task_name,
            "terminal_id": terminal_id,
            "status": "error",
            "error": str(e)
        }
        
    # finally:
    #     if terminal_id:
    #         try:
    #             print(f"Stopping terminal for task: {task_name}")
    #             client.stop()
    #             print(f"Terminal stopped for task: {task_name}")
    #         except Exception as e:
    #             print(f"Error stopping terminal for task {task_name}: {e}")


def test_task(task_name: str, tasks_dir: str) -> Dict[str, Any]:
    client = TerminalBenchServerClient()
    terminal_id = None
    
    try:
        print(f"Starting terminal for task: {task_name}")
        terminal_id = client.start(task_name=task_name, rollout_id="test")
        print(f"Terminal started with ID: {terminal_id} for task: {task_name}")
        
        st = time.perf_counter()
        client.solve()
        et = time.perf_counter()
        print(f"Task {task_name} solved in {(et - st):.2f} seconds")

        st = time.perf_counter()
        client.test()
        et = time.perf_counter()
        
        duration = et - st
        print(f"Task {task_name} tested in {duration:.2f} seconds")

        return {
            "task_name": task_name,
            "terminal_id": terminal_id,
            "status": "success",
            "duration": duration
        }
    
    except Exception as e:
        print(f"Error while running task {task_name} (terminal_id: {terminal_id}): {e}")
        return {
            "task_name": task_name,
            "terminal_id": terminal_id,
            "status": "error",
            "error": str(e)
        }
        
    finally:
        if terminal_id:
            try:
                print(f"Stopping terminal for task: {task_name}")
                client.stop()
                print(f"Terminal stopped for task: {task_name}")
            except Exception as e:
                print(f"Error stopping terminal for task {task_name}: {e}")


if __name__ == "__main__":
    # tasks_dir = '/home/bk478/susRL/tv-cache/data/appworld-train'
    tasks_dir = '../tasks'
    max_workers = 1
    
    task_names = [d for d in os.listdir(tasks_dir) 
                  if os.path.isdir(os.path.join(tasks_dir, d))]
    
    # task_names = task_names[: 1]
    # choose random 1 task
    import random
    task_names = [random.choice(task_names)]
    task_names = ["hello-world"]
    # task_names = ["07b42fd_1"]
    print(f"Found {len(task_names)} tasks: {task_names}\n")
    
    # just run 1 task
    # task_name = task_names[0]
    # task_name = "postgres-csv-clean"
    # run_task(task_name, tasks_dir)

    # task_names = ["postgres-csv-clean"]
    # task_names = ["hello-world"]
    
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_task, task_name, tasks_dir): task_name 
                   for task_name in task_names}
        
        for future in as_completed(futures):
            task_name = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                print(f"Unexpected error for task {task_name}: {e}")

    # store results
    results_path = Path("terminal_bench_results.json")
    with results_path.open("w") as f:
        json.dump(results, f, indent=4)