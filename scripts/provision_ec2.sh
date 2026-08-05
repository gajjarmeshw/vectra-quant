#!/usr/bin/env bash
# Provision the SENTINEL host: EC2 t4g.small in ap-south-1 with an Elastic IP.
# Idempotent — safe to re-run; it adopts existing resources by Name tag.
#
#   ./scripts/provision_ec2.sh            # create / show
#   ./scripts/provision_ec2.sh destroy    # tear everything down
#
# Decisions: D-006 (why EC2 ap-south-1), D-012 (REST price path).
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
NAME="${SENTINEL_NAME:-sentinel}"
TYPE="${INSTANCE_TYPE:-t4g.small}"
DISK_GB="${DISK_GB:-20}"
KEY_NAME="${KEY_NAME:-${NAME}-key}"
SG_NAME="${SG_NAME:-${NAME}-sg}"
KEY_FILE="${KEY_FILE:-$HOME/.ssh/${KEY_NAME}.pem}"

aws() { command aws --region "$REGION" "$@"; }
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

require() {
  command -v aws >/dev/null || { echo "aws CLI not found"; exit 1; }
  aws sts get-caller-identity >/dev/null 2>&1 || {
    echo "AWS credentials not working. Export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
    exit 1
  }
}

instance_id() {
  aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=${NAME}" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null | grep -v '^None$' || true
}

destroy() {
  local id; id="$(instance_id)"
  if [ -n "$id" ]; then
    say "terminating $id"
    aws ec2 terminate-instances --instance-ids "$id" >/dev/null
    aws ec2 wait instance-terminated --instance-ids "$id"
  fi
  local alloc
  alloc="$(aws ec2 describe-addresses --filters "Name=tag:Name,Values=${NAME}" \
           --query 'Addresses[0].AllocationId' --output text 2>/dev/null | grep -v '^None$' || true)"
  [ -n "$alloc" ] && { say "releasing EIP"; aws ec2 release-address --allocation-id "$alloc"; }
  local sg
  sg="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${SG_NAME}" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | grep -v '^None$' || true)"
  [ -n "$sg" ] && { say "deleting SG"; aws ec2 delete-security-group --group-id "$sg" || true; }
  say "destroyed"
  exit 0
}

require
[ "${1:-}" = "destroy" ] && destroy

# ---------------------------------------------------------------- key pair
if ! aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  say "creating key pair -> $KEY_FILE"
  mkdir -p "$(dirname "$KEY_FILE")"
  aws ec2 create-key-pair --key-name "$KEY_NAME" \
    --query 'KeyMaterial' --output text > "$KEY_FILE"
  chmod 400 "$KEY_FILE"
else
  say "key pair $KEY_NAME exists"
fi

# ---------------------------------------------------------------- security group
SG_ID="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${SG_NAME}" \
         --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | grep -v '^None$' || true)"
if [ -z "$SG_ID" ]; then
  say "creating security group"
  SG_ID="$(aws ec2 create-security-group --group-name "$SG_NAME" \
           --description "SENTINEL: 22/80/443" --query 'GroupId' --output text)"
  MYIP="$(curl -fsS https://checkip.amazonaws.com || echo 0.0.0.0)"
  # SSH is restricted to the provisioning machine's current address, not the world.
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "${MYIP}/32" >/dev/null
  for p in 80 443; do
    aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --protocol tcp --port "$p" --cidr 0.0.0.0/0 >/dev/null
  done
  say "SG $SG_ID (ssh limited to ${MYIP}/32)"
else
  say "security group $SG_ID exists"
fi

# ---------------------------------------------------------------- instance
IID="$(instance_id)"
if [ -z "$IID" ]; then
  AMI="$(aws ssm get-parameters \
    --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
    --query 'Parameters[0].Value' --output text)"
  say "launching $TYPE from $AMI"
  IID="$(aws ec2 run-instances \
    --image-id "$AMI" --instance-type "$TYPE" --key-name "$KEY_NAME" \
    --security-group-ids "$SG_ID" \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=${DISK_GB},VolumeType=gp3,DeleteOnTermination=true}" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}}]" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --user-data '#!/bin/bash
set -eux
dnf update -y
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user
mkdir -p /usr/local/lib/docker/cli-plugins
ARCH=$(uname -m)
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
# AL2023 ships a buildx too old for "compose build"; install a current one.
case "$ARCH" in aarch64) BX=arm64;; x86_64) BX=amd64;; esac
BXV=$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)
curl -fsSL "https://github.com/docker/buildx/releases/download/${BXV}/buildx-${BXV}.linux-${BX}" \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
mkdir -p /opt/sentinel && chown ec2-user:ec2-user /opt/sentinel
' \
    --query 'Instances[0].InstanceId' --output text)"
  aws ec2 wait instance-running --instance-ids "$IID"
  say "instance $IID running"
else
  say "instance $IID exists"
fi

# ---------------------------------------------------------------- elastic IP
ALLOC="$(aws ec2 describe-addresses --filters "Name=tag:Name,Values=${NAME}" \
         --query 'Addresses[0].AllocationId' --output text 2>/dev/null | grep -v '^None$' || true)"
if [ -z "$ALLOC" ]; then
  say "allocating elastic IP"
  ALLOC="$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=${NAME}}]" \
    --query 'AllocationId' --output text)"
fi
aws ec2 associate-address --instance-id "$IID" --allocation-id "$ALLOC" >/dev/null
EIP="$(aws ec2 describe-addresses --allocation-ids "$ALLOC" \
       --query 'Addresses[0].PublicIp' --output text)"

cat <<EOF

================================================================
  SENTINEL host ready
================================================================
  instance   : $IID  ($TYPE, $REGION)
  elastic IP : $EIP
  ssh        : ssh -i $KEY_FILE ec2-user@$EIP
  site       : https://${EIP//./-}.sslip.io

  NEXT — two manual steps:
   1. Whitelist $EIP in Groww (Trading APIs -> Add static IP), if enforced.
   2. Deploy:  ./scripts/deploy.sh $EIP

  Cost: ~\$14/month. Free while your \$124 of credits last.
================================================================
EOF
