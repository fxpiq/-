#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo bash deploy/install.sh 运行。"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  docker.io \
  docker-compose-v2 \
  ffmpeg \
  fonts-noto-cjk \
  openssl \
  ufw

systemctl enable --now docker

mkdir -p data secrets
chown -R 10001:10001 data
chown -R 10001:10001 secrets
chmod 700 data secrets

if [[ ! -f .env ]]; then
  cp .env.example .env
  ADMIN_PASSWORD_VALUE="$(openssl rand -hex 16)"
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASSWORD_VALUE}|" .env
  chmod 600 .env
fi

if [[ ! -f /swapfile ]] && [[ "$(free -m | awk '/^Swap:/ {print $2}')" -lt 2048 ]]; then
  echo "未检测到 Swap，正在创建 4 GB Swap（语音模型运行时建议启用）。"
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

docker compose pull || true
docker compose up -d --build

echo
echo "LiveClip 已启动：http://<服务器公网IP>:8000"
echo "管理员用户名与密码保存在：${PROJECT_DIR}/.env"
echo "查看状态：docker compose ps"
echo "查看日志：docker compose logs -f --tail=200"

