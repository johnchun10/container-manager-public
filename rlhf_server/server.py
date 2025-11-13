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

# Install and import terminal bench from this commit - 9f7cb4f4e1363db9c07e331ba81c99a02e5bafcd

app = Flask(__name__)

# Global dictionaries
terminals: Dict[str, Terminal] = {}
sessions: Dict[str, TmuxSession] = {}
trial_path: Dict[str, TrialPaths] = {}
task_paths: Dict[str, TaskPaths] = {}
rollout_env_id = {}
task_dir_env_id = {}

# Single lock for all dictionaries
state_lock = threading.Lock()

# path = "/home/bk478/susRL/tv-cache/data/appworld-train"
path = "../tasks"
container_resurrect_path = "/root/tmux_resurrect"


# @app.route('/env_to_rollout', methods=['POST'])
# def get_rollout_from_env():
#     data = request.json
#     env_id = data['env_id']
#     if env_id in rollout_env_id:
#         return jsonify({'rollout_id': rollout_env_id[env_id]}), 200
#     else:
#         return jsonify({'error': 'Env ID not found'}), 404
    
# @app.route('/rollout_to_env', methods=['POST'])
# def get_env_from_rollout():
#     data = request.json
#     rollout_id = data['rollout_id']
#     for env_id, r_id in rollout_env_id.items():
#         if r_id == rollout_id:
#             return jsonify({'env_id': env_id}), 200
#     return jsonify({'error': 'Rollout ID not found'}), 404

def setup_tmux_res_in_container(container_name: str):
    """Copy the tmux resurrect files to the specified container."""
    # /home/bk478/susRL/tv-cache/rlhf-container-manager/rlhf_server/tmux-res
    try: 
        local_resurrect_path = "/home/bk478/susRL/tv-cache/rlhf-container-manager/rlhf_server/tmux-res"
        subprocess.run(
                ["docker", "cp", str(local_resurrect_path), f"{container_name}:{container_resurrect_path}"],
                check=True
            )
            # docker cp /bin/ps <container>:/usr/local/bin/ps
        # subprocess.run(
        #     ["docker", "cp", "/bin/ps", f"{container_name}:/bin/ps"],
        #     check=True
        # )
        # update ~/.tmux.conf
        subprocess.run(
            [
                "docker", "exec", container_name,
                "bash", "-c",
                f"chmod -R 755 '{container_resurrect_path}' && "
                f"echo \"set -g @resurrect-dir '{container_resurrect_path}'\" >> ~/.tmux.conf && "
                "echo \"set -g @resurrect-capture-pane-contents 'on'\" >> ~/.tmux.conf && "
                "echo \"set -g @resurrect-save-bash-history 'on'\" >> ~/.tmux.conf && "
                f"echo \"run-shell '{container_resurrect_path}/resurrect.tmux'\" >> ~/.tmux.conf && "
                "tmux source-file ~/.tmux.conf"
            ],
            check=True
        )
        # subprocess.run(
        # [
        #     "docker", "exec", container_name,
        #     "tmux", "send-keys", "-t", "agent", 
        #     f"chmod -R 755 '{container_resurrect_path}'"
        # ],
        # check=True,
        # text=True
        # )
    except Exception as e:
        print(f"Error copying tmux resurrect to container {container_name}: {e}")

def save_tmux_session(container_name: str):
    """Save the specified tmux session in the container using tmux-resurrect."""
    print("Saving tmux session for container:", container_name)
    try:
        # subprocess.run(
        # [
        #     "docker", "exec", container_name,
        #     "bash", "-c",
        #     f"chmod -R 755 '{container_resurrect_path}'"
        # ],
        # check=True,
        # text=True
        # )
        subprocess.run([
            "docker", "exec", container_name,
            "tmux", "send-keys", "-t", "agent",   # name of the existing session
            f"{container_resurrect_path}/scripts/save.sh", "Enter"
        ], check=True)
        
    except Exception as e:
        print(f"Error saving tmux session in container {container_name}: {e}")

def restore_tmux_session(container_name: str):
    """Restore the specified tmux session in the container using tmux-resurrect."""
    print("Restoring tmux session for container:", container_name)
    try:
        # subprocess.run(
        # [
        #     "docker", "exec", container_name,
        #     "tmux", "send-keys", "-t", "agent", 
        #     f"chmod -R 755 '{container_resurrect_path}'", "Enter"
        # ],
        # check=True,
        # text=True
        # )
        subprocess.run([
            "docker", "exec", container_name,
            "tmux", "send-keys", "-t", "agent",   # name of the existing session
            f"{container_resurrect_path}/scripts/restore.sh", "Enter"
        ], check=True)
    except Exception as e:
        print(f"Error restoring tmux session in container {container_name}: {e}")

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
    
    # docker_compose_path = Path(f"../tasks/{task_name}/docker-compose.yaml")
    docker_compose_path = Path(f"{path}/{task_name}/docker-compose.yaml")
    if not docker_compose_path.exists():
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
        disable_recording=False,
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
        setup_tmux_res_in_container(container_name=client_container_name)
        return jsonify({"id": terminal_id}), 200
    except Exception as e:
        print(f"Error starting terminal for task {task_name}: {e}")
        return jsonify({"error": str(e)}), 500

def get_all_container_by_id(rollout_id: str):
    result = subprocess.run(
        f"docker ps --filter name={rollout_id} --format '{{{{.Names}}}}'",
        shell=True, capture_output=True, text=True
    )
    container_ids = result.stdout.strip().split('\n') if result.returncode == 0 and result.stdout.strip() else []
    return container_ids

def commit_container(container_names: list[str], old_rollout_id: str, new_rollout_id: str):
    commit_results = []
    image_names = []
    for container_name in container_names:
        new_container_name = container_name.replace(old_rollout_id, new_rollout_id)
        commit_result = subprocess.run(
            ['docker', 'commit', container_name, f'{new_container_name}'],
            capture_output=True, text=True
        )
        if commit_result.returncode != 0:
            return None, commit_result
        image_names.append(f'{new_container_name}')
        commit_results.append(commit_result)
    return image_names, None

def delete_committed_images(image_names: list[str]):
    commit_results = []
    for image_name in image_names:
        commit_result = subprocess.run(
            ['docker', 'rmi', '-f', image_name],
            capture_output=True, text=True
        )
        if commit_result.returncode != 0:
            return commit_result
        commit_results.append(commit_result)
    return None
#container names are image names
def get_service_name_from_container_name(container_names):
    # postgres-csv-clean_0f3e208d-dfdc-45cf-96c6-4a45c8424593_test_container-db-1
    #
    services_dict = {}
    for container_name in container_names:
        original_name = container_name
        while '_fork' in container_name:
            container_name = container_name.replace('_fork', '')
        parts = container_name.split('_')
        if parts[-1] == 'container':
            services_dict['main'] = original_name
        else:
            service_name = parts[-1].split('-')[1]
            services_dict[service_name] = original_name
    return services_dict
def create_forked_task_dir(container_names, task_name, terminal_id):
    with state_lock:
        if terminal_id not in task_dir_env_id:
            print(f"No task directory found for terminal id {terminal_id}, must be first fork")
            original_task_dir = Path(f"{path}/{task_name}")
        else:
            print(f"Using existing task directory for terminal id {terminal_id}")
            original_task_dir = Path(task_dir_env_id[terminal_id])
    # random 6 char string
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

    # remove the build section if exists
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
    # if 'rollout_id' not in data:
    #     new_rollout_id = str(uuid.uuid4())
    # else:
    #     new_rollout_id = data["rollout_id"]
    
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
    
    # base_container_name = terminals[terminal_id]._compose_manager._client_container_name
    # sessions[terminal_id].send_keys([f"{container_resurrect_path}/scripts/save.sh", 'Enter'], block=True, max_timeout_sec=5)
    # save_tmux_session(container_name=base_container_name)
    
    # rollout_id = client_image_name.split('_', 1)[1].replace('_test', '')
    print("Rolling out from rollout id:", rollout_id)

    container_names = get_all_container_by_id(rollout_id)
    print("Container names:", container_names)
    image_names, commit_error = commit_container(container_names, rollout_id, new_rollout_id)
    print("Committed forked image names:", image_names)
    if commit_error:
        return jsonify({"error": f"Failed to commit containers: {commit_error.stderr}"}), 500
    new_task_dir = create_forked_task_dir(image_names, task_name, terminal_id)

    client_image_name = client_image_name.replace(rollout_id, new_rollout_id)
    # start a new terminal with the new task dir
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
        # print("deleting image names: ", image_names)
        del_result = delete_committed_images(image_names)
        if del_result:
            print(f"Error deleting images: {del_result.stderr}")
        print("deleted committed images successfully")
        # remove new directory
        # subprocess.run(['rm', '-r', str(new_task_dir)])
        
        print(f"list of running containers {image_names}")
        new_session = new_terminal.create_session(session_name="agent", is_active_stream=False, as_configured_user=True)
        print("created new session successfully")
        new_terminal_id = str(uuid.uuid4())
        
        with state_lock:
            task_dir_env_id[new_terminal_id] = str(new_task_dir)
            rollout_env_id[new_terminal_id] = new_rollout_id
            terminals[new_terminal_id] = new_terminal
            sessions[new_terminal_id] = new_session
            trial_path[new_terminal_id] = trial_paths  # Use the new trial_paths, not the old one
            task_paths[new_terminal_id] = TaskPaths(input_path=Path(f"{path}/{new_task_dir}"))  # Use forked task directory
        # restore_tmux_session(container_name=new_client_container_name)
        # sessions[terminal_id].send_keys([f"{container_resurrect_path}/scripts/restore.sh", 'Enter'], block=True, max_timeout_sec=5)
        return jsonify({"id": new_terminal_id}), 200
    except Exception as e:
        print(f"Error forking terminal: {e}")
        return jsonify({"error": str(e)}), 500

def checkpoint_containers(container_names: list[str], old_rollout_id: str, new_rollout_id: str):
    """Create checkpoints for all containers with unique checkpoint names per container.
    
    Checkpoint names follow the same pattern as container names: replace old_rollout_id with new_rollout_id.
    This keeps checkpoint names clean and consistent.
    """
    checkpoint_map = {}  # Maps container_name -> checkpoint_name
    checkpoint_results = []
    
    for container_name in container_names:
        # Create checkpoint name by replacing rollout ID, similar to how we handle container/image names
        checkpoint_name = container_name.replace(old_rollout_id, new_rollout_id)
        
        # Docker checkpoint requires CRIU and experimental features enabled
        checkpoint_result = subprocess.run(
            ['docker', 'checkpoint', 'create', container_name, checkpoint_name],
            capture_output=True, text=True
        )
        if checkpoint_result.returncode != 0:
            # Clean up any checkpoints created before the failure
            for prev_container, prev_checkpoint in checkpoint_map.items():
                subprocess.run(
                    ['docker', 'checkpoint', 'rm', prev_container, prev_checkpoint],
                    capture_output=True, text=True
                )
            return None, None, checkpoint_result
        
        checkpoint_map[container_name] = checkpoint_name
        checkpoint_results.append(checkpoint_result)
    
    return checkpoint_results, checkpoint_map, None

def restore_containers_from_checkpoint(container_names: list[str], checkpoint_map: dict):
    """Restore containers from checkpoint with new names.
    
    Since checkpoint_name = new_container_name (both use new_rollout_id), 
    we can directly restore with a single name.
    """
    restored_containers = []
    
    for container_name in container_names:
        # Both checkpoint name and new container name use new_rollout_id
        checkpoint_name = checkpoint_map[container_name]
        
        # Start the container from checkpoint - name is used for both checkpoint and container
        restore_result = subprocess.run(
            ['docker', 'start', '--checkpoint', checkpoint_name, checkpoint_name],
            capture_output=True, text=True
        )
        if restore_result.returncode != 0:
            return None, restore_result

        restored_containers.append(checkpoint_name)

    return restored_containers, None

def delete_checkpoint(checkpoint_map: dict):
    """Delete checkpoints for all containers using the checkpoint map."""
    for container_name, checkpoint_name in checkpoint_map.items():
        subprocess.run(
            ['docker', 'checkpoint', 'rm', container_name, checkpoint_name],
            capture_output=True, text=True
        )

@app.route('/fork_checkpoint', methods=['POST'])
def fork_terminal_checkpoint():
    """Fork a terminal session using Docker checkpoint instead of commit."""
    data = request.json
    if "id" not in data:
        return jsonify({"error": "No terminal id provided"}), 400
    
    terminal_id = data['id']
    new_rollout_id = str(uuid.uuid4())
    
    with state_lock:
        if terminal_id not in terminals:
            print("Terminal not found:", terminal_id)
            return jsonify({"error": "Terminal not found"}), 404
        task_name = task_paths[terminal_id].input_path.parts[-1]
        if terminal_id not in rollout_env_id:
            print("Rollout ID not found for terminal:", terminal_id)
            return jsonify({"error": "Rollout ID not found for terminal"}), 404
        rollout_id = rollout_env_id[terminal_id]
    print(f"Creating checkpoint from rollout id: {rollout_id}")

    # Get all container names for this rollout
    container_names = get_all_container_by_id(rollout_id)
    print(f"Container names to checkpoint: {container_names}")
    
    # Create checkpoints for all containers (each gets a unique checkpoint name by replacing rollout ID)
    checkpoint_results, checkpoint_map, checkpoint_error = checkpoint_containers(container_names, rollout_id, new_rollout_id)
    if checkpoint_error:
        return jsonify({"error": f"Failed to checkpoint containers: {checkpoint_error.stderr}"}), 500
    print(f"Successfully created checkpoints: {checkpoint_map}")
    
    try:
        # Restore containers from checkpoint with new names
        # Note: Checkpoints are self-sufficient and contain the entire container state,
        # so we don't need to copy task directories or modify docker-compose files
        restored_containers, restore_error = restore_containers_from_checkpoint(
            container_names,  checkpoint_map
        )
        if restore_error:
            delete_checkpoint(checkpoint_map)
            return jsonify({"error": f"Failed to restore containers: {restore_error.stderr}"}), 500
        
        print(f"Successfully restored containers from checkpoint: {restored_containers}")
        
        # Clean up checkpoints after successful restore
        delete_checkpoint(checkpoint_map)
        
        # Create trial paths for logging
        with state_lock:
            client_image_name = terminals[terminal_id]._compose_manager._client_image_name.replace(rollout_id, new_rollout_id)
            original_task_path = task_paths[terminal_id].input_path
        
        trial_paths = TrialPaths(
            Path(f'../runs/{client_image_name}'), 
            task_name, 
            f'trial_{new_rollout_id}'
        )
        trial_paths.mkdir()
        
        # For checkpoint-based fork, we use the original task path since containers are already running
        # with their filesystem state intact from the checkpoint
        
        # Create a new terminal object that manages the restored containers
        # Note: This terminal object is mainly for session management since containers are already running
        new_client_container_name = f'{client_image_name}_container'
        docker_image_name_prefix = f'{client_image_name}_docker'
        
        # Use the original docker-compose path since containers are already restored
        original_docker_compose_path = Path(f"{original_task_path}/docker-compose.yaml")
        
        new_terminal = Terminal(
            client_container_name=new_client_container_name,
            client_image_name=client_image_name,
            docker_image_name_prefix=docker_image_name_prefix,
            docker_compose_path=original_docker_compose_path,
            sessions_logs_path=trial_paths.sessions_path,
            agent_logs_path=trial_paths.agent_logging_dir,
            commands_path=trial_paths.commands_path,
            no_rebuild=True,  # Don't rebuild, containers are already running from checkpoint
            cleanup=True,
            livestream=False,
            disable_recording=False,
        )
        
        # Attach to the restored containers
        new_session = new_terminal.create_session(
            session_name="agent", 
            is_active_stream=False, 
            as_configured_user=True
        )
        
        # Register the new terminal
        new_terminal_id = str(uuid.uuid4())
        with state_lock:
            rollout_env_id[new_terminal_id] = new_rollout_id
            terminals[new_terminal_id] = new_terminal
            sessions[new_terminal_id] = new_session
            trial_path[new_terminal_id] = trial_paths
            task_paths[new_terminal_id] = TaskPaths(input_path=original_task_path)
        # Note: We don't set task_dir_env_id for checkpoint-based forks since we use the original task directory
        
        print(f"Successfully forked terminal using checkpoint. New terminal ID: {new_terminal_id}")
        return jsonify({"id": new_terminal_id}), 200
        
    except Exception as e:
        print(f"Error forking terminal with checkpoint: {e}")
        # Clean up on error
        if 'checkpoint_map' in locals():
            delete_checkpoint(checkpoint_map)
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
                # remove the forked task directory
                forked_task_dir = Path(task_dir_env_id[terminal_id])
                if forked_task_dir.exists():
                    subprocess.run(['rm', '-r', str(forked_task_dir)])
                del task_dir_env_id[terminal_id]
        return jsonify({"status": "stopped"}), 200
    except Exception as e:
        print(f" Error stopping terminal: {e}")
        return jsonify({"error": str(e)}), 500

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
        session.send_keys([f"{container_resurrect_path}/scripts/restore.sh", 'Enter'], block=True, max_timeout_sec=2, min_timeout_sec=1)
        session.send_keys([command, 'Enter'], block=blocking, max_timeout_sec=timeout)
        session.send_keys([f"{container_resurrect_path}/scripts/save.sh", 'Enter'], block=True, max_timeout_sec=2, min_timeout_sec=1)
        return jsonify({"status": "executed"}), 200
    except Exception as e:
        print(f"Error executing command: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_pane_logs', methods=['POST'])
def get_pane_logs():
    data = request.json
    terminal_id = data['id']
    
    try:
        with state_lock:
            if terminal_id not in sessions:
                return jsonify({"error": "Session not found"}), 404
            session = sessions[terminal_id]
        pane_output = session.capture_pane(capture_entire=True)
        # print("Captured pane output:", pane_output)
        # Split by newlines and filter out unwanted lines
        pane_output = [
            line for line in pane_output.splitlines()
            if "scripts/save.sh" not in line 
            and "scripts/restore.sh" not in line 
            and "scripts/ps.sh" not in line
            and "ps: command not found" not in line
        ]
        # remove empty lines
        pane_output = [line for line in pane_output if line.strip() != '']
        # Replace root@<containerid>: prefix with terminal-bench:
        pane_output = [re.sub(r'^root@[^:]+:', 'root@terminal:', line) for line in pane_output]
        pane_output = "\n".join(pane_output)
        print("Filtered pane output:", pane_output)
        return jsonify({"logs": pane_output}), 200
    except Exception as e:
        print(f"Error getting pane logs: {e}")
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

    # setup the environment to run tests
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
        print(f"Error while setting up environment for tests: {e}")
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
    data = request.json
    terminal_id = data['id']

    with state_lock:
        if terminal_id in terminals:
            terminal = terminals[terminal_id]
        else:
            return jsonify({'error': 'Terminal with id {} not found'.format(terminal_id)})
    
    try:
        return jsonify({'hash': terminal.get_hash()})
    except Exception as e:
        print(f"Error getting hash for terminal {terminal_id}: {e}")
        return jsonify({'hash': 'error'})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=9901)


### ~/tmux.conf
# ctrl b ctrl s to save
# ctrl b ctrl r to restore
# set -g @resurrect-capture-pane-contents 'on'
# set -g @resurrect-save-bash-history 'on'
# run-shell ~/tmux-resurrect/resurrect.tmux
#