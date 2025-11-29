from flask import Flask, request, jsonify
from terminal_bench.terminal import Terminal
from terminal_bench.handlers.trial_handler import TrialPaths, TaskPaths, Task
from pathlib import Path
import uuid
import subprocess
from typing import Dict
from terminal_bench.terminal.tmux_session import TmuxSession
from terminal_bench.terminal.docker_compose_manager import DockerComposeManager
from terminal_bench.parsers.parser_factory import ParserFactory, ParserName
from terminal_bench.parsers.base_parser import UnitTestStatus
from terminal_bench.terminal.models import TerminalCommand
import yaml
import time
import random
import re
import string
import threading
import os
import re
from tmux import *
from fork import *
# Install and import terminal bench from this commit - 9f7cb4f4e1363db9c07e331ba81c99a02e5bafcd

app = Flask(__name__)

# Global dictionaries
terminals: Dict[str, Terminal] = {}
sessions: Dict[str, TmuxSession] = {}
trial_path: Dict[str, TrialPaths] = {}
task_paths: Dict[str, TaskPaths] = {}
# path = "/home/bk478/susRL/tv-cache/data/appworld-train"
# path = "../env_bench/tasks"
rollout_env_id = {}
task_dir_env_id = {}
terminal_state_dict = {}
last_executed_command = {}

# Single lock for all dictionaries
state_lock = threading.Lock()
fork_save_lock = threading.Lock()
hash_lock = threading.Lock()

# path = "/home/bk478/susRL/tv-cache/data/appworld-train"
# path = "../tasks"
TASKS_ROOT = os.getenv("TASKS_ROOT", "../tasks") 
FLASK_PORT = int(os.getenv("FLASK_PORT", "9901"))
path = TASKS_ROOT
enable_caching = os.getenv("ENABLE_CACHING", "0") == "1"
enable_hashing = False
"""
Start a new terminal session.
Arguments:
- task_name: name of the task (str)
- rollout_id: unique id for the rollout (str)
Returns:
- id: unique id for the terminal session (str)
"""
@app.route('/start', methods=['POST'])
def start_terminal():
    start_time = time.perf_counter()
    data = request.json
    print("Received start request with data:", data)
    task_name = data['task_name']
    if 'rollout_id' not in data:
        rollout_id = str(uuid.uuid4())
    else:
        rollout_id = data['rollout_id']
    
    terminal_id = str(uuid.uuid4())
    
    docker_compose_path = Path(f"{path}/{task_name}/docker-compose.yaml")
    if not docker_compose_path.exists():
        print(f"Error: docker-compose.yaml not found for task {task_name} at {docker_compose_path}")
        return jsonify({"error": f"File {docker_compose_path} does not exist"}), 400
    
    client_image_name = f'{task_name}_{rollout_id}_test'
    client_container_name = f'{client_image_name}_container'
    docker_image_name_prefix = f'{client_image_name}_docker'
    
    trial_paths = TrialPaths(Path(f'../runs/{client_image_name}'), task_name, 'trial_{}'.format(rollout_id))
    trial_paths.mkdir()
    
    terminal = Terminal(
        client_container_name=client_container_name,
        client_image_name=client_image_name,
        docker_image_name_prefix=docker_image_name_prefix,
        docker_compose_path=docker_compose_path,
        sessions_logs_path=trial_paths.sessions_path,
        agent_logs_path=trial_paths.agent_logging_dir,
        commands_path=trial_paths.commands_path,
        no_rebuild=False,
        cleanup=True,
        livestream=False,
        disable_recording=True,
    )
    end_time = time.perf_counter()
    print(f"Initialized terminal in {(end_time - start_time):.2f} seconds")
    try:
        st = time.perf_counter()
        terminal.start()
        et = time.perf_counter()
        print(f"Terminal started in {(et - st):.2f} seconds")
        st = time.perf_counter()
        session = terminal.create_session(session_name="agent", is_active_stream=False, as_configured_user=True)
        
        with state_lock:
            terminals[terminal_id] = terminal
            sessions[terminal_id] = session
            trial_path[terminal_id] = trial_paths
            task_paths[terminal_id] = TaskPaths(input_path=Path(f"{path}/{task_name}"))
            rollout_env_id[terminal_id] = rollout_id

        et = time.perf_counter()

        print(f"Session created in {(et - st):.2f} seconds")
        
        if enable_caching:
            setup_tmux_res_in_container(container_name=client_container_name)
            session.send_keys([f"source {container_resurrect_path}/scripts/startup.sh", 'Enter'], min_timeout_sec=0.1)
        return jsonify({"id": terminal_id}), 200
    
    except Exception as e:
        print(f"Error starting terminal for task {task_name}: {e}")
        return jsonify({"error": str(e)}), 500


def create_forked_task_dir(container_names, task_name, terminal_id):
    with state_lock:
        if terminal_id not in task_dir_env_id:
            print(f"No task directory found for terminal id {terminal_id}, must be first fork")
            original_task_dir = Path(f"{path}/{task_name}")
        else:
            print(f"Using existing task directory for terminal id {terminal_id}")
            original_task_dir = Path(task_dir_env_id[terminal_id])

    random_string = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    new_task_dir = Path(f"{path}/{task_name}_{random_string}")
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

@app.route('/fork', methods=['POST'])
def fork_terminal():
    data = request.json
    if "id" not in data:
        return jsonify({"error": "No terminal id provided"}), 400
    terminal_id = data['id']
    new_rollout_id = str(uuid.uuid4())
    with state_lock:
        if terminal_id not in terminals:
            print("Terminal not found:", terminal_id)
            return jsonify({"error": "Terminal not found"}), 404
        client_image_name = terminals[terminal_id]._compose_manager._client_image_name
        task_name = task_paths[terminal_id].input_path.parts[-1]
        if terminal_id not in rollout_env_id:
            print("Rollout ID not found for terminal:", terminal_id)
            return jsonify({"error": "Rollout ID not found for terminal"}), 404
        rollout_id = rollout_env_id[terminal_id]
        if terminal_id not in sessions:
            return jsonify({"error": "Session not found"}), 404
        session = sessions[terminal_id]

    if enable_caching:
        session.send_keys([f"{container_resurrect_path}/scripts/exit.sh", 'Enter'], min_timeout_sec=0.)

    container_names = get_all_container_by_id(rollout_id)
    with fork_save_lock:
        image_names, commit_error = commit_container(container_names, rollout_id, new_rollout_id)
    if commit_error:
        return jsonify({"error": f"Failed to commit containers: {commit_error.stderr}"}), 500
    new_task_dir = create_forked_task_dir(image_names, task_name, terminal_id)

    client_image_name = client_image_name.replace(rollout_id, new_rollout_id)
    docker_compose_path = new_task_dir / 'docker-compose.yaml'
    new_client_image_name = f'{client_image_name}'
    new_client_container_name = f'{new_client_image_name}_container'
    docker_image_name_prefix = f'{new_client_image_name}_docker'

    trial_paths = TrialPaths(Path(f'../runs/{client_image_name}'), task_name, 'trial_{}'.format(new_rollout_id))
    trial_paths.mkdir()

    try:
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
        )
        new_terminal.start()
        del_result = delete_committed_images(image_names)
        if del_result:
            print(f"Error deleting images: {del_result.stderr}")
        new_session = new_terminal.create_session(session_name="agent", is_active_stream=False, as_configured_user=True)
        new_terminal_id = str(uuid.uuid4())
        
        with state_lock:
            task_dir_env_id[new_terminal_id] = str(new_task_dir)
            rollout_env_id[new_terminal_id] = new_rollout_id
            terminals[new_terminal_id] = new_terminal
            sessions[new_terminal_id] = new_session
            trial_path[new_terminal_id] = trial_paths 
            task_paths[new_terminal_id] = TaskPaths(input_path=Path(f"{new_task_dir}"))

        if enable_caching:
            new_session.send_keys([f"source {container_resurrect_path}/scripts/startup.sh", 'Enter'], min_timeout_sec=0.1)
        return jsonify({"id": new_terminal_id}), 200
    except Exception as e:
        print(f"Error forking terminal: {e}")
        return jsonify({"error": str(e)}), 500

"""
Stop a terminal session.
Arguments:
- id: unique id for the terminal session (str)
Returns:
- status: 'stopped' if successful (str)
"""
@app.route('/stop', methods=['POST'])
def stop_terminal():
    data = request.json
    terminal_id = data['id']

    with state_lock:
        if terminal_id not in terminals:
            print("Received stop request, but terminal not found:", terminal_id)
            return jsonify({"error": "Terminal not found"}), 404
        terminal = terminals[terminal_id]
    
    try:
        terminal.stop()
        
        with state_lock:
            del terminals[terminal_id]
            del sessions[terminal_id]
            if terminal_id in trial_path:
                del trial_path[terminal_id]
            if terminal_id in task_paths:
                del task_paths[terminal_id]
            if terminal_id in rollout_env_id:
                del rollout_env_id[terminal_id]
            if terminal_id in task_dir_env_id:
                forked_task_dir = Path(task_dir_env_id[terminal_id])
                if forked_task_dir.exists():
                    subprocess.run(['rm', '-r', str(forked_task_dir)])
                del task_dir_env_id[terminal_id]
        return jsonify({"status": "stopped"}), 200
    
    except Exception as e:
        print(f" Error stopping terminal: {e} for terminal id {terminal_id}")
        return jsonify({"error": str(e)}), 500
    

def _expand_container_path(user: str, path: str) -> str:
    """
    Expand ~ inside a typical Linux/Docker container:
      - root -> /root
      - other -> /home/<user>
    """
    if not path.startswith('~'):
        return path

    if user == 'root':
        home = '/root'
    else:
        home = f'/home/{user}'

    if path == '~':
        return home
    else:
        # handle things like ~/foo/bar
        return home + path[1:]  # replace leading ~ with home


def get_curr_dir_from_tmux_session(session) -> str:
    PROMPT_RE = re.compile(
    r'(?P<user>[^\s@]+)@(?P<host>[^:\s]+):(?P<path>[^#\$\s]+)[#\$]'
    )
    try:
        pane_output = session.capture_pane(capture_entire=True)
        lines = [line.rstrip('\n') for line in pane_output.splitlines() if line.strip()]

        for line in reversed(lines):
            matches = list(PROMPT_RE.finditer(line))
            if matches:
                last = matches[-1]
                user = last.group('user')
                raw_path = last.group('path').strip()

                path = _expand_container_path(user, raw_path)

                if path.startswith('/'):
                    return path

            stripped = line.strip()
            if stripped.startswith('/'):
                return stripped

        print(f"Warning: Could not parse current directory from pane output, using /app for pane output: {pane_output}")
        return "/app"
    except Exception as e:
        print(f"Error getting current directory from tmux session: {e}")
        return "/app"


def save_session_in_thread(session: TmuxSession, terminal_id: str, curr_dir: str):
    """Save the tmux session and update terminal state in a background thread."""
    
    try:
        with hash_lock:
            with fork_save_lock:
                session.send_keys([f"{container_resurrect_path}/scripts/exit.sh", 'Enter'], min_timeout_sec=1)
            new_curr_dir = get_curr_dir_from_tmux_session(session)
            if curr_dir != new_curr_dir:
                with state_lock:
                    terminal_state_dict[terminal_id] = "stateful"
            else:
                with state_lock:
                    if terminal_id not in terminal_state_dict:
                        terminal_state_dict[terminal_id] = "stateless"
            print(f"Session saved successfully for terminal {terminal_id}")
    except Exception as e:
        print(f"Error saving session in thread for terminal {terminal_id}: {e}")

def start_save_thread(session: TmuxSession, terminal_id: str, curr_dir: str):
    """Start a background thread to save the session."""
    save_thread = threading.Thread(
        target=save_session_in_thread, 
        args=(session, terminal_id, curr_dir),
        daemon=True
    )
    save_thread.start()

"""
Execute a command in the terminal session.
Arguments:
- id: unique id for the terminal session (str)
- command: command to execute (str)
- blocking: whether to block until command completes (bool)
- timeout: maximum time to wait for command to complete (int)
Returns:
- status: 'executed' if successful (str)
"""

@app.route('/execute', methods=['POST'])
def execute_command():
    data = request.json
    print("Received execute request with data:", data)
    terminal_id = data['id']
    command = data['command']
    blocking = data['blocking']
    timeout = data['timeout']
    
    with state_lock:
        if terminal_id not in sessions:
            return jsonify({"error": "Session not found"}), 404
        session = sessions[terminal_id]
    
    # TODO: add if blocking
    try:
        # if enable_caching:
        #     session.send_keys([f"source {container_resurrect_path}/scripts/startup.sh", 'Enter'], min_timeout_sec=1)
        if enable_hashing:
            curr_dir = get_curr_dir_from_tmux_session(session)
        # with state_lock:
        #     last_executed_command[terminal_id] = command
        session.send_keys([command, 'Enter'], block=blocking, max_timeout_sec=timeout)
        # if enable_caching:
        #     start_save_thread(session, terminal_id, curr_dir)
        
        return jsonify({"status": "executed"}), 200
    except TimeoutError as te:
        print(f'Timed out executing command: {command} in terminal {terminal_id}')
        return jsonify({"error": str(te)}), 504
    except Exception as e:
        print(f"Error executing command: {e}")
        return jsonify({"error": str(e)}), 500
    
def trim_before_command(text: str, command: str) -> str:
    cmd = command.strip()
    lines = text.splitlines()
    
    for i, line in enumerate(lines):
        if cmd in line:
            # keep from this line onward
            return "\n".join(lines[i:])
    
    return text

@app.route('/get_pane_logs', methods=['POST'])
def get_pane_logs():
    data = request.json
    terminal_id = data['id']
    
    try:
        with state_lock:
            if terminal_id not in sessions:
                return jsonify({"error": "Session not found"}), 404
            session = sessions[terminal_id]
            last_executed_cmd = last_executed_command.get(terminal_id, "")
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
        # if enable_caching:
        #     if last_executed_cmd:
        #         pane_output = trim_before_command(pane_output, last_executed_cmd)
        # print("Filtered pane output:", pane_output)
        return jsonify({"logs": pane_output}), 200
    except Exception as e:
        print(f"Error getting pane logs: {e} for terminal {terminal_id}")
        return jsonify({"error": str(e)}), 500

@app.route('/test', methods=['POST'])
def run_tests():
    data = request.json
    terminal_id = data['id']

    with state_lock:
        if terminal_id not in sessions:
            return jsonify({"error": "trying to test a non-existent session"}), 404
        session = sessions[terminal_id]
        task_path = task_paths[terminal_id]
        terminal = terminals[terminal_id]

    try:
        paths = [
            task_path.run_tests_path,
        ]

        if task_path.test_dir.exists():
            paths.append(task_path.test_dir)

        terminal.copy_to_container(
            paths=paths,
            container_dir=str(DockerComposeManager.CONTAINER_TEST_DIR),
        )

        session.send_keys(
                [
                    "bash ",
                    str(
                        DockerComposeManager.CONTAINER_TEST_DIR
                        / task_path.run_tests_path.name
                    ),
                    "Enter",
                ],
                block=True,
                max_timeout_sec=180, # TODO: Get this number from actual test times?
            )
        task = Task.from_yaml(task_path.task_config_path)
        parser = ParserFactory.get_parser(task.parser_name)

        post_test_pane = session.capture_pane(capture_entire=True)
        parsed_response = parser.parse(post_test_pane)

        test_result = all(
            result == UnitTestStatus.PASSED for result in parsed_response.values()
        )

        return jsonify({'passed': test_result})

    except Exception as e:
        import traceback
        print(f"Error while setting up environment for tests: {traceback.format_exc()}")
        return jsonify({'error': f"Error while setting up environment for tests: {e}"}), 404

@app.route('/solve', methods=['POST'])
def solve():
    data = request.json
    terminal_id = data['id']
    
    with state_lock:
        solution_path = task_paths[terminal_id].solution_path
        session = sessions[terminal_id]

    if solution_path.suffix == ".sh":
        solution = {
            "type": "sh",
            "path": solution_path
        }
    else:
        solution = {
            "type": "yaml",
            "commands": TerminalCommand.from_yaml_list(
                solution_path
            ),
        }

    try:
        if solution["type"] == "sh":
            session.copy_to_container(
                solution["path"],
                container_dir="/oracle",
                container_filename="solution.sh",
            )
            session.send_keys(
                ["bash /oracle/solution.sh", "Enter"],
                max_timeout_sec=float("inf"),
                block=True,
            )
        else:
            for command in solution["commands"]:
                session.send_command(command)

        return jsonify({'solved': True})
    
    except TimeoutError as error:
        print(f"Error solving terminal {terminal_id}: {error}")
        return jsonify({'solved': "Error, command timedout"}), 404
    
    except Exception as error:
        print(f"Error solving terminal {terminal_id}: {error}")
        return jsonify({'solved': "Error: {}".format(error)}), 500

@app.route('/hash', methods=['POST'])
def hash():
    return jsonify({'hash': "1111"})
    data = request.json
    terminal_id = data['id']

    with state_lock:
        if terminal_id in terminals:
            terminal = terminals[terminal_id]
        else:
            return jsonify({'error': 'Terminal with id {} not found'.format(terminal_id)})
    
    try:
        hash = terminal.get_hash()
        if hash == "0000":
            with state_lock and hash_lock:
                if terminal_id in terminal_state_dict:
                    if terminal_state_dict[terminal_id] == "stateful":
                        return jsonify({'hash': "1111"})
                    else:
                        return jsonify({'hash': "0000"})
                else:
                    print(f"State not updated yet for terminal {terminal_id}, returning stateful hash")
                    return jsonify({'hash': "1111"})

        return jsonify({'hash': hash})
    except Exception as e:
        print(f"Error getting hash for terminal {terminal_id}: {e}")
        return jsonify({'hash': 'error'})

if __name__ == '__main__':
    print(f"Starting RLHF server on port {FLASK_PORT} with tasks root {TASKS_ROOT} and caching set to {enable_caching}")
    app.run(debug=False, host='0.0.0.0', port=FLASK_PORT)
