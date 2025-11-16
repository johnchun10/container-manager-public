import subprocess
container_resurrect_path = "/root/tmux_resurrect"

def setup_tmux_res_in_container(container_name: str):
    """Copy the tmux resurrect files to the specified container."""
    try: 
        local_resurrect_path = "/home/bk478/susRL/tv-cache/rlhf-container-manager/rlhf_server/tmux-res"
        subprocess.run(
                ["docker", "cp", str(local_resurrect_path), f"{container_name}:{container_resurrect_path}"],
                check=True
            )
        subprocess.run(
            [
                "docker", "exec", container_name,
                "bash", "-c",
                f"chmod -R 755 '{container_resurrect_path}' && "
                f"echo \"set -g @resurrect-dir '{container_resurrect_path}'\" >> ~/.tmux.conf && "
                "echo \"set -g @resurrect-capture-pane-contents 'on'\" >> ~/.tmux.conf && "
                "echo \"set -g @resurrect-save-bash-history 'on'\" >> ~/.tmux.conf && "
#                 set -g @resurrect-save-shell-history 'on'
# set -g @resurrect-capture-pane-contents 'on'
# set -g @resurrect-strategy-vim 'session'
                "echo \"set -g @resurrect-save-shell-history 'on'\" >> ~/.tmux.conf && "
                f"echo \"set -g @resurrect-strategy-vim 'session'\" >> ~/.tmux.conf && "
                f"echo \"set -g @resurrect-strategy 'vim'\" >> ~/.tmux.conf && "

                f"echo \"run-shell '{container_resurrect_path}/resurrect.tmux'\" >> ~/.tmux.conf && "
                "tmux source-file ~/.tmux.conf && "
                "hostname terminal"
            ],
            check=True
        )
    except Exception as e:
        print(f"Error copying tmux resurrect to container {container_name}: {e}")

def save_tmux_session(container_name: str):
    """Save the specified tmux session in the container using tmux-resurrect."""
    try:
        subprocess.run([
            "docker", "exec", container_name,
            "tmux", "send-keys", "-t", "agent",   # name of the existing session
            f"{container_resurrect_path}/scripts/save.sh", "Enter"
        ], check=True)
        
    except Exception as e:
        print(f"Error saving tmux session in container {container_name}: {e}")

def restore_tmux_session(container_name: str):
    """Restore the specified tmux session in the container using tmux-resurrect."""
    try:
        subprocess.run([
            "docker", "exec", container_name,
            "tmux", "send-keys", "-t", "agent",   # name of the existing session
            f"{container_resurrect_path}/scripts/restore.sh", "Enter"
        ], check=True)
    except Exception as e:
        print(f"Error restoring tmux session in container {container_name}: {e}")
