set -u
SP=/tmp/claude-1000/-home-ntfreem-projects-discord-pro/e94d5dc7-3bc8-4650-97f5-618cf54508f3/scratchpad
G=$SP/g6a; rm -rf $G; mkdir -p $G
cd /home/ntfreem/projects/discord-pro
IMG=bitbotpro-api-test:spec012-phase6a
IDX=$(mktemp); cp .git/index $IDX; export GIT_INDEX_FILE=$IDX; git add -A backend >/dev/null 2>&1; TREE=$(git write-tree --prefix=backend/); unset GIT_INDEX_FILE; rm -f $IDX
echo "TREE=$TREE"
git archive $TREE | docker build --progress=plain --target test -t $IMG - > $G/build.log 2>&1; echo "== build exit $?"
grep -E "^#[0-9]+ [0-9.]+ (Ran|OK|FAILED)" $G/build.log | tail -2
echo "== unit (verbose)"; docker run --rm $IMG python -m unittest discover -s tests/unit -t . -v > $G/unit.log 2>&1; grep -E "^Ran|^OK|^FAILED|^(FAIL|ERROR):" $G/unit.log
python3 - "$G/unit.log" <<'PY'
import re, sys, collections
c = collections.Counter(); s = collections.Counter(); f = collections.Counter()
for line in open(sys.argv[1]):
    m = re.match(r"^\S+ \((tests\.unit\.(\w+))\.", line)
    if not m: continue
    mod = m.group(2); c[mod] += 1
    if line.rstrip().endswith("skipped") or "skipped '" in line: s[mod] += 1
    if line.rstrip().endswith(("FAIL", "ERROR")): f[mod] += 1
groups = {"webchat": ("test_api_webchat",), "telegram": ("test_api_telegram", "test_api_telegram_delivery"),
          "discord": ("test_api_discord", "test_discord_gateway_ws"), "zendesk": ("test_api_zendesk", "test_api_zendesk_lifecycle"),
          "knowledge": ("test_knowledge", "test_knowledge_eval", "test_semantic_verifier", "test_knowledge_harness", "test_core_knowledge", "test_core_knowledge_approval"),
          "deletion": ("test_core_deletion", "test_api_deletion", "test_deletion_safety"), "phase6a": ("test_api_phase6a",),
          "matrix": ("test_api_matrix",)}
for g, mods in groups.items():
    print(f"   {g}: run={sum(c[m] for m in mods)} skipped={sum(s[m] for m in mods)} failed={sum(f[m] for m in mods)}")
PY
echo "== security 009-011"; docker run --rm $IMG python -m unittest tests.unit.test_auth_security tests.unit.test_oauth_webhook_auth tests.unit.test_outbound_guard tests.unit.test_outbound_routes tests.unit.test_tenant_scope tests.unit.test_v2_tenant_isolation tests.unit.test_security_utils tests.unit.test_security_headers tests.unit.test_rate_limit tests.unit.test_log_redaction tests.unit.test_client_ip tests.unit.test_public_routes tests.unit.test_public_urls tests.unit.test_password_policy 2>&1 | tail -3
echo "== matrix"; docker run --rm $IMG python -c "
from tests.unit.test_api_matrix import cases, ACTORS
print('matrix cases', len(cases()), 'requests', len(cases())*len(ACTORS))"
echo "== knowledge harness (scripted; plumbing only)"; for m in "deterministic corpus" "deterministic holdout" "verifier holdout" "end_to_end holdout"; do set -- $m; docker run --rm -u $(id -u):$(id -g) -v $G:/out $IMG python -m tests.knowledge_eval.harness --mode $1 --set $2 --out /out/harness_$1_$2.json > /dev/null 2>&1; echo "   $1 $2 exit $?"; done
echo "== contract drift"; mkdir -p $G/contract; docker run --rm -u $(id -u):$(id -g) -v $G/contract:/out $IMG python -m tests.contract.export_frontend_contract /out | tail -1
diff -q $G/contract/routes.json frontend/src/app12/contract/routes.json && echo "routes.json: no drift"
docker network create --internal bbp-s012-itest >/dev/null
docker run -d --name bbp-s012-itest-mongo --network bbp-s012-itest --ulimit nofile=64000:64000 --tmpfs /data/db:rw,size=2g --memory 1g mongo:8.0.38-noble mongod --bind_ip_all --wiredTigerCacheSizeGB 0.25 >/dev/null
until docker exec bbp-s012-itest-mongo mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do :; done
for i in 1 2 3; do echo "== mongo run $i"; docker run --rm --network bbp-s012-itest -e MONGO_TEST_URL=mongodb://bbp-s012-itest-mongo:27017 -e MONGO_TEST_DB=bbp_itest $IMG python -m unittest discover -s tests/integration -t . 2>&1 | grep -E '^Ran|^OK|^FAILED|^(FAIL|ERROR):'; done
echo "== load"; docker run --rm --network bbp-s012-itest -e MONGO_TEST_URL=mongodb://bbp-s012-itest-mongo:27017 -e LOAD_TEST=1 -e LOAD_TEST_REPORT=/out/load.json -u $(id -u):$(id -g) -v $G:/out $IMG python -m unittest tests.integration.test_spec012_load 2>&1 | grep -E '^Ran|^OK|^FAILED|^(FAIL|ERROR):'
docker rm -f bbp-s012-itest-mongo >/dev/null; docker network rm bbp-s012-itest >/dev/null
echo "== frontend jest"; (cd frontend && docker run --rm -v "$PWD":/app -w /app -u $(id -u):$(id -g) -e HOME=/tmp -e CI=true node:20.18.1-bookworm-slim sh -c "yarn test --watchAll=false 2>&1" > $G/jest.log); grep -E "^Tests:|^Test Suites:" $G/jest.log
echo "== frontend build (production API origin)"; (cd frontend && docker run --rm -v "$PWD":/app -w /app -u $(id -u):$(id -g) -e HOME=/tmp -e GENERATE_SOURCEMAP=false -e REACT_APP_API_BASE_URL=https://api.bitbotpro.io node:20.18.1-bookworm-slim sh -c "yarn build 2>&1" > $G/build_fe.log); grep -E "Compiled|Failed to compile" $G/build_fe.log; echo "   app12 warnings: $(grep -c 'src/app12' $G/build_fe.log)"
echo "== e2e frontend build (same origin)"; (cd frontend && docker run --rm -v "$PWD":/app -w /app -u $(id -u):$(id -g) -e HOME=/tmp -e GENERATE_SOURCEMAP=false -e BUILD_PATH=build-e2e -e REACT_APP_API_BASE_URL= node:20.18.1-bookworm-slim sh -c "yarn build 2>&1" | grep -E "Compiled|Failed to compile")
start_e2e() {
  docker network create --internal bbp-e2e >/dev/null
  docker run -d --name bbp-e2e-mongo --network bbp-e2e --ulimit nofile=64000:64000 --tmpfs /data/db:rw,size=1g --memory 1g mongo:8.0.38-noble mongod --bind_ip_all --wiredTigerCacheSizeGB 0.25 >/dev/null
  until docker exec bbp-e2e-mongo mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do :; done
  docker run -d --name bbp-e2e-app --network bbp-e2e -e MONGO_URL=mongodb://bbp-e2e-mongo:27017 -e DB_NAME=bbp_e2e -e E2E_ORIGIN=http://bbp-e2e-app:8080 -e FRONTEND_BUILD=/frontend -v "$PWD/frontend/build-e2e":/frontend:ro $IMG python -m tests.e2e.server >/dev/null
  for i in $(seq 1 90); do docker run --rm --network bbp-e2e curlimages/curl:8.11.1 -s -o /dev/null -w "%{http_code}" http://bbp-e2e-app:8080/api/auth/session 2>/dev/null | grep -q 401 && break; done
}
stop_e2e() { docker logs bbp-e2e-app > $G/e2e_server_$1.log 2>&1; docker rm -f bbp-e2e-app bbp-e2e-mongo >/dev/null; docker network rm bbp-e2e >/dev/null; }
echo "== e2e HTTP journeys (real frontend/backend boundary)"; start_e2e
docker run --rm --network bbp-e2e -e E2E_BASE_URL=http://bbp-e2e-app:8080 $IMG python -m unittest tests.e2e.test_e2e_http -v > $G/e2e_http.log 2>&1; grep -E "^Ran|^OK|^FAILED|^(FAIL|ERROR):" $G/e2e_http.log
stop_e2e http
echo "== e2e browser (Playwright, Chromium)"; start_e2e
docker run --rm --network bbp-e2e --ipc=host -v "$PWD/e2e":/e2e -w /e2e -u $(id -u):$(id -g) -e HOME=/tmp -e E2E_BASE_URL=http://bbp-e2e-app:8080 mcr.microsoft.com/playwright:v1.49.1-noble npx playwright test > $G/playwright.log 2>&1; grep -E "passed|failed|skipped|flaky|✘" $G/playwright.log
echo "   server worker errors: $(docker logs bbp-e2e-app 2>&1 | grep -c 'worker error')"
stop_e2e browser
echo "== cleaned: $(docker ps -a --format '{{.Names}}' | grep -cE 'itest|e2e')"
echo "== production"; docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep -E "^bitbotpro-api |gridscan" | head
