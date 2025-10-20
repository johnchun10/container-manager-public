from flask import Flask, request, jsonify
from terminal_bench.terminal import Terminal
from terminal_bench.handlers.trial_handler import TrialPaths, TaskPaths, Task
from pathlib import Path
import uuid
from typing import Dict
from terminal_bench.terminal.tmux_session import TmuxSession
from terminal_bench.terminal.docker_compose_manager import DockerComposeManager
from terminal_bench.parsers.parser_factory import ParserFactory, ParserName
from terminal_bench.parsers.base_parser import UnitTestStatus
from terminal_bench.terminal.models import TerminalCommand

# Install and import terminal bench from this commit - 9f7cb4f4e1363db9c07e331ba81c99a02e5bafcd

app = Flask(__name__)
terminals: Dict[str, Terminal] = {}
sessions: Dict[str, TmuxSession] = {}
trial_path: Dict[str, TrialPaths] = {}
task_paths: Dict[str, TaskPaths] = {}

@app.route('/start', methods=['POST'])
def start_terminal():
    data = request.json
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
        return jsonify({"status": "stopped"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/execute', methods=['POST'])
def execute_command():
    data = request.json
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
    app.run(debug=True, host='0.0.0.0', port=9800)