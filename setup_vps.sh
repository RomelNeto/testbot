#!/usr/bin/env bash
# Setup one-shot do testbot num VPS Ubuntu 22.04 (Oracle Cloud Always Free).
# Uso:   bash setup_vps.sh
# Depois: o bot arranca como serviço systemd -> sudo systemctl status testbot
set -euo pipefail

REPO_URL="https://github.com/RomelNeto/testbot.git"
INSTALL_DIR="$HOME/testbot"
BOT_USER="${USER:-ubuntu}"

echo "==> Instalando dependências do sistema..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git tmux curl

# Swap: importante em VMs Always Free de 1 GB (ex.: E2.1.Micro / e2-micro).
# Cria um swapfile de 2G se a RAM for < 2 GB, para o Python nunca rebentar.
if [ "$(free -m | awk '/^Mem:/{print $2}')" -lt 2048 ]; then
    if [ ! -f /swapfile ]; then
        echo "==> RAM < 2 GB: a criar swap de 2G..."
        sudo fallocate -l 2G /swapfile 2>/dev/null \
            || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile >/dev/null
        sudo swapon /swapfile
        echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
        echo "  swap de 2G ativada."
    fi
fi

if [ ! -d "$INSTALL_DIR" ]; then
    echo "==> A clonar o repositório..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

echo "==> Criando ambiente Python (venv)..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> Bootstrap inicial (carteiras qualificadas + resolução de posições abertas)..."
.venv/bin/python main.py cycle || echo "  (bootstrap com avisos - verificar em seguida)"

echo "==> A dar permissão de execução aos scripts..."
chmod +x deploy/*.sh

echo "==> A instalar serviços systemd..."
mkdir -p logs
sed "s/User=ubuntu/User=$BOT_USER/g" deploy/testbot.service > /tmp/testbot.service
sudo cp /tmp/testbot.service /etc/systemd/system/testbot.service
sudo cp deploy/sync.service  deploy/sync.timer  /etc/systemd/system/
sudo cp deploy/report.service deploy/report.timer /etc/systemd/system/
sudo cp deploy/rank.service  deploy/rank.timer  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now testbot
sudo systemctl enable sync.timer report.timer rank.timer
sudo systemctl start sync.timer report.timer rank.timer 2>/dev/null || true

echo
echo "============================================================"
echo "  Pronto!"
echo "============================================================"
echo "  - Bot (loop, 30s):  sudo systemctl status testbot"
echo "  - Logs:             tail -f $INSTALL_DIR/logs/bot.log"
echo "  - Polling:          POLL_INTERVAL_SECONDS em deploy/testbot.service"
echo "                      (editar e: sudo systemctl restart testbot)"
echo
echo "  Dashboard ao vivo (opcional, precisa de token - ver VPS.md):"
echo "      configurar credenciais e o sync já está no timer de 10 min."
echo
echo "  LEMBRETE: desativa o GitHub Actions depois de confirmar que o VPS"
echo "  está a correr (Actions -> polymarket-sim-bot -> Disable workflow),"
echo "  para não rodar o bot 2x (VPS + Actions) e gerar conflitos."
echo "============================================================"
