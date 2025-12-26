import os
from pathlib import Path
import yaml

def find_multi_service_compose_dirs(root_dir):
    """
    Scan directories for docker-compose.yaml files and identify those with more than 1 service.
    
    Args:
        root_dir: Root directory to start scanning from
    """
    root_path = Path(root_dir)
    
    # Common docker-compose file names
    compose_filenames = ['docker-compose.yaml', 'docker-compose.yml', 'compose.yaml', 'compose.yml']
    
    multi_service_dirs = []
    
    # Walk through all directories
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Check if any compose file exists in this directory
        compose_file = None
        for filename in compose_filenames:
            if filename in filenames:
                compose_file = os.path.join(dirpath, filename)
                break
        
        if compose_file:
            try:
                # Read and parse the compose file
                with open(compose_file, 'r') as f:
                    compose_data = yaml.safe_load(f)
                
                # Check if 'services' key exists and count services
                if compose_data and 'services' in compose_data:
                    service_count = len(compose_data['services'])
                    
                    if service_count > 1:
                        multi_service_dirs.append({
                            'directory': dirpath,
                            'file_path': compose_file,
                            'service_count': service_count,
                            'compose_file': os.path.basename(compose_file)
                        })
                        
            except yaml.YAMLError as e:
                print(f"Error parsing {compose_file}: {e}")
            except Exception as e:
                print(f"Error reading {compose_file}: {e}")
    
    return multi_service_dirs

def find_compose_with_ports_or_networks(root_dir):
    """
    Scan directories for docker-compose.yaml files and identify those with either ports or networks specified.
    
    Args:
        root_dir: Root directory to start scanning from
    
    Returns:
        List of dictionaries containing information about compose files with ports or networks
    """
    root_path = Path(root_dir)
    
    # Common docker-compose file names
    compose_filenames = ['docker-compose.yaml', 'docker-compose.yml', 'compose.yaml', 'compose.yml']
    
    results = []
    
    # Walk through all directories
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Check if any compose file exists in this directory
        compose_file = None
        for filename in compose_filenames:
            if filename in filenames:
                compose_file = os.path.join(dirpath, filename)
                break
        
        if compose_file:
            try:
                # Read and parse the compose file
                with open(compose_file, 'r') as f:
                    compose_data = yaml.safe_load(f)
                
                if compose_data and 'services' in compose_data:
                    # Check if any service has ports defined
                    has_ports = False
                    services_with_ports = []
                    
                    for service_name, service_config in compose_data['services'].items():
                        if service_config and 'ports' in service_config:
                            has_ports = True
                            services_with_ports.append(service_name)
                    
                    # Check if networks are defined (either at root level or in services)
                    has_networks = False
                    network_info = {
                        'root_level': False,
                        'service_level': False,
                        'network_names': []
                    }
                    
                    # Check root-level networks definition
                    if 'networks' in compose_data:
                        has_networks = True
                        network_info['root_level'] = True
                        network_info['network_names'] = list(compose_data['networks'].keys())
                    
                    # Check service-level networks
                    for service_name, service_config in compose_data['services'].items():
                        if service_config and 'networks' in service_config:
                            has_networks = True
                            network_info['service_level'] = True
                    
                    # Add if either ports OR networks are present
                    if has_ports or has_networks:
                        results.append({
                            'directory': dirpath,
                            'file_path': compose_file,
                            'compose_file': os.path.basename(compose_file),
                            'service_count': len(compose_data['services']),
                            'has_ports': has_ports,
                            'services_with_ports': services_with_ports,
                            'has_networks': has_networks,
                            'network_info': network_info
                        })
                        
            except yaml.YAMLError as e:
                print(f"Error parsing {compose_file}: {e}")
            except Exception as e:
                print(f"Error reading {compose_file}: {e}")
    
    return results

def main():
    # Change this to your target directory
    root_directory = "."  # Current directory, or specify a path like "/path/to/scan"
    
    print(f"Scanning directory: {os.path.abspath(root_directory)}\n")
    
    # Find multi-service compose files
    multi_service_results = find_multi_service_compose_dirs(root_directory)
    
    if multi_service_results:
        print(f"Found {len(multi_service_results)} directories with docker-compose files containing more than 1 service:\n")
        for result in multi_service_results:
            print(f"Directory: {result['directory']}")
            print(f"  File Path: {result['file_path']}")
            print(f"  Services: {result['service_count']}")
            print()
    else:
        print("No directories found with docker-compose files containing more than 1 service.\n")
    
    print("=" * 80)
    print()
    
    # Find compose files with either ports or networks
    ports_networks_results = find_compose_with_ports_or_networks(root_directory)
    
    if ports_networks_results:
        print(f"Found {len(ports_networks_results)} directories with docker-compose files containing ports or networks:\n")
        for result in ports_networks_results:
            print(f"Directory: {result['directory']}")
            print(f"  File Path: {result['file_path']}")
            print(f"  Total Services: {result['service_count']}")
            print(f"  Has Ports: {result['has_ports']}")
            if result['has_ports']:
                print(f"    Services with Ports: {', '.join(result['services_with_ports'])}")
            print(f"  Has Networks: {result['has_networks']}")
            if result['has_networks']:
                if result['network_info']['network_names']:
                    print(f"    Network names: {', '.join(result['network_info']['network_names'])}")
                print(f"    Networks at root level: {result['network_info']['root_level']}")
                print(f"    Networks in services: {result['network_info']['service_level']}")
            print()
    else:
        print("No directories found with docker-compose files containing ports or networks.")

if __name__ == "__main__":
    main()