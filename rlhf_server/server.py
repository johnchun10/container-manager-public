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

# Install and import terminal bench from this commit - 9f7cb4f4e1363db9c07e331ba81c99a02e5bafcd

app = Flask(__name__)
terminals: Dict[str, Terminal] = {}
sessions: Dict[str, TmuxSession] = {}
trial_path: Dict[str, TrialPaths] = {}
task_paths: Dict[str, TaskPaths] = {}

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
    data = request.json
    print("Received start request with data:", data)
    task_name = data['task_name']
    rollout_id = data['rollout_id']
    
    terminal_id = str(uuid.uuid4())
    
    docker_compose_path = Path(f"../tasks/{task_name}/docker-compose.yaml")
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
    
    try:
        terminal.start()
        session = terminal.create_session(session_name="agent", is_active_stream=False, as_configured_user=True)
        
        terminals[terminal_id] = terminal
        sessions[terminal_id] = session
        trial_path[terminal_id] = trial_paths
        task_paths[terminal_id] = TaskPaths(input_path=Path(f'../tasks/{task_name}'))
        
        return jsonify({"id": terminal_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_all_container_by_id(rollout_id: str):
    result = subprocess.run(
        f"docker ps --filter name={rollout_id} --format '{{{{.Names}}}}'",
        shell=True, capture_output=True, text=True
    )
    container_ids = result.stdout.strip().split('\n') if result.returncode == 0 and result.stdout.strip() else []
    return container_ids

def commit_container(container_names: list[str]):
    commit_results = []
    image_names = []
    for container_name in container_names:
        commit_result = subprocess.run(
            ['docker', 'commit', container_name, f'{container_name}_fork'],
            capture_output=True, text=True
        )
        if commit_result.returncode != 0:
            return None, commit_result
        image_names.append(f'{container_name}_fork')
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
        container_name = container_name.replace('_fork', '')
        parts = container_name.split('_')
        if parts[-1] == 'container':
            services_dict['main'] = original_name
        else:
            service_name = parts[-1].split('-')[1]
            services_dict[service_name] = original_name
    return services_dict
def create_forked_task_dir(container_names, task_name):
    original_task_dir = Path(f'../tasks/{task_name}')
    new_task_dir = Path(f'../tasks/{task_name}_fork')
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
    terminal_id = data['id']
    new_rollout_id = data["rollout_id"]
    if terminal_id not in terminals:
        return jsonify({"error": "Terminal not found"}), 404
    task_name = task_paths[terminal_id].input_path.parts[-1]
    client_image_name = terminals[terminal_id]._compose_manager._client_image_name
    rollout_id = client_image_name.split('_', 1)[1].replace('_test', '')

    container_names = get_all_container_by_id(rollout_id)
    image_names, commit_error = commit_container(container_names)

    if commit_error:
        return jsonify({"error": f"Failed to commit containers: {commit_error.stderr}"}), 500
    new_task_dir = create_forked_task_dir(image_names, task_name)
    
    # start a new terminal with the new task dir
    docker_compose_path = new_task_dir / 'docker-compose.yaml'
    new_client_image_name = f'{client_image_name}_fork'
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
        new_session = new_terminal.create_session(session_name="agent", is_active_stream=False, as_configured_user=True)
        
        new_terminal_id = str(uuid.uuid4())
        terminals[new_terminal_id] = new_terminal
        sessions[new_terminal_id] = new_session
        trial_path[new_terminal_id] = trial_paths  # Use the new trial_paths, not the old one
        task_paths[new_terminal_id] = TaskPaths(input_path=Path(f'../tasks/{task_name}_fork'))  # Use forked task directory
        
        return jsonify({"id": new_terminal_id}), 200
    except Exception as e:
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

    if terminal_id not in terminals:
        return jsonify({"error": "Terminal not found"}), 404
    
    try:
        terminals[terminal_id].stop()
        del terminals[terminal_id]
        del sessions[terminal_id]
        if terminal_id in trial_path:
            del trial_path[terminal_id]
        if terminal_id in task_paths:
            del task_paths[terminal_id]
        return jsonify({"status": "stopped"}), 200
    except Exception as e:
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
    
    if terminal_id not in sessions:
        return jsonify({"error": "Session not found"}), 404
    
    # TODO: add if blocking
    try:
        sessions[terminal_id].send_keys([command, 'Enter'], block=blocking, max_timeout_sec=timeout)
        return jsonify({"status": "executed"}), 200
    except Exception as e:
        print(f"Error executing command: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_pane_logs', methods=['POST'])
def get_pane_logs():
    data = request.json
    terminal_id = data['id']
    
    try:
        if terminal_id not in sessions:
            return jsonify({"error": "Session not found"}), 404
        pane_output = sessions[terminal_id].capture_pane(capture_entire=True)
        return jsonify({"logs": pane_output}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/test', methods=['POST'])
def run_tests():
    data = request.json
    terminal_id = data['id']

    if terminal_id not in sessions:
        return jsonify({"error": "trying to test a non-existent session"}), 404
    
    task_path = task_paths[terminal_id]
    terminal = terminals[terminal_id]
    session = sessions[terminal_id]

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
        return jsonify({'error': "Error while setting up enviornment for tests"}), 404

@app.route('/solve', methods=['POST'])
def solve():
    data = request.json
    terminal_id = data['id']
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
        return jsonify({'solved': "Error, command timedout"}), 404
    
    except Exception as error:
        return jsonify({'solved': "Error: {}".format(error)}), 500

@app.route('/hash', methods=['POST'])
def hash():
    data = request.json
    terminal_id = data['id']

    if terminal_id in terminals:
        try:
            return jsonify({'hash': terminals[terminal_id].get_hash()})
        except Exception:
            return jsonify({'hash': 'error'})

    return jsonify({'error': 'Terminal with id {} not found'.format(terminal_id)})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=9901)