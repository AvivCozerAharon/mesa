#!/usr/bin/env bash
# Deploy/atualizacao do mesa na EC2 (a mesma do samu-sim). Uso: bash scripts/deploy_ec2.sh <ip>
# Primeira vez: cria /opt/mesa, .env a partir do exemplo (preencher depois via ssh) e sobe o compose.
set -euo pipefail
IP="${1:?ip da ec2}"
ssh -o StrictHostKeyChecking=accept-new ec2-user@"$IP" bash -s <<'REMOTO'
set -e
if [ ! -d /opt/mesa ]; then
  sudo git clone -q https://github.com/AvivCozerAharon/mesa.git /opt/mesa
  sudo chown -R ec2-user:ec2-user /opt/mesa
fi
cd /opt/mesa && git pull -q
[ -f .env ] || { cp .env.example .env; echo ">> .env criado a partir do exemplo: preencha MESA_SENHA e OPENAI_API_KEY (ssh + nano /opt/mesa/.env) e rode: docker compose restart"; }
mkdir -p dados
sudo docker compose up --build -d
sudo docker compose ps
REMOTO
echo ">> http://$IP:8100/saude"
