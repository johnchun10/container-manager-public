#!/bin/bash

# Script to stop and remove all hello-world test containers

# Check if at least one argument is provided
if [ $# -lt 1 ]; then
    echo "Error: No container name pattern provided"
    echo "Usage: $0 <container-name-pattern>"
    exit 1
fi

echo "Stopping and removing $1 test containers..."

# Method 1: Stop and remove by container name pattern
echo "Method 1: Using container name pattern..."
# Stop all containers with the pattern in the name
docker stop $(docker ps -q --filter "name=$1")

# Remove all containers with the pattern in the name  
docker rm $(docker ps -aq --filter "name=$1")