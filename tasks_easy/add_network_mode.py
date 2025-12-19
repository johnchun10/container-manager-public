#!/usr/bin/env python3

import os
from pathlib import Path
import yaml


def add_network_mode_to_compose_files():
    tasks_easy_dir = Path(__file__).parent
    skipped_dirs = []

    for task_dir in sorted(tasks_easy_dir.iterdir()):
        if not task_dir.is_dir():
            continue

        compose_file = task_dir / "docker-compose.yaml"
        if not compose_file.exists():
            continue

        with open(compose_file, 'r') as f:
            data = yaml.safe_load(f)

        if not data or 'services' not in data:
            continue

        services = data['services']

        if len(services) > 1 or task_dir.name.startswith('jupyter'):
            # Handle multi-service tasks: add test-network for all services
            print(f"Processing multi-service task {task_dir.name} (has {len(services)} services)")

            # Add networks to each service
            for service_name, service_config in services.items():
                if service_config is None:
                    service_config = {}
                    services[service_name] = service_config

                # Add networks configuration
                service_config['networks'] = ['test-network']

            # Add top-level networks configuration
            if 'networks' not in data:
                data['networks'] = {}

            data['networks']['test-network'] = {
                'name': '${T_BENCH_BRIDGE_NETWORK_NAME}',
                'external': True
            }

            with open(compose_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

            skipped_dirs.append(task_dir)
        else:
            # Single service: use network_mode host
            for service_name, service_config in services.items():
                if service_config is None:
                    service_config = {}
                    services[service_name] = service_config

                service_config['network_mode'] = 'host'

            with open(compose_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return skipped_dirs

if __name__ == '__main__':
    skipped_dirs = add_network_mode_to_compose_files()
    
