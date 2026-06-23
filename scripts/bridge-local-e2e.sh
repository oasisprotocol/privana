#!/usr/bin/env bash
#
# Run the Python-driven local two-chain ROSE bridge e2e:
#   - Sapphire side: sapphire-localnet (with the bundled rofl-appd) — you start this
#   - Base side:     a local anvil chain (chainId 84532) — this script manages it
#   - deploy:        hardhat deploys + owner-wires both sides, writes a manifest
#   - drive:         pytest (test/py/test_bridge_local_e2e.py) does the onlyROFL
#                    wiring + every bridge flow through the real Python services
#
# Prereqs:
#   - sapphire-localnet running with the appd exposed (RPC :8545, appd TCP :8549).
#     The mounted /rofls must contain a rofl.yaml (the repo root has one) so the
#     localnet detects a TDX ROFL and starts the bundled appd:
#       docker run -d --rm --name sapphire-localnet-bridge --platform linux/x86_64 \
#         -p 8544-8549:8544-8549 -v "$PWD":/rofls \
#         ghcr.io/oasisprotocol/sapphire-localnet \
#         -to "chimney theory present latin find behave ankle clock shadow earn suit reflect"
#     (run from the repo root; drop --platform on non-arm64; the -to mnemonic
#      funds the hardhat.config deployer)
#   - foundry (anvil) on PATH; uv + bun installed
#
# Usage:  scripts/bridge-local-e2e.sh   (from the repo root)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SAPPHIRE_RPC="${SAPPHIRE_LOCALNET_URL:-http://127.0.0.1:8545}"
APPD_URL="${ROFL_APPD_URL:-http://127.0.0.1:8549}"
BASE_PORT="${BASE_LOCAL_PORT:-8546}"
BASE_RPC="${BASE_LOCAL_RPC_URL:-http://127.0.0.1:${BASE_PORT}}"
MANIFEST="${BRIDGE_LOCAL_MANIFEST:-$ROOT/solidity/deployments/bridge-local.json}"

rpc() { curl -s -m 3 -X POST "$1" -H 'content-type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' 2>/dev/null; }

echo "==> Checking sapphire-localnet at ${SAPPHIRE_RPC}"
if ! rpc "$SAPPHIRE_RPC" | grep -q result; then
  cat >&2 <<EOF
ERROR: sapphire-localnet is not reachable at ${SAPPHIRE_RPC}.
Start it (with the bundled appd) from the repo root first, e.g.:
  docker run -d --rm --name sapphire-localnet-bridge --platform linux/x86_64 \\
    -p 8544-8549:8544-8549 -v "\$PWD":/rofls \\
    ghcr.io/oasisprotocol/sapphire-localnet \\
    -to "chimney theory present latin find behave ankle clock shadow earn suit reflect"
Then wait ~60-90s for it to log "TDX ROFL detected" and serve :8549.
EOF
  exit 1
fi

echo "==> Checking rofl-appd at ${APPD_URL}"
if ! curl -s -m 3 "${APPD_URL}/rofl/v1/app/id" | grep -q rofl1; then
  echo "ERROR: rofl-appd not reachable at ${APPD_URL}/rofl/v1/app/id." >&2
  echo "       Ensure the localnet image bundles the appd and ports 8544-8549 are published." >&2
  exit 1
fi

ANVIL_PID=""
cleanup() { [ -n "$ANVIL_PID" ] && kill "$ANVIL_PID" 2>/dev/null || true; }

if rpc "$BASE_RPC" | grep -q result; then
  echo "==> Reusing Base node already at ${BASE_RPC}"
else
  echo "==> Starting anvil Base chain (chainId 84532) on :${BASE_PORT}"
  anvil --chain-id 84532 --port "$BASE_PORT" --base-fee 0 --silent &
  ANVIL_PID=$!
  trap cleanup EXIT
  for _ in $(seq 1 30); do rpc "$BASE_RPC" | grep -q result && break; sleep 0.5; done
  rpc "$BASE_RPC" | grep -q result || { echo "ERROR: anvil failed to start" >&2; exit 1; }
fi

# PRIVATE_KEY is cleared so hardhat uses the funded TEST_HDWALLET mnemonic
# (the one the localnet -to flag funds), not a dev .env signing key.
echo "==> Deploying + owner-wiring both sides (hardhat) -> ${MANIFEST}"
(
  cd solidity
  PRIVATE_KEY= ROFL_APPD_URL="$APPD_URL" BASE_LOCAL_RPC_URL="$BASE_RPC" \
    BRIDGE_LOCAL_MANIFEST="$MANIFEST" \
    npx hardhat run scripts/bridge-local-deploy.ts --network sapphire-localnet
)

echo "==> Running Python bridge e2e"
# Unset SAPPHIRE_VIEW_PRIVATE_KEY so the confidential reader uses the ROFL appd
# query-signer keypair (the signed-query path this e2e exists to exercise), not a
# view key that may be exported in a dev shell.
unset SAPPHIRE_VIEW_PRIVATE_KEY
SAPPHIRE_LOCALNET_URL="$SAPPHIRE_RPC" ROFL_APPD_URL="$APPD_URL" \
  BASE_LOCAL_RPC_URL="$BASE_RPC" BRIDGE_LOCAL_MANIFEST="$MANIFEST" \
  uv run pytest test/py/test_bridge_local_e2e.py -v
