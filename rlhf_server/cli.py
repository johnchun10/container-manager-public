from terminal_bench.terminal import Terminal
from terminal_bench.handlers.trial_handler import TrialPaths
from pathlib import Path
import time

# required params
task_name = 'hello-world'
job_id = "test_job"
epoch = 8
rollout_id = 2

docker_compose_path = Path(f"../tasks/{task_name}/docker-compose.yaml")
assert docker_compose_path.exists(), f"File {docker_compose_path} does not exist"

client_image_name = f'{task_name}_{job_id}_{epoch}_{rollout_id}_test'
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
    pre_agent_pane = session.capture_pane(capture_entire=True)
    print("==========PRE==========")
    print(pre_agent_pane)
    session.send_keys("echo test > hello.txt")
    post_agent_pane = session.capture_pane(capture_entire=True)
    print('==========POST==========')
    print(post_agent_pane)
    print('Waiting to kill container')
    time.sleep(30)
except Exception as e:
    print("failed: {}".format(e))
finally:
    terminal.stop()