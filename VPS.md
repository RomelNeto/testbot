# Migrar o testbot para um VPS (Oracle Cloud Always Free)

Objetivo: deixar de depender do GitHub Actions (atraso de 30-40 min) e seguir os
resultados quase em tempo real. No VPS o bot corre em **loop contínuo com
polling de 30s**, e continua tudo versionado no mesmo repositório GitHub
(atualiza-se com `git pull`, sem uploads manuais).

> ⚠️ Isto é um simulador **dry-run** (sem dinheiro real). O `DRY_RUN=True` está
> no `config.py` e o modo real continua **não implementado** de propósito.

---

## 1. Criar a VM na Oracle (Always Free)

1. Conta em **https://www.oracle.com/cloud/free** (pede cartão para verificação, **não cobra**).
2. Console → **Compute → Instances → Create instance**.
3. Nome: `testbot` · Imagem: **Canonical Ubuntu 22.04** (ou 24.04).
4. **Shape**: `Ampere A1` (ARM) — configure **4 OCPU / 24 GB RAM** (é o máximo
   grátis para sempre). Se der "out of capacity", muda de região ou reduz para
   2 OCPU/12 GB (já é mais que suficiente).
5. **SSH keys**: adiciona a tua chave pública `.pub` (ou deixa gerar uma `.pem`
   para descarregar — é a que usas no SSH).
6. **Create**. Anota o **IP público**.

## 2. Ligar por SSH

```bash
# Se usaste a chave .pem que a Oracle gerou:
chmod 400 sua-chave.pem
ssh -i sua-chave.pem ubuntu@<IP_PUBLICO>

# Se usaste a tua chave pública (já instalada):
ssh ubuntu@<IP_PUBLICO>
```

> O utilizador default do Ubuntu na Oracle é **`ubuntu`**.

## 3. Setup do bot (uma vez)

```bash
cd ~
curl -sL https://raw.githubusercontent.com/RomelNeto/testbot/main/setup_vps.sh -o setup_vps.sh
bash setup_vps.sh
```

O script:
- instala Python/git/tmux;
- clona `RomelNeto/testbot` para `~/testbot`;
- cria `.venv` e instala `requirements.txt`;
- faz um **bootstrap inicial** (`main.py cycle`);
- instala e liga os serviços systemd:
  - **`testbot.service`** — o bot em loop contínuo (polling 30s), com `git pull`
    a cada restart e restart automático se cair;
  - **`sync.timer`** — faz push dos dados para o GitHub a cada 10 min (dashboard);
  - **`report.timer`** — relatório diário às 08:00 UTC;
  - **`rank.timer`** — redescoberta + ranking semanal (segunda 03:00 UTC).

Confirmar que está a correr:

```bash
sudo systemctl status testbot
tail -f ~/testbot/logs/bot.log
```

## 4. Dashboard ao vivo (opcional, mas recomendado)

O dashboard (`index.html`) lê os dados do **raw do GitHub** — por isso o VPS
precisa de **enviar** os dados para lá. Configura as credenciais **uma vez** no VPS:

1. Cria um **Personal Access Token** no GitHub (Settings → Developer settings →
   Personal access tokens → Fine-grained ou classic) com permissão **Contents:
   Read and write** (clássico: scope `repo`).
2. No VPS:
   ```bash
   git config --global credential.helper store
   git config --global user.name  "testbot-vps"
   git config --global user.email "bot@vps.local"
   git remote set-url origin https://<TEU_USER>@github.com/RomelNeto/testbot.git
   cd ~/testbot && git push origin main   # pede o token; cola como password
   ```
3. A partir daí, o timer `sync` (10 min) faz commit+push dos `data/` e `reports/`
   automaticamente, e o dashboard segue ao vivo.

> 🔒 **Segurança**: o token fica guardado em `~/.git-credentials` (permissões
> 600) **fora do repositório** — nunca dentro de `~/testbot`. Nunca coloques
> chaves/tokens em ficheiros do repo.

## 5. Atualizar o código (como com git, sem upload manual)

No teu PC (onde desenvolves):

```bash
git push origin main
```

No VPS, uma de duas formas:

- **Automática (recomendado)**: `sudo systemctl restart testbot` — o
  `deploy/run_bot.sh` faz `git pull --rebase` antes de arrancar;
- **Manual**: `cd ~/testbot && git pull --rebase origin main`.

Para mudar o polling: edita `POLL_INTERVAL_SECONDS` em `deploy/testbot.service`
(30s por omissão) → `sudo systemctl restart testbot`.

## 6. ⚠️ Desativar o GitHub Actions (depois de confirmar o VPS)

O Actions ainda tem o cron `*/30` a correr o bot. Se ficarem os dois a escrever
`data/`, vão gerar conflitos e trades duplicados. **Depois** de confirmar que o
VPS está a copiar/resolver (via `tail -f logs/bot.log`):

1. GitHub → **Actions** → **polymarket-sim-bot** → **⋯ → Disable workflow**.

Se um dia quiseres voltar ao Actions, reativa-o e para o serviço no VPS:
`sudo systemctl stop testbot sync.timer report.timer rank.timer`.

## 7. Comandos úteis

```bash
sudo systemctl status testbot          # estado do bot
sudo systemctl restart testbot         # reinicia (puxa código novo)
sudo systemctl stop testbot            # para
tail -f ~/testbot/logs/bot.log         # ver o loop ao vivo
journalctl -u testbot -n 100           # logs do systemd
systemctl list-timers                  # timers ativos (sync/report/rank)
```

## 8. Migrar para dinheiro real (quando/quiseres)

- Começar com **10-20%** do capital; ter um **kill switch** (`sudo systemctl stop testbot`).
- Manter `DRY_RUN=True` até validar **50+ fechamentos** com os filtros atuais.
- O copy trading tem atraso inato — o polling de 30s minimiza, mas não elimina.
- Ordem em mercado já fechado é rejeitada pelo CLOB (não perdes); o risco real é
  comprar tarde/perto da resolução.

## 9. Troubleshooting

| Sintoma | Causa provável | Ação |
|---|---|---|
| Bot não arranca | `.venv` não criado | `cd ~/testbot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| Pull falha no restart | mudança local não commitada | `cd ~/testbot && git add -A && git commit -m "wip" && sudo systemctl restart testbot` |
| Dashboard parado | sem credenciais de push | secção 4 (token) |
| Trades duplicados | Actions + VPS ambos ativos | secção 6 (disable Actions) |
| Muitas chamadas API | polling muito baixo | sobe `POLL_INTERVAL_SECONDS` (30s é ok; 15s arrisca rate limit) |

---

Ficheiros do kit de migração (neste repo):
- `setup_vps.sh` — instalador one-shot (secção 3)
- `deploy/run_bot.sh` — pull + arranque do loop
- `deploy/testbot.service` — serviço systemd do bot
- `deploy/sync_data.sh`, `deploy/sync.{service,timer}` — dashboard ao vivo
- `deploy/report.{service,timer}` — relatório diário
- `deploy/rank.{service,timer}` — redescoberta/ranking semanal
