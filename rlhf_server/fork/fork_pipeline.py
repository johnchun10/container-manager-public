from terminal_bench.terminal.tmux_session import TmuxSession
from terminal_bench.terminal.docker_compose_manager import DockerComposeManager
from terminal_bench.parsers.parser_factory import ParserFactory, ParserName
from terminal_bench.parsers.base_parser import UnitTestStatus
from terminal_bench.terminal.models import TerminalCommand
from terminal_bench.terminal import Terminal
from terminal_bench.handlers.trial_handler import TrialPaths, TaskPaths, Task
from typing import Dict, Deque, List
from threading import Lock, Thread, Semaphore
import re
import os
import uuid
import subprocess
from pathlib import Path
import yaml
from collections import deque
import time
from rlhf_server.fork.fork_utils import get_all_container_by_id, get_service_name_from_container_name, delete_committed_images
from rlhf_server.networks.network_utils import does_container_need_bridge_network
from rlhf_server.networks.bridge_networks import BridgeNetworks

WAIT_TIME_OUT = 0.25
MAX_PARALLELISM = 20
MAX_RETRIES = 3

class ContainerStartArgs:
    def __init__(
        self,
        client_image_name,
        rollout_id,
        new_rollout_id,
        new_task_dir,
        task_name,
        image_names,
        old_pane_log,
        new_terminal_id
    ):
        self.client_image_name = client_image_name
        self.rollout_id = rollout_id
        self.new_rollout_id = new_rollout_id
        self.new_task_dir = new_task_dir
        self.task_name = task_name
        self.image_names = image_names
        self.old_pane_log = old_pane_log
        self.new_terminal_id = new_terminal_id


class ForkRequest:
    def __init__(self, terminal_id, container_start_args=None):
        self.terminal_id = terminal_id
        self.container_start_args: ContainerStartArgs = container_start_args
        self.commit_complete = False
        self.fork_complete = False
        self.priority = False
        self.id = str(uuid.uuid4())
        self.error = None
    
    @property
    def new_terminal_id(self):
        return f'term_{self.id}'

    @property
    def new_rollout_id(self):
        return self.id


class ForkPipeline:

    def __init__(self, 
                 terminals: Dict[str, Terminal], 
                 state_lock: Lock, 
                 sessions: Dict[str, TmuxSession], 
                 last_executed_command: Dict[str, str],
                 panelog_forked_terminal_dict: Dict[str, str],
                 task_paths: Dict[str, TaskPaths],
                 rollout_env_id: Dict[str, str],
                 task_dir_env_id: Dict[str, str],
                 trial_path: Dict[str, TrialPaths],
                 terminal_to_network: Dict[str, str],
                 bridge_networks: BridgeNetworks):
        
        self.terminals = terminals
        self.state_lock = state_lock
        self.sessions = sessions
        self.panelog_forked_terminal_dict = panelog_forked_terminal_dict
        self.last_executed_command = last_executed_command
        self.enable_caching = os.getenv("ENABLE_CACHING", "0") == "1"
        self.container_resurrect_path = os.getenv("CONTAINER_RESURRECT_PATH", "/root/tmux_resurrect")
        
        self.path = os.getenv("TASKS_ROOT", "../tasks") 
        self.task_paths = task_paths
        self.rollout_env_id = rollout_env_id
        self.task_dir_env_id = task_dir_env_id
        self.trial_path = trial_path
        self.terminal_to_network = terminal_to_network
        self.bridge_networks = bridge_networks

        self.commit_queue: Deque[ForkRequest] = deque()
        self.commit_q_lock: Lock = Lock()

        self.startup_queue: Deque[ForkRequest] = deque()
        self.startup_q_lock: Lock = Lock()

        self.delete_queue: Deque[str] = deque()
        self.delete_q_lock: Lock = Lock()

        self.ongoing_forks: Dict[str, ForkRequest] = {}

        # Semaphores to enforce parallelism limit per operation
        self.commit_semaphore = Semaphore(MAX_PARALLELISM)
        self.startup_semaphore = Semaphore(MAX_PARALLELISM)
        self.delete_semaphore = Semaphore(MAX_PARALLELISM)

        self.commit_thread = Thread(target=self.commit_worker, daemon=True)
        self.commit_thread.start()

        self.fork_thread = Thread(target=self.fork_worker, daemon=True)
        self.fork_thread.start()

        self.delete_thread = Thread(target=self.delete_worker, daemon=True)
        self.delete_thread.start()


    def commit_worker(self):
        """Continuously process commit queue with semaphore-based parallelism control"""
        while True:
            fork_req = None
            with self.commit_q_lock:
                if len(self.commit_queue) > 0:
                    fork_req = self.commit_queue.popleft()

            if fork_req:
                self.commit_semaphore.acquire()
                thread = Thread(target=self._commit_with_release, args=(fork_req, ))
                thread.start()
            else:
                # No requests, sleep briefly
                time.sleep(WAIT_TIME_OUT)


    def _commit_with_release(self, fork_req: ForkRequest):
        try:
            st = time.perf_counter()
            self.commit_container(fork_req)
            et = time.perf_counter()
            print(f'Time taken to commit container: {et - st} seconds. The container is {fork_req.id}')
        except Exception as e:
            print(f"Error in commit_container for terminal {fork_req.terminal_id}: {e}")
        finally:
            self.commit_semaphore.release()
    

    def fork_worker(self):
        """Continuously process startup queue using semaphore-based parallelism control such that there are always MAX_PARALLELISM creations"""

        while True:
            fork_req = None
            with self.startup_q_lock:
                if len(self.startup_queue) > 0:
                    fork_req = self.startup_queue.popleft()

            if fork_req:
                self.startup_semaphore.acquire()
                thread = Thread(target=self._startup_with_release, args=(fork_req, ))
                thread.start()
            else:
                time.sleep(WAIT_TIME_OUT)


    def _startup_with_release(self, fork_req: ForkRequest):

        try:
            st = time.perf_counter()
            self.start_container(fork_req)
            et = time.perf_counter()

            print(f'Time taken to start container: {et - st} seconds. The container is {fork_req.terminal_id}')
        except Exception as e:
            print(f"Error in start_container for terminal {fork_req.new_terminal_id}: {e}")
        finally:
            self.startup_semaphore.release()
    

    def delete_worker(self):

        while True:
            del_req: str = None

            with self.delete_q_lock:
                if len(self.delete_queue) > 0:
                    del_req = self.delete_queue.pop()
            
            if del_req != None:
                self.delete_semaphore.acquire()
                thread = Thread(target=self._delete_with_release, args=(del_req, ))
                thread.start()
            
            else:
                time.sleep(WAIT_TIME_OUT)
    

    def _delete_with_release(self, del_req: str):

        try:
            delete_committed_images([del_req])
        except Exception as e:
            print(f"Error in delete for terminal {del_req}: {e}")
        finally:
            self.delete_semaphore.release()


    def get_pane_logs_local(self, terminal_id: str) -> str:

        try:
            with self.state_lock:
                if terminal_id not in self.sessions:
                    return "Session not found"
                if terminal_id not in self.panelog_forked_terminal_dict:
                    print(f"No forked pane log found for terminal {terminal_id}")
                    old_pane_log = ""
                else:
                    print(f"Using forked pane log for terminal {terminal_id}")
                    old_pane_log = self.panelog_forked_terminal_dict[terminal_id]
                session = self.sessions[terminal_id]
                last_executed_cmd = self.last_executed_command.get(terminal_id, "")
            pane_output = session.capture_pane(capture_entire=True)

            pane_output = [
                line for line in pane_output.splitlines()
                if "scripts/save.sh" not in line 
                and "scripts/restore.sh" not in line 
                and "scripts/ps.sh" not in line
                and "scripts/startup.sh" not in line
                and "scripts/exit.sh" not in line
                and "ps: command not found" not in line
            ]

            pane_output = [line for line in pane_output if line.strip() != '']
            pane_output = [re.sub(r'^root@[^:]+:', 'root@terminal:', line) for line in pane_output]
            pane_output = "\n".join(pane_output)

            if old_pane_log and old_pane_log != "":
                pane_output = old_pane_log + "\n" + pane_output
            return pane_output
        
        except Exception as e:
            print(f"Error getting pane logs: {e} for terminal {terminal_id}")
            return ""


    def call_docker_commit(self, container_names: list[str], old_rollout_id: str, new_rollout_id: str):
        commit_results = []
        image_names = []
        for container_name in container_names:
            new_container_name = container_name.replace(old_rollout_id, new_rollout_id)

            # Retry logic for docker commit
            commit_result = None
            for attempt in range(MAX_RETRIES):
                commit_result = subprocess.run(
                    ['docker', 'commit',  '--no-pause', container_name, f'{new_container_name}'],
                    capture_output=True, text=True
                )

                if commit_result.returncode == 0:
                    break

                if attempt < MAX_RETRIES - 1:
                    print(f"Docker commit failed for {container_name} (attempt {attempt + 1}/{MAX_RETRIES}). Retrying...")
                    time.sleep(1)  # Brief delay before retry

            if commit_result.returncode != 0:
                print(f"Docker commit failed for {container_name} after {MAX_RETRIES} attempts")
                return None, commit_result

            image_names.append(f'{new_container_name}')
            commit_results.append(commit_result)
        return image_names, None
    

    def create_forked_task_dir(self, container_names, task_name, terminal_id):
        with self.state_lock:
            if terminal_id not in self.task_dir_env_id:
                print(f"No task directory found for terminal id {terminal_id}, must be first fork")
                original_task_dir = Path(f"{self.path}/{task_name}")
            else:
                print(f"Using existing task directory for terminal id {terminal_id}")
                original_task_dir = Path(self.task_dir_env_id[terminal_id])

        random_string = str(uuid.uuid4())
        new_task_dir = Path(f"{self.path}/{task_name}_{random_string}")

        print(f'NEW task dir is: {new_task_dir}')

        if new_task_dir.exists():
            subprocess.run(['rm', '-r', str(new_task_dir)])
        subprocess.run(['cp', '-r', str(original_task_dir), str(new_task_dir)])
        docker_compose_path = new_task_dir / 'docker-compose.yaml'

        with open(docker_compose_path, 'r') as f:
            docker_compose = yaml.safe_load(f)
        service_names_dict = get_service_name_from_container_name(container_names)

        for service_name, values in docker_compose['services'].items():
            if service_name in service_names_dict.keys():
                image_name = service_names_dict[service_name]
                values['image'] = image_name
            else:
                values["image"] = service_names_dict['main']

            if 'build' in values:
                del values['build']
        
        with open(docker_compose_path, 'w') as f:
            yaml.dump(docker_compose, f)

        return new_task_dir


    def commit_container(self, fork_request: ForkRequest):
        new_rollout_id = fork_request.new_rollout_id
        new_terminal_id = fork_request.new_terminal_id

        terminal_id = fork_request.terminal_id

        print(f'Committing {fork_request.id} after getting it from queue')

        with self.state_lock:
            if terminal_id not in self.terminals:
                raise ValueError(f"Terminal {terminal_id} not found")
            
            client_image_name = self.terminals[terminal_id]._compose_manager._client_image_name
            task_name = self.task_paths[terminal_id].input_path.parts[-1]

            if terminal_id not in self.rollout_env_id:
                print("Rollout ID not found for terminal:", terminal_id)
                raise ValueError(f"Rollout ID not found for terminal: {terminal_id}")
            
            rollout_id = self.rollout_env_id[terminal_id]
            if terminal_id not in self.sessions:
                raise ValueError(f"Session not found: {terminal_id}")
            
            session = self.sessions[terminal_id]
        
        print(f'Committing {fork_request.id} 2 after getting it from queue')

        if self.enable_caching:
            old_pane_log = self.get_pane_logs_local(terminal_id)
            session.send_keys([f"{self.container_resurrect_path}/scripts/exit.sh", 'Enter'], min_timeout_sec=0.)
        else:
            old_pane_log = ""

        print(f'Committing {fork_request.id} 3 after getting it from queue')

        container_names = get_all_container_by_id(rollout_id)
        print(f'Committing {fork_request.id} 3.1 after getting it from queue')
        image_names, commit_error = self.call_docker_commit(container_names, rollout_id, new_rollout_id)
        if commit_error != None:
            print(f'Committing {fork_request.id} 3.1.2 after getting it from queue, commit error: {commit_error.stdout} and {commit_error.stderr}')
        print(f'Committing {fork_request.id} 3.2 after getting it from queue')

        if commit_error:
            return f"Failed to commit containers: {commit_error.stderr}"
        
        print(f'Committing {fork_request.id} 4 after getting it from queue')
        new_task_dir = self.create_forked_task_dir(image_names, task_name, terminal_id)
        
        container_start_args = ContainerStartArgs(client_image_name, rollout_id, new_rollout_id, new_task_dir, task_name, image_names, old_pane_log, new_terminal_id)
        fork_request.container_start_args = container_start_args

        with self.startup_q_lock:
            print(f'Added {fork_request.id} to startup queue')
            if fork_request.priority:
                self.startup_queue.appendleft(fork_request)
            else:
                self.startup_queue.append(fork_request)

        fork_request.commit_complete = True
    

    def start_container(self, fork_request: ForkRequest):
        args = fork_request.container_start_args
        client_image_name = args.client_image_name
        rollout_id = args.rollout_id
        new_rollout_id = args.new_rollout_id
        new_task_dir = args.new_task_dir
        task_name = args.task_name
        image_names = args.image_names
        old_pane_log = args.old_pane_log
        new_terminal_id = fork_request.new_terminal_id

        # Retry logic for starting container
        for attempt in range(MAX_RETRIES):
            try:
                print(f'Starting {fork_request.id} after getting it from startup queue (attempt {attempt + 1}/{MAX_RETRIES})')
                client_image_name = client_image_name.replace(rollout_id, new_rollout_id)

                docker_compose_path = new_task_dir / 'docker-compose.yaml'
                new_client_image_name = f'{client_image_name}'
                new_client_container_name = f'{new_client_image_name}_container'
                docker_image_name_prefix = f'{new_client_image_name}_docker'

                trial_paths = TrialPaths(Path(f'../runs/{client_image_name}'), task_name, 'trial_{}'.format(new_rollout_id))
                trial_paths.mkdir()

                network_required = does_container_need_bridge_network(docker_compose_path)
                network_name = None
                print(f'Starting {fork_request.id} 1.1')

                if network_required:
                    network_name = self.bridge_networks.get_network()
                    print(f'Needed bridge network while forking: {fork_request.id}, so got it from {network_name}')
                    print(f'Starting {fork_request.id} 1.2')
                    with self.state_lock:
                        self.terminal_to_network[new_terminal_id] = network_name

                new_terminal = Terminal(
                    client_container_name=new_client_container_name,
                    client_image_name=new_client_image_name,
                    docker_image_name_prefix=docker_image_name_prefix,
                    docker_compose_path=docker_compose_path,
                    sessions_logs_path=trial_paths.sessions_path,
                    agent_logs_path=trial_paths.agent_logging_dir,
                    commands_path=trial_paths.commands_path,
                    no_rebuild=False,
                    cleanup=True,
                    livestream=False,
                    disable_recording=False,
                    bridge_network_name=network_name
                )
                new_terminal.start()
                print(f'Starting {fork_request.id} 1.3')

                # Delete committed images after starting
                # TODO: Move this somewhere else
                with self.delete_q_lock:
                    self.delete_queue.extend(image_names)

                print(f'Starting {fork_request.id} 1.4')
                new_session = new_terminal.create_session(session_name="agent", is_active_stream=False, as_configured_user=True)

                print(f'Starting {fork_request.id} 1.5')

                with self.state_lock:
                    self.task_dir_env_id[new_terminal_id] = str(new_task_dir)
                    self.rollout_env_id[new_terminal_id] = new_rollout_id
                    self.terminals[new_terminal_id] = new_terminal
                    self.sessions[new_terminal_id] = new_session
                    self.trial_path[new_terminal_id] = trial_paths
                    self.task_paths[new_terminal_id] = TaskPaths(input_path=Path(f"{new_task_dir}"))
                    self.panelog_forked_terminal_dict[new_terminal_id] = old_pane_log

                if self.enable_caching:
                    new_session.send_keys([f"source {self.container_resurrect_path}/scripts/startup.sh", 'Enter'], min_timeout_sec=0.1)

                print(f"Successfully started forked container with terminal ID: {fork_request.id}")
                fork_request.fork_complete = True
                break  # Success, exit retry loop

            except Exception as e:
                print(f"Error starting forked container {fork_request.id} in background (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    print(f"Retrying in 2 seconds...")
                    time.sleep(2)  # Delay before retry
                else:
                    print(f"Failed to start container after {MAX_RETRIES} attempts")
                    fork_request.error = str(e)
    

    def fork_priority(self, terminal_id) -> str:
        fork_req = ForkRequest(terminal_id=terminal_id)
        fork_req.priority = True

        with self.commit_q_lock:
            print(f'Added for request for {terminal_id} and the q length is {len(self.commit_queue)}')
            self.commit_queue.appendleft(fork_req)
        
        prev_logged = 0
        while not fork_req.fork_complete:
            
            if time.perf_counter() - prev_logged > 2:
                print(f'Waiting for {fork_req.id} fork to complete')
                prev_logged = time.perf_counter()
                
            time.sleep(WAIT_TIME_OUT)
        
        assert fork_req.new_terminal_id != None
        return fork_req.new_terminal_id
    

    def fork_async(self, terminal_id) -> str:

        fork_req = ForkRequest(terminal_id=terminal_id)
        
        with self.commit_q_lock:
            self.commit_queue.append(fork_req)
        
        
        logged_time = 0
        while True:
            if fork_req.commit_complete:
                break
            time.sleep(WAIT_TIME_OUT)
            if time.perf_counter() - logged_time > 1:
                print(f'Waiting for commit to complete for terminal {terminal_id}')
                logged_time = time.perf_counter()
        
        with self.state_lock:
            self.ongoing_forks[fork_req.new_terminal_id] = fork_req
            return fork_req.new_terminal_id
    

    def check_fork_complete(self, fork_rq_id) -> str:
        """Returns the new terminal id if forking has completed, None otherwise"""
        with self.state_lock:
            if fork_rq_id in self.ongoing_forks:
                if self.ongoing_forks[fork_rq_id].fork_complete:
                    return self.ongoing_forks[fork_rq_id].new_terminal_id

            return None
        
        