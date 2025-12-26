#!/bin/bash

sudo bash -c 'cat > /etc/docker/daemon.json << "EOF"
{
  "default-address-pools": [
    {
      "base": "10.0.0.0/8",
      "size": 24
    }
  ]
}
EOF'
sudo cat /etc/docker/daemon.json
sudo systemctl restart docker

set -e  # Exit on error

echo "Cleaning up old Docker configurations..."
# Remove ALL old Docker repository files
sudo rm -f /etc/apt/sources.list.d/docker.list
sudo rm -f /etc/apt/sources.list.d/docker.sources
sudo rm -f /etc/apt/cloud-init.gpg.d/docker.gpg
sudo rm -f /etc/apt/keyrings/docker.asc
sudo rm -f /etc/apt/keyrings/docker.gpg

echo "Removing old Docker packages..."
sudo apt remove -y docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc 2>/dev/null || true

echo "Updating package list..."
sudo apt update

echo "Installing prerequisites..."
sudo apt install -y ca-certificates curl

echo "Setting up Docker GPG key..."
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "Adding Docker repository..."
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF

echo "Updating package list with Docker repository..."
sudo apt update

echo "Installing Docker..."
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "Adding current user to docker group..."
sudo usermod -aG docker $USER

echo "Docker installation complete!"
echo "Please log out and log back in for group changes to take effect."
echo "Then verify with: docker --version && docker ps"