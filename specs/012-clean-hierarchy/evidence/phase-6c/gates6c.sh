set -u
SP=/tmp/claude-1000/-home-ntfreem-projects-discord-pro/e94d5dc7-3bc8-4650-97f5-618cf54508f3/scratchpad
G=$SP/g6c-$(date +%s); mkdir -p $G; echo "LOGS=$G"
cd /home/ntfreem/projects/discord-pro
IDX=$(mktemp); cp .git/index $IDX; export GIT_INDEX_FILE=$IDX; git add -A backend >/dev/null 2>&1; TREE=$(git write-tree --prefix=backend/); git add -A frontend e2e >/dev/null 2>&1; FTREE=$(git write-tree --prefix=frontend/); unset GIT_INDEX_FILE; rm -f $IDX
T12=${TREE:0:12}
IMG=bitbotpro-api-test:spec012-beta1-$T12
RC=bitbotpro-api:spec012-beta1-$T12
echo "TREE=$TREE"; echo "FRONTEND_TREE=$FTREE"; echo "LOCKS: requirements.txt=$(sha256sum backend/requirements.txt | cut -c1-16) yarn.lock=$(sha256sum frontend/yarn.lock | cut -c1-16) e2e/package-lock.json=$(sha256sum e2e/package-lock.json | cut -c1-16)"; echo "BASE=$(grep -oE "python:[^ ]+@sha256:[0-9a-f]+" backend/Dockerfile)"; echo "TEST_IMAGE=$IMG"; echo "RC_IMAGE=$RC"
git archive $TREE > $G/backend-src.tar
docker build --progress=plain --target test -t $IMG - < $G/backend-src.tar > $G/build_test.log 2>&1; echo "== test image build exit $?"
grep -E "^#[0-9]+ [0-9.]+ (Ran|OK|FAILED)" $G/build_test.log | tail -2
docker build --progress=plain --target runtime --build-arg SOURCE_TREE=$TREE -t $RC - < $G/backend-src.tar > $G/build_rc.log 2>&1; echo "== RC image build exit $?"
docker image inspect $IMG --format 'test image id={{.Id}}' ; docker image inspect $RC --format 'rc image id={{.Id}} size={{.Size}}'

echo "== unit (verbose, in the test image)"; docker run --rm $IMG python -m unittest discover -s tests/unit -t . -v > $G/unit.log 2>&1; grep -E "^Ran|^OK|^FAILED|^(FAIL|ERROR):" $G/unit.log
python3 - "$G/unit.log" <<'PY'
import re, sys, collections
c = collections.Counter(); s = collections.Counter(); f = collections.Counter(); skipmods = collections.Counter()
for line in open(sys.argv[1]):
    m = re.match(r"^\S+ \((tests\.unit\.(\w+))\.", line)
    if not m: continue
    mod = m.group(2); c[mod] += 1
    if "skipped" in line.rsplit("...", 1)[-1]: s[mod] += 1; skipmods[mod] += 1
    if line.rstrip().endswith(("FAIL", "ERROR")): f[mod] += 1
groups = {"webchat": ("test_api_webchat", "test_api_webchat_billing"), "telegram": ("test_api_telegram", "test_api_telegram_delivery"),
          "discord": ("test_api_discord", "test_discord_gateway_ws"), "zendesk": ("test_api_zendesk", "test_api_zendesk_lifecycle"),
          "knowledge": ("test_knowledge", "test_knowledge_eval", "test_semantic_verifier", "test_knowledge_harness", "test_core_knowledge", "test_core_knowledge_approval"),
          "deletion": ("test_core_deletion", "test_api_deletion", "test_deletion_safety"), "phase6a": ("test_api_phase6a",), "registration_b_acct": ("test_api_registration",), "auth_members": ("test_api_auth", "test_api_members"), "usage_accounting": ("test_api_webchat_billing", "test_api_phase6a"),
          "key_rotation": ("test_key_rotation",), "production_wiring": ("test_production_app",), "matrix": ("test_api_matrix",),
          "kept_security_009_011": ("test_auth_security", "test_oauth_webhook_auth", "test_outbound_guard", "test_security_utils", "test_security_headers", "test_rate_limit", "test_log_redaction", "test_client_ip", "test_public_urls", "test_password_policy", "test_api_integrations_ssrf", "test_chat_sessions", "test_email_delivery", "test_runtime_config", "test_entrypoint"),
          "spec012_security": ("test_api_security", "test_api_security_4f", "test_api_ratelimits", "test_core_isolation", "test_api_auth", "test_core_architecture")}
for g, mods in groups.items():
    print(f"   {g}: run={sum(c[m] for m in mods)} skipped={sum(s[m] for m in mods)} failed={sum(f[m] for m in mods)}")
print("   skipped by module:", dict(skipmods))
PY

echo "== matrix"; docker run --rm $IMG python -c "
from tests.unit.test_api_matrix import cases, ACTORS
print('matrix routes', len(cases()), 'requests', len(cases())*len(ACTORS))"
echo "== knowledge harness (scripted; plumbing only)"; for m in "deterministic corpus" "deterministic holdout" "verifier holdout" "end_to_end holdout"; do set -- $m; docker run --rm -u $(id -u):$(id -g) -v $G:/out $IMG python -m tests.knowledge_eval.harness --mode $1 --set $2 --out /out/harness_$1_$2.json > /dev/null 2>&1; echo "   $1 $2 exit $?"; done
echo "== contract drift"; mkdir -p $G/contract; docker run --rm -u $(id -u):$(id -g) -v $G/contract:/out $IMG python -m tests.contract.export_frontend_contract /out | tail -1
diff -q $G/contract/routes.json frontend/src/app12/contract/routes.json && echo "routes.json: no drift"

docker network create --internal bbp-s012-itest >/dev/null
docker run -d --name bbp-s012-itest-mongo --network bbp-s012-itest --ulimit nofile=64000:64000 --tmpfs /data/db:rw,size=2g --memory 1g mongo:8.0.38-noble mongod --bind_ip_all --wiredTigerCacheSizeGB 0.25 >/dev/null
until docker exec bbp-s012-itest-mongo mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do :; done
for i in 1 2 3; do echo "== mongo run $i"; docker run --rm --network bbp-s012-itest -e MONGO_TEST_URL=mongodb://bbp-s012-itest-mongo:27017 -e MONGO_TEST_DB=bbp_itest $IMG python -m unittest discover -s tests/integration -t . -v > $G/mongo_run$i.log 2>&1; grep -E '^Ran|^OK|^FAILED|^(FAIL|ERROR):' $G/mongo_run$i.log; done
echo "== load"; docker run --rm --network bbp-s012-itest -e MONGO_TEST_URL=mongodb://bbp-s012-itest-mongo:27017 -e LOAD_TEST=1 -e LOAD_TEST_REPORT=/out/load.json -u $(id -u):$(id -g) -v $G:/out $IMG python -m unittest tests.integration.test_spec012_load 2>&1 | grep -E '^Ran|^OK|^FAILED|^(FAIL|ERROR):'
docker rm -f bbp-s012-itest-mongo >/dev/null; docker network rm bbp-s012-itest >/dev/null

echo "== rehearsal: MongoDB open-file limit"; bash deploy/rehearsal/mongo-nofile-rehearsal.sh $IMG $G/rehearsal-nofile > /dev/null 2>&1; echo "   exit $?"; cat $G/rehearsal-nofile/mongo-nofile-rehearsal.txt | sed 's/^/   /'
echo "== rehearsal: backup and restore"; bash deploy/rehearsal/backup-restore-rehearsal.sh $IMG $G/rehearsal-backup > /dev/null 2>&1; echo "   exit $?"
grep -E "^==|archive:|gzip|dryRun|negative|restored into|\"ok\"|failures" $G/rehearsal-backup/backup-restore-rehearsal.txt | sed 's/^/   /'

echo "== RC image checks"
docker image inspect $RC --format 'user={{.Config.User}} healthcheck={{json .Config.Healthcheck.Test}} labels={{json .Config.Labels}}'
docker image inspect $RC --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -iE "secret|password|token|key=" | grep -v '^GPG_KEY=' && echo "   SECRET-LIKE ENV FOUND" || echo "   env: no secret-like variables (GPG_KEY = public signing-key fingerprint of the python base image)"
docker history --no-trunc $RC --format '{{.CreatedBy}}' | sed 's/password_policy\.py//g' | grep -iE "secret|password|api_key|token=" && echo "   SECRET-LIKE HISTORY" || echo "   history: no secret-like build steps"
docker run --rm --entrypoint sh $RC -c 'ls /app; test ! -e /app/api_integrations.py && test ! -e /app/manage.py && test ! -d /app/tests && echo "   retained legacy sources, manage.py and tests are NOT in the image"'
docker run --rm --entrypoint python $RC -c "import importlib.metadata as m; print('   python packages in image:', len(list(m.distributions())))"
docker network create --internal bbp-6b-img >/dev/null
docker run -d --name bbp-6b-img-mongo --network bbp-6b-img --tmpfs /data/db:rw,size=1g --memory 1g mongo:8.0.38-noble mongod --bind_ip_all --wiredTigerCacheSizeGB 0.25 >/dev/null
until docker exec bbp-6b-img-mongo mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1; do :; done
KEY=$(docker run --rm --entrypoint python $RC -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
JWT=$(head -c 48 /dev/urandom | base64 | tr -d '\n/+=')
COMMON="--network bbp-6b-img --read-only --tmpfs /tmp:size=64m --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 256 --memory 1g -e APP_ENV=production -e MONGO_URL=mongodb://bbp-6b-img-mongo:27017 -e DB_NAME=bbp_rc_smoke -e JWT_SECRET=$JWT -e ENCRYPTION_KEY=$KEY -e PUBLIC_API_URL=https://api.bitbotpro.io -e PUBLIC_APP_URL=https://bitbotpro.io -e CORS_ORIGINS=https://bitbotpro.io -e ALLOWED_HOSTS=bbp-6b-img-api,127.0.0.1,localhost"
docker run -d --name bbp-6b-img-api $COMMON -e APP_MODE=api $RC >/dev/null
docker run -d --name bbp-6b-img-worker $COMMON -e APP_MODE=worker $RC python worker.py >/dev/null
for i in $(seq 1 60); do s=$(docker inspect bbp-6b-img-api --format '{{.State.Health.Status}}'); [ "$s" = healthy ] && break; sleep 2; done; echo "   api container: $(docker inspect bbp-6b-img-api --format '{{.State.Health.Status}} user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}')"
for i in $(seq 1 60); do s=$(docker inspect bbp-6b-img-worker --format '{{.State.Health.Status}}'); [ "$s" = healthy ] && break; sleep 2; done; echo "   worker container: $(docker inspect bbp-6b-img-worker --format '{{.State.Health.Status}}')"
C="docker run --rm --network bbp-6b-img curlimages/curl:8.11.1 -s -o /dev/null -w %{http_code}"
echo "   GET /api/health=$($C http://bbp-6b-img-api:8000/api/health) /api/ready=$($C http://bbp-6b-img-api:8000/api/ready) foreign Host=$($C -H 'Host: evil.example' http://bbp-6b-img-api:8000/api/health) /api/enterprises (no session)=$($C http://bbp-6b-img-api:8000/api/enterprises) /docs=$($C http://bbp-6b-img-api:8000/docs)"
docker logs bbp-6b-img-api > $G/rc_api.log 2>&1; docker logs bbp-6b-img-worker > $G/rc_worker.log 2>&1
grep -cE "$JWT|$KEY" $G/rc_api.log $G/rc_worker.log | sed 's/^/   secret occurrences in logs: /'
docker exec bbp-6b-img-mongo mongosh --quiet --eval 'db.getSiblingDB("bbp_rc_legacy").workspaces.insertOne({name:"legacy"})' >/dev/null
docker run -d --name bbp-6b-img-legacy $COMMON -e APP_MODE=api -e DB_NAME=bbp_rc_legacy $RC >/dev/null; sleep 12
echo "   legacy database: container $(docker inspect bbp-6b-img-legacy --format '{{.State.Status}} exit={{.State.ExitCode}}'); $(docker logs bbp-6b-img-legacy 2>&1 | grep -ciE 'legacy \(pre-Spec 012\) database') refusal line(s); workspaces untouched=$(docker exec bbp-6b-img-mongo mongosh --quiet --eval 'db.getSiblingDB("bbp_rc_legacy").getCollectionNames().join(",")')"
docker run --rm $COMMON -e APP_MODE=api -e JWT_SECRET=short $RC > $G/rc_badconfig.log 2>&1; echo "   bad configuration: exit $? ($(cat $G/rc_badconfig.log | head -1))"
docker rm -f bbp-6b-img-api bbp-6b-img-worker bbp-6b-img-legacy bbp-6b-img-mongo >/dev/null; docker network rm bbp-6b-img >/dev/null

echo "== RC image vulnerability scan (offline tarball, Trivy)"
docker save $RC -o $G/rc-image.tar
docker run --rm -v $G:/scan:ro -v $SP/trivy-cache:/root/.cache aquasec/trivy:0.58.1 image --quiet --input /scan/rc-image.tar --scanners vuln --format json --output /dev/stdout > $G/trivy.json 2> $G/trivy.err; echo "   trivy exit $?"
python3 - "$G/trivy.json" <<'PY'
import json, sys, collections
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("   no scan result:", type(e).__name__); sys.exit(0)
c = collections.Counter(); rows = []
for r in d.get("Results", []):
    for v in r.get("Vulnerabilities") or []:
        c[v["Severity"]] += 1
        if v["Severity"] in ("CRITICAL", "HIGH"):
            rows.append((v["Severity"], v.get("PkgName"), v.get("InstalledVersion"), v.get("FixedVersion") or "-", v["VulnerabilityID"], r.get("Type")))
print("   by severity:", dict(c))
for row in sorted(set(rows)): print("   ", *row)
PY
rm -f $G/rc-image.tar; python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print('   zlib1g findings:', [ (v['VulnerabilityID'],v['InstalledVersion']) for r in d.get('Results',[]) for v in (r.get('Vulnerabilities') or []) if v.get('PkgName')=='zlib1g'])" $G/trivy.json

echo "== frontend jest"; (cd frontend && docker run --rm -v "$PWD":/app -w /app -u $(id -u):$(id -g) -e HOME=/tmp -e CI=true node:20.18.1-bookworm-slim sh -c "yarn test --watchAll=false 2>&1" > $G/jest.log); grep -E "^Tests:|^Test Suites:" $G/jest.log
echo "== frontend build (production API origin, CI=true)"; (cd frontend && docker run --rm -v "$PWD":/app -w /app -u $(id -u):$(id -g) -e HOME=/tmp -e CI=true -e GENERATE_SOURCEMAP=false -e REACT_APP_API_BASE_URL=https://api.bitbotpro.io node:20.18.1-bookworm-slim sh -c "yarn build 2>&1" > $G/build_fe.log); grep -E "Compiled|Failed to compile" $G/build_fe.log; echo "   warnings: $(grep -ciE '^\s*Line [0-9]+' $G/build_fe.log)"
echo "== e2e frontend build (same origin)"; (cd frontend && docker run --rm -v "$PWD":/app -w /app -u $(id -u):$(id -g) -e HOME=/tmp -e CI=true -e GENERATE_SOURCEMAP=false -e BUILD_PATH=build-e2e -e REACT_APP_API_BASE_URL= node:20.18.1-bookworm-slim sh -c "yarn build 2>&1" | grep -E "Compiled|Failed to compile")
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
echo "== cleaned: $(docker ps -a --format '{{.Names}}' | grep -cE 'itest|e2e|bbp-6b')"
echo "== production"; docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep -E "^bitbotpro-|gridscan" | head
