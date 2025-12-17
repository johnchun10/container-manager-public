import subprocess
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

def get_service_name_from_container_name(container_names):
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