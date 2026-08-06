#!/usr/bin/env bash
# oracle_a1_retry.sh — cria AUTOMATICAMENTE a VM Ampere A1 (Always Free) quando
# houver capacidade, em loop. Corre no OCI Cloud Shell (CLI já autenticado).
#
# PRÉ-REQUISITO (1 vez, na consola Oracle — evita os erros de rede do CLI):
#   1) Networking → Virtual cloud networks → vcn-testbot (já criada)
#   2) Dentro da VCN → Subnets → Create subnet:
#        Name: subnet-testbot-public · CIDR: 10.0.0.0/24
#        Route table: Default Route Table · Security list: Default Security List
#        Subnet access: Public subnet
#   3) Copiar o OCID da subnet (⋯ → Copy OCID)
#
# Uso no Cloud Shell:
#   export SUBNET_OCID=ocid1.subnet.oc1.eu-madrid-1.....
#   bash <(curl -sL https://raw.githubusercontent.com/RomelNeto/testbot/main/deploy/oracle_a1_retry.sh)
#   # ou tudo numa linha:
#   bash <(curl -sL https://raw.githubusercontent.com/RomelNeto/testbot/main/deploy/oracle_a1_retry.sh) EU-MADRID-1-AD-1 4 24 <SUBNET_OCID>
#
# Parâmetros: [AD] [OCPU] [GB] [SUBNET_OCID]
set -uo pipefail

AD_NAME="${1:-EU-MADRID-1-AD-1}"
OCPU="${2:-4}"
MEM_GB="${3:-24}"
SUBNET_OCID="${4:-${SUBNET_OCID:-}}"
INSTANCE_NAME="testbot-a1"
SSH_PUBKEY="${SSH_PUBKEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ3hdAEzp4IA087fz9JsPlmdLcQiObX8BYxi1A4729K6 testbot-oracle}"

if [ -z "$SUBNET_OCID" ]; then
  echo "ERRO: falta o OCID da subnet."
  echo "Cria a subnet na consola (passo a passo acima) e cola o OCID:"
  echo "  export SUBNET_OCID=ocid1.subnet...."
  echo "  bash <(curl -sL https://raw.githubusercontent.com/RomelNeto/testbot/main/deploy/oracle_a1_retry.sh)"
  exit 1
fi

echo "==> A obter compartment (raiz)..."
COMPARTMENT_OCID=$(oci iam compartment list --compartment-id-in-subtree true \
  --query "data[0].id" --raw-output 2>/dev/null)
[ -z "$COMPARTMENT_OCID" ] && COMPARTMENT_OCID=$(oci iam compartment list \
  --query "data[0].id" --raw-output)
echo "    compartment: $COMPARTMENT_OCID"

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
    --operating-system "Canonical Ubuntu" --operating-system-version "22.04" \
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
    INST_ID=$(echo "$OUT" | grep -o '"id": "[^"]*"' | head -1 | sed 's/.*"id": "\([^"]*\)".*/\1/')
    echo "    id: $INST_ID"
    echo "A aguardar IP público (30s)..."
    sleep 30
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
