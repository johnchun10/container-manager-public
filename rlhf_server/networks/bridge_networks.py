import os
import subprocess
from collections import deque
from typing import Deque
from threading import Lock
import time

RETRY_TIMEOUT = 1

class BridgeNetworks:

    def __init__(self, num_networks):
        self.num_networks = num_networks
        self.networks: Deque[str] = deque()
        self.networks_lock: Lock = Lock()
        self.created_count = 0
        self.create_networks(num_networks)


    def run_command(self, command, env_vars=None):
        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)
        
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=env
        )
        return result.returncode == 0, result.stdout, result.stderr

    def _create_network(self, network_name: str):
         success, _, _ = self.run_command(["docker", "network", "create", network_name])
         return success  
    
    def get_network_name(self) -> str:
        return f"test-network-{self.created_count}"

    def create_networks(self, count):

        with self.networks_lock:
            for _ in range(count):
                network_name = self.get_network_name()
                success = self._create_network(network_name)
                if success:
                    self.networks.append(network_name)
                self.created_count += 1


    def delete_networks(self):
        print(f'Deleting {len(self.networks)}')

        with self.networks_lock:
            for idx, network in enumerate(self.networks):
                return_code, stdout, stderr = self.run_command(["docker", "network", "rm", network])

                print(f'Result of deleting network: {network} is: {return_code}, stdout: {stdout} and error: {stderr}')
            
                print(f'Deleted {idx + 1}/{len(self.networks)}')

            # TODO: handle this clear call when deletions fail
            self.networks.clear()
    

    def get_network(self):
        
        with self.networks_lock:
            if len(self.networks) > 0:
                print(f'Number of networks after removing {self.networks[0]} is {len(self.networks) - 1}')
                return self.networks.popleft()
            
            new_network = self.get_network_name()
            self.created_count += 1

            success = False
            while not success:
                success = self._create_network(new_network)
                if not success:
                    print(f'Failed to create network: {new_network} in get')
                    time.sleep(RETRY_TIMEOUT)
                else:
                    print(f'There were no networks to reuse, so creating a new one called: {new_network} and returning that, status of network creation is: {success}')
            return new_network
    
    def put_network(self, network_name: str):

        with self.networks_lock:
            self.networks.append(network_name)
            print(f'Number of networks after adding {network_name} is {len(self.networks)}')
