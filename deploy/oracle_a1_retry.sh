#!/usr/bin/env bash
# oracle_a1_retry.sh — cria AUTOMATICAMENTE a VM Ampere A1 (Always Free) quando
# houver capacidade, em loop. Corre no OCI Cloud Shell (CLI já autenticado) —
# sem API key, sem AWS e SEM precisar de tocar no console (cria a própria rede:
# VCN + subnet pública + gateway + regras SSH).
#
# Uso no Cloud Shell:
#   bash <(curl -sL https://raw.githubusercontent.com/RomelNeto/testbot/main/deploy/oracle_a1_retry.sh)
#   # conservador (entra mais fácil): bash ... EU-MADRID-1-AD-1 2 12
#
# Parâmetros: [AD] [OCPU] [GB]
set -uo pipefail

AD_NAME="${1:-EU-MADRID-1-AD-1}"
OCPU="${2:-4}"
MEM_GB="${3:-24}"
INSTANCE_NAME="testbot-a1"
VCN_NAME="vcn-testbot"
SSH_PUBKEY="${SSH_PUBKEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ3hdAEzp4IA087fz9JsPlmdLcQiObX8BYxi1A4729K6 testbot-oracle}"

echo "==> A obter compartment (raiz)..."
COMPARTMENT_OCID=$(oci iam compartment list --compartment-id-in-subtree true \
  --query "data[0].id" --raw-output 2>/dev/null)
[ -z "$COMPARTMENT_OCID" ] && COMPARTMENT_OCID=$(oci iam compartment list \
  --query "data[0].id" --raw-output)
echo "    compartment: $COMPARTMENT_OCID"

# ---------------------------------------------------------------------------
# Garantir a rede: VCN + internet gateway + rota + security list (SSH) + subnet
# ---------------------------------------------------------------------------
VCN_OCID=$(oci network vcn list --compartment-id "$COMPARTMENT_OCID" \
  --query "data[?contains(\"display-name\",'$VCN_NAME')].id | [0]" --raw-output 2>/dev/null)
if [ -z "$VCN_OCID" ] || [ "$VCN_OCID" = "null" ]; then
  echo "==> A criar VCN $VCN_NAME..."
  VCN_OCID=$(oci network vcn create --compartment-id "$COMPARTMENT_OCID" \
    --cidr-block "10.0.0.0/16" --display-name "$VCN_NAME" --dns-label "testbot" \
    --query "data.id" --raw-output)
else
  echo "==> VCN existente: $VCN_OCID"
fi

IGW_OCID=$(oci network internet-gateway list --compartment-id "$COMPARTMENT_OCID" \
  --vcn-id "$VCN_OCID" --query "data[0].id" --raw-output 2>/dev/null)
if [ -z "$IGW_OCID" ] || [ "$IGW_OCID" = "null" ]; then
  echo "==> A criar internet gateway..."
  IGW_OCID=$(oci network internet-gateway create --compartment-id "$COMPARTMENT_OCID" \
    --vcn-id "$VCN_OCID" --is-enabled true --display-name "igw-testbot" \
    --query "data.id" --raw-output)
fi

# Pequeno helper: espera o recurso existir (evita o "eventual consistency"
# logo após criar a VCN, que fazia a subnet falhar com InvalidParameter).
wait_for_id() {
  local label="$1"; shift
  local val=""
  for _i in $(seq 1 12); do
    val="$("$@" 2>/dev/null)"
    [ -n "$val" ] && [ "$val" != "null" ] && { echo "$val"; return 0; }
    sleep 5
  done
  echo "ERRO: não encontrei $label na VCN $VCN_OCID" >&2
  return 1
}

ROUTE_ID=$(wait_for_id "route table" oci network route-table list \
  --compartment-id "$COMPARTMENT_OCID" --vcn-id "$VCN_OCID" \
  --query "data[0].id" --raw-output) || exit 1
echo "==> A garantir rota 0.0.0.0/0 -> internet..."
oci network route-table update --route-table-id "$ROUTE_ID" \
  --route-rules "[{\"cidrBlock\":\"0.0.0.0/0\",\"networkEntityId\":\"$IGW_OCID\"}]" \
  --force >/dev/null 2>&1 || echo "    (rota já configurada)"

SEC_LIST_ID=$(wait_for_id "security list" oci network security-list list \
  --compartment-id "$COMPARTMENT_OCID" --vcn-id "$VCN_OCID" \
  --query "data[0].id" --raw-output) || exit 1
echo "==> A garantir SSH (22) na security list..."
oci network security-list update --security-list-id "$SEC_LIST_ID" \
  --ingress-security-rules '[{"source":"0.0.0.0/0","protocol":"6","tcpOptions":{"destinationPortRange":{"min":22,"max":22}}}]' \
  --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all"}]' \
  --force >/dev/null 2>&1 || echo "    (security list já configurada)"

SUBNET_OCID=$(oci network subnet list --compartment-id "$COMPARTMENT_OCID" \
  --vcn-id "$VCN_OCID" --query "data[0].id" --raw-output 2>/dev/null)
if [ -z "$SUBNET_OCID" ] || [ "$SUBNET_OCID" = "null" ]; then
  echo "==> A criar subnet pública 10.0.0.0/24..."
  SUBNET_OCID=$(oci network subnet create --compartment-id "$COMPARTMENT_OCID" \
    --vcn-id "$VCN_OCID" --cidr-block "10.0.0.0/24" \
    --route-table-id "$ROUTE_ID" --security-list-ids "[\"$SEC_LIST_ID\"]" \
    --display-name "subnet-testbot-public" \
    --query "data.id" --raw-output 2>&1) \
    || { echo "    ERRO ao criar subnet (ver acima):"; exit 1; }
fi
echo "    subnet: $SUBNET_OCID"
if [ -z "$SUBNET_OCID" ] || [ "$SUBNET_OCID" = "null" ]; then
  echo "ERRO: subnet não criada. Vê o erro acima."
  exit 1
fi

echo "==> A aguardar subnet ficar AVAILABLE..."
for _i in $(seq 1 15); do
  ST=$(oci network subnet get --subnet-id "$SUBNET_OCID" \
    --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "")
  [ "$ST" = "AVAILABLE" ] && break
  sleep 5
done

echo "==> A procurar imagem Ubuntu 22.04 (ARM) compatível com A1..."
IMAGE_OCID=$(oci compute image list --compartment-id "$COMPARTMENT_OCID" \
  --shape VM.Standard.A1.Flex \
  --operating-system "Canonical Ubuntu" \
  --operating-system-version "22.04" \
  --query "data[0].id" --raw-output 2>/dev/null)
if [ -z "$IMAGE_OCID" ] || [ "$IMAGE_OCID" = "null" ]; then
  echo "    (--shape vazio; a tentar por nome 'aarch64'...)"
  IMAGE_OCID=$(oci compute image list --compartment-id "$COMPARTMENT_OCID" \
    --operating-system "Canonical Ubuntu" \
    --operating-system-version "22.04" \
    --query "data[?contains(ascii_downcase(\"display-name\"),'aarch64')].id | [0]" \
    --raw-output 2>/dev/null)
fi
if [ -z "$IMAGE_OCID" ] || [ "$IMAGE_OCID" = "null" ]; then
  echo "ERRO: imagem Ubuntu 22.04 ARM não encontrada. Imagens Ubuntu 22.04 disponíveis:"
  oci compute image list --compartment-id "$COMPARTMENT_OCID" \
    --operating-system "Canonical Ubuntu" \
    --operating-system-version "22.04" \
    --query "data[].[\"display-name\",\"id\"]" --raw-output 2>/dev/null | head -30
  exit 1
fi
echo "    imagem: $IMAGE_OCID"

echo "==> A verificar se já existe a instância $INSTANCE_NAME..."
EXISTS=$(oci compute instance list --compartment-id "$COMPARTMENT_OCID" \
  --availability-domain "$AD_NAME" \
  --query "data[?contains(\"display-name\",'$INSTANCE_NAME')].id | [0]" --raw-output 2>/dev/null)
if [ -n "$EXISTS" ] && [ "$EXISTS" != "null" ]; then
  echo "    já existe: $EXISTS"
  echo "    IP público:"
  oci compute instance list-vnics --compartment-id "$COMPARTMENT_OCID" \
    --instance-id "$EXISTS" --query "data[0].\"public-ip\"" --raw-output 2>/dev/null
  exit 0
fi

echo "==> A tentar criar A1 ($OCPU OCPU / $MEM_GB GB) em $AD_NAME..."
echo "    (loop a cada 3 min até conseguir — Ctrl+C para parar)"
ATTEMPT=0
while true; do
  ATTEMPT=$((ATTEMPT+1))
  echo -n "    tentativa $ATTEMPT... "
  OUT=$(oci compute instance launch \
    --availability-domain "$AD_NAME" \
    --compartment-id "$COMPARTMENT_OCID" \
    --shape VM.Standard.A1.Flex \
    --shape-config "{\"ocpus\":$OCPU,\"memoryInGBs\":$MEM_GB}" \
    --subnet-id "$SUBNET_OCID" \
    --image-id "$IMAGE_OCID" \
    --display-name "$INSTANCE_NAME" \
    --assign-public-ip true \
    --metadata "{\"ssh_authorized_keys\":\"$SSH_PUBKEY\"}" \
    2>&1) || true

  if echo "$OUT" | grep -qi "OutOfCapacity\|out of capacity\|Out of capacity"; then
    echo "sem capacidade — a aguardar 180s..."
    sleep 180
    continue
  fi

  if echo "$OUT" | grep -q '"id"'; then
    echo "🎉 CRIADA!"
    echo "$OUT" | grep -o '"id": "[^"]*"' | head -1
    echo "A aguardar IP público (30s)..."
    sleep 30
    INST_ID=$(echo "$OUT" | grep -o '"id": "[^"]*"' | head -1 | sed 's/.*"id": "\([^"]*\)".*/\1/')
    oci compute instance list-vnics --compartment-id "$COMPARTMENT_OCID" \
      --instance-id "$INST_ID" --query "data[0].\"public-ip\"" --raw-output 2>/dev/null
    echo
    echo "Agora: ssh -i ~/.ssh/id_ed25519 ubuntu@<IP>  (ou vê o IP na consola)"
    break
  fi

  echo "erro inesperado:"
  echo "$OUT"
  echo "a tentar de novo em 60s..."
  sleep 60
done
