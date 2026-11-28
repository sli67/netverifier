#!/usr/bin/env bash
# Builds Verifier's images and deploys them to the k3s cluster from main.tf.
#
# Run on the app server, in a clone of the repo:
#   DEEPSEEK_API_KEY=... OPENAI_API_KEY=... ./scripts/k3s-deploy.sh
# Keys left unset keep their current values. To also create a login:
#   ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=... ./scripts/k3s-deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

source /etc/verifier/env # S3_BUCKET, S3_REGION (written by main.tf)
NS=verifier
PUSH=localhost:5000
TAG=$(git rev-parse --short HEAD)

echo "==> Registry"
kubectl apply -f k8s/registry.yaml
kubectl -n $NS rollout status deploy/registry --timeout=300s
until curl -sf http://localhost:5000/v2/ >/dev/null; do sleep 2; done

echo "==> Building images ($TAG)"
docker build -t $PUSH/verifier-api:$TAG -f api/Dockerfile .
docker build -t $PUSH/verifier-dashboard:$TAG -f dashboard/Dockerfile .
docker build -t $PUSH/verifier-worker:$TAG worker
for image in api dashboard worker; do docker push $PUSH/verifier-$image:$TAG; done

echo "==> Secret"
existing() { kubectl -n $NS get secret verifier-env -o "jsonpath={.data.$1}" 2>/dev/null | base64 -d || true; }
DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-$(existing DEEPSEEK_API_KEY)}
OPENAI_API_KEY=${OPENAI_API_KEY:-$(existing OPENAI_API_KEY)}
param() { aws ssm get-parameter --region "$S3_REGION" --name "/verifier/$1" --with-decryption --query Parameter.Value --output text; }
DATABASE_URL=$(param database-url)
POSTGRES_PASSWORD=$(param db-password)
kubectl -n $NS create secret generic verifier-env \
  --from-literal=DATABASE_URL="$DATABASE_URL" \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  --from-literal=S3_BUCKET="$S3_BUCKET" \
  --from-literal=S3_REGION="$S3_REGION" \
  --from-literal=DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -
CONFIG=$(kubectl -n $NS get secret verifier-env -o jsonpath='{.data}' | sha256sum | cut -c1-16)

echo "==> Deploying"
sed -e "s/__TAG__/$TAG/g" -e "s/__CONFIG__/$CONFIG/g" k8s/verifier.yaml | kubectl apply -f -
for deploy in postgres api dashboard worker; do
  kubectl -n $NS rollout status deploy/$deploy --timeout=600s
done

if [ -n "${ADMIN_EMAIL:-}" ]; then
  echo "==> Creating login $ADMIN_EMAIL"
  # The password goes in on stdin, so it never shows in a process list.
  printf '%s\n' "${ADMIN_PASSWORD:?set ADMIN_PASSWORD too}" |
    kubectl -n $NS exec -i deploy/api -- node dist/scripts/create-user.js "$ADMIN_EMAIL" admin || true
fi

TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
echo "==> Done: http://$IP"
