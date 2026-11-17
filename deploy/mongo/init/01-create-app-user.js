// Runs once, on first start with an empty data volume, as the root user.
// Creates a least-privilege application user (readWrite on the app DB only).
const fs = require('fs');
const dbName = process.env.BITBOTPRO_MONGO_DB || 'bitbotpro';
const user = process.env.BITBOTPRO_MONGO_USER || 'bitbotpro_app';
const pwd = fs.readFileSync('/run/secrets/mongo_app_password', 'utf8').trim();
if (pwd.length < 32) { throw new Error('mongo_app_password is missing or too short'); }

db.getSiblingDB(dbName).createUser({
  user: user,
  pwd: pwd,
  roles: [{ role: 'readWrite', db: dbName }],
});
print(`[init] created user '${user}' with readWrite on '${dbName}'`);
