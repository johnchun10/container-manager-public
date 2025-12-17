import yaml
from pathlib import Path


def does_container_need_bridge_network(compose_file_path: str) -> bool:
    """
    Parse a Docker Compose file and check if it needs a bridge network.
    """
    try:
        compose_path = Path(compose_file_path)

        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)

        networks = compose_data.get('networks', {})

        return 'test-network' in networks

    except (yaml.YAMLError, IOError) as e:
        return False
    
    except Exception as e:
        raise e