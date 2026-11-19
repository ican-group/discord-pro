// PROPOSED (Spec 012 Phase 6D) — NOT RUN. Creates the beta application user, restricted to bitbotpro_v12.
//
// Run ONCE, after approval, as the MongoDB root user inside bitbotpro-mongo, with the password
// read from a 0600 file mounted at /tmp/beta_pw (never on the command line, never echoed):
//   docker cp "$BITBOTPRO_SECRETS_DIR/beta_mongo_app_password" bitbotpro-mongo:/tmp/beta_pw
//   docker exec bitbotpro-mongo sh -c 'mongosh --quiet -u root -p "$(cat /run/secrets/mongo_root_password)" \
//       --authenticationDatabase admin /tmp/mongo-create-beta-user.js; rm -f /tmp/beta_pw'
// (copy this file to /tmp first with docker cp). It creates nothing else: bitbotpro_v12 itself is
// created and prepared by the beta API on its first start (prepare_database).
//
// Roles: readWrite + dbAdmin on bitbotpro_v12 ONLY (dbAdmin is needed for collMod of the schema
// validators on restart). No role on bitbotpro, admin, or any other database; rehearsed in
// deploy/rehearsal/beta-db-isolation-rehearsal.sh.
const fs = require('fs');
const pwd = fs.readFileSync('/tmp/beta_pw', 'utf8').trim();
if (pwd.length < 32) { throw new Error('beta password missing or shorter than 32 characters'); }
const db12 = db.getSiblingDB('bitbotpro_v12');
if (db12.getUser('bitbotpro_v12_app')) { throw new Error('bitbotpro_v12_app already exists; refusing'); }
db12.createUser({ user: 'bitbotpro_v12_app', pwd: pwd,
                  roles: [{ role: 'readWrite', db: 'bitbotpro_v12' }, { role: 'dbAdmin', db: 'bitbotpro_v12' }] });
print("[beta] created user 'bitbotpro_v12_app' (readWrite + dbAdmin on bitbotpro_v12 only)");
