#!/usr/bin/env bash
# Instance bootstrap. Installs Docker and prepares /opt/app, then stops --
# the application itself is shipped by deploy.sh so that redeploying does not
# mean recycling the instance (and losing its Elastic IP association timing).
#
# Progress is left in /var/log/user-data.log and a marker file that deploy.sh
# waits on, so a deploy that races the bootstrap fails loudly instead of oddly.
set -euxo pipefail

exec > >(tee -a /var/log/user-data.log) 2>&1

export DEBIAN_FRONTEND=noninteractive

# cloud-init can still hold the apt lock this early on.
for _ in $(seq 1 30); do
  if apt-get update -y; then break; fi
  sleep 5
done

apt-get install -y ca-certificates curl gnupg

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker
usermod -aG docker ubuntu

# Spark writes a lot of small files and the JVM likes plenty of maps.
cat > /etc/sysctl.d/99-app.conf <<'SYSCTL'
vm.max_map_count = 262144
vm.swappiness = 10
SYSCTL
sysctl --system

# A little swap. 8 GB is enough for the stack, but swap turns a transient spike
# during an image build into a slowdown instead of an OOM kill.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

install -d -o ubuntu -g ubuntu /opt/app

touch /var/lib/cloud-bootstrap-complete
echo "bootstrap complete"
