from server import *
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
        
        new_terminal_id = str(uuid.uuid4())
        with state_lock:
            rollout_env_id[new_terminal_id] = new_rollout_id
            terminals[new_terminal_id] = new_terminal
            sessions[new_terminal_id] = new_session
            trial_path[new_terminal_id] = trial_paths
            task_paths[new_terminal_id] = TaskPaths(input_path=original_task_path)
        
        print(f"Successfully forked terminal using checkpoint. New terminal ID: {new_terminal_id}")
        return jsonify({"id": new_terminal_id}), 200
        
    except Exception as e:
        print(f"Error forking terminal with checkpoint: {e}")
        if 'checkpoint_map' in locals():
            delete_checkpoint(checkpoint_map)
        return jsonify({"error": str(e)}), 500