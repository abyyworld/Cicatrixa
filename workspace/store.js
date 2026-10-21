// The workspace's tables in the accounts' Turso database, and every query on them.
// Shapes follow the platform's tables (settings, github_connections, projects,
// services) closely enough that `python -m app.import_online` copies them 1:1.
//
// libSQL enforces foreign keys only with PRAGMA foreign_keys=ON, which a remote
// connection does not keep, so every delete below is spelled out.
import { decryptSetting, encryptSetting } from "./secrets.js";

// accounts/auth.js writes a password proof here (recordPasswordProof), possibly before
// the workspace's function has ever run, so it creates this one table itself.
export const SETTING_TABLE = `create table if not exists cx_setting (key text primary key, value text not null, updatedAt text not null)`;
// cx_setting key of an account's password proof: pw_proof:<userId>, value an ISO time.
export const PROOF_PREFIX = "pw_proof:";

export const WORKSPACE_SCHEMA = [
  SETTING_TABLE,
  // One installation is bound to one account, ever (unique), and an account has one.
  `create table if not exists cx_github (userId text not null primary key references "user"("id") on delete cascade, installationId integer not null unique, accountLogin text not null, accountType text not null, provedBy text not null, createdAt text not null)`,
  `create table if not exists cx_project (id text primary key, userId text not null references "user"("id") on delete cascade, name text not null, slug text not null unique, createdAt text not null)`,
  `create index if not exists cx_project_user on cx_project(userId)`,
  `create table if not exists cx_service (id text primary key, projectId text not null references cx_project(id) on delete cascade, name text not null, slug text not null unique, repoFull text not null, branch text not null, isPrimary integer not null default 0, status text not null default 'queued', sha text, plan text, log text, error text, updatedAt text not null, createdAt text not null)`,
  `create index if not exists cx_service_project on cx_service(projectId)`,
  `create table if not exists cx_analysis (id text primary key, userId text not null, at text not null)`,
  `create index if not exists cx_analysis_user on cx_analysis(userId, at)`,
];

export async function ensureWorkspaceSchema(client) {
  await client.batch(WORKSPACE_SCHEMA, "write");
}

const iso = (ms = Date.now()) => new Date(ms).toISOString();

function plain(rs) {
  return rs.rows.map((row) => Object.fromEntries(rs.columns.map((c, i) => [c, row[i]])));
}

async function all(client, sql, args = []) {
  return plain(await client.execute({ sql, args }));
}

async function one(client, sql, args = []) {
  return (await all(client, sql, args))[0] || null;
}

// ---------- settings ----------

// Same names as the platform's settings keys; these three are stored encrypted.
export const SECRET_SETTINGS = ["gh_app_pem", "gh_app_webhook_secret", "gh_app_client_secret"];
export const APP_SETTINGS = ["gh_app_id", "gh_app_slug", "gh_app_name", "gh_app_client_id",
                             ...SECRET_SETTINGS];

// The App's settings. Password proofs share the table (one row per account that ever
// reset its password) but are no setting: they are read one at a time (passwordProof).
export async function getSettings(client, secret) {
  const out = {};
  for (const { key, value } of await all(client, "select key, value from cx_setting " +
                                                 `where substr(key, 1, ${PROOF_PREFIX.length}) <> ?`,
                                         [PROOF_PREFIX])) {
    out[key] = decryptSetting(secret, value);
  }
  return out;
}

// Stores a new App's credentials in one transaction. gh_app_id is a plain insert, so if
// an App is already stored the whole batch fails and nothing is overwritten: a second
// App would strand every installation made on the first.
export async function saveAppSettings(client, secret, values, now = Date.now()) {
  const at = iso(now);
  const stmts = [{ sql: "insert into cx_setting (key, value, updatedAt) values ('gh_app_id', ?, ?)",
                   args: [String(values.gh_app_id), at] }];
  for (const key of APP_SETTINGS) {
    if (key === "gh_app_id") continue;
    const v = String(values[key] ?? "");
    stmts.push({
      sql: "insert into cx_setting (key, value, updatedAt) values (?, ?, ?) " +
           "on conflict(key) do update set value = excluded.value, updatedAt = excluded.updatedAt",
      args: [key, SECRET_SETTINGS.includes(key) && v ? encryptSetting(secret, v) : v, at],
    });
  }
  await client.batch(stmts, "write");
}

// ---------- password proofs (who may be an admin) ----------

// A row says the account's password was last set through an emailed reset link, so the
// person who holds the password also reads the inbox. A confirmation link proves less:
// whoever signed up chose the password, and anyone who reads the inbox can click it.
// `python -m app.import_online` reads the same rows to decide the platform's is_admin.
export async function recordPasswordProof(client, userId, now = Date.now()) {
  const at = iso(now);
  await client.batch([
    SETTING_TABLE,
    { sql: "insert into cx_setting (key, value, updatedAt) values (?, ?, ?) " +
           "on conflict(key) do update set value = excluded.value, updatedAt = excluded.updatedAt",
      args: [PROOF_PREFIX + userId, at, at] },
  ], "write");
}

// The time of the proof (ISO), or null.
export async function passwordProof(client, userId) {
  const row = await one(client, "select value from cx_setting where key = ?",
                        [PROOF_PREFIX + userId]);
  return row ? row.value : null;
}

// ---------- GitHub connections ----------

export function getConnection(client, userId) {
  return one(client, "select * from cx_github where userId = ?", [userId]);
}

export async function installationOwner(client, installationId) {
  const row = await one(client, "select userId from cx_github where installationId = ?",
                        [Number(installationId)]);
  return row ? row.userId : null;
}

// Replaces the account's single binding. The unique installationId makes a race with
// another account fail here instead of binding one installation twice.
export async function bindInstallation(client, { userId, installationId, accountLogin,
                                                 accountType, provedBy }, now = Date.now()) {
  await client.batch([
    { sql: "delete from cx_github where userId = ?", args: [userId] },
    { sql: "insert into cx_github (userId, installationId, accountLogin, accountType, provedBy, " +
           "createdAt) values (?, ?, ?, ?, ?, ?)",
      args: [userId, Number(installationId), accountLogin || "", accountType || "",
             provedBy || "", iso(now)] },
  ], "write");
}

export async function deleteConnection(client, userId) {
  await client.execute({ sql: "delete from cx_github where userId = ?", args: [userId] });
}

export async function deleteInstallation(client, installationId) {
  const rs = await client.execute({ sql: "delete from cx_github where installationId = ?",
                                    args: [Number(installationId)] });
  return rs.rowsAffected;
}

// ---------- projects and services ----------

export async function slugTaken(client, slug) {
  return Boolean(await one(client,
    "select 1 as x from cx_project where slug = ? union all select 1 from cx_service where slug = ?",
    [slug, slug]));
}

export async function countUserServices(client, userId) {
  const row = await one(client, "select count(*) as c from cx_service s join cx_project p " +
                                "on p.id = s.projectId where p.userId = ?", [userId]);
  return Number(row.c);
}

// project: {id, userId, name, slug}; services: [{id, name, slug, repoFull, branch, isPrimary}]
// The cap is checked inside the write: the caller's own count is stale by the time GitHub
// has answered, and requests sent side by side all pass it. A write transaction
// runs alone, so the project goes in only if the account's services plus these fit
// under maxServices, and each service only if its project went in. false: over the cap,
// nothing written.
export async function insertProject(client, project, services,
                                    { maxServices = Number.MAX_SAFE_INTEGER, now = Date.now() } = {}) {
  const at = iso(now);
  const rs = await client.batch([
    { sql: "insert into cx_project (id, userId, name, slug, createdAt) select ?, ?, ?, ?, ? " +
           "where (select count(*) from cx_service s join cx_project p on p.id = s.projectId " +
           "where p.userId = ?) + ? <= ?",
      args: [project.id, project.userId, project.name, project.slug, at, project.userId,
             services.length, maxServices] },
    ...services.map((s) => ({
      sql: "insert into cx_service (id, projectId, name, slug, repoFull, branch, isPrimary, " +
           "status, updatedAt, createdAt) select ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ? " +
           "where exists (select 1 from cx_project where id = ?)",
      args: [s.id, project.id, s.name, s.slug, s.repoFull, s.branch, s.isPrimary ? 1 : 0, at, at,
             project.id],
    })),
  ], "write");
  return rs[0].rowsAffected === 1;
}

export async function listProjects(client, userId) {
  // rowid keeps the order things were added in (ids are random).
  const projects = await all(client, "select * from cx_project where userId = ? " +
                                     "order by createdAt desc, rowid desc", [userId]);
  const services = await all(client, "select s.* from cx_service s join cx_project p " +
                                     "on p.id = s.projectId where p.userId = ? " +
                                     "order by s.isPrimary desc, s.createdAt, s.rowid", [userId]);
  return projects.map((p) => ({ ...p, services: services.filter((s) => s.projectId === p.id) }));
}

export async function getProject(client, userId, projectId) {
  const project = await one(client, "select * from cx_project where id = ? and userId = ?",
                            [projectId, userId]);
  if (!project) return null;
  project.services = await all(client, "select * from cx_service where projectId = ? " +
                                       "order by isPrimary desc, createdAt, rowid", [projectId]);
  return project;
}

export async function deleteProject(client, userId, projectId) {
  const rs = await client.batch([
    { sql: "delete from cx_service where projectId in " +
           "(select id from cx_project where id = ? and userId = ?)", args: [projectId, userId] },
    { sql: "delete from cx_project where id = ? and userId = ?", args: [projectId, userId] },
  ], "write");
  return rs[1].rowsAffected > 0;
}

// ---------- analyses ----------

// Marks a service as being analysed, unless another analysis of it began less than
// staleMs ago. The start time is updatedAt, which nothing touches until the end, so a
// function killed mid-run leaves a service that may be re-run once staleMs has passed.
export async function claimService(client, serviceId, now = Date.now(), staleMs = 90_000) {
  const rs = await client.execute({
    sql: "update cx_service set status = 'analyzing', updatedAt = ?, log = '', error = null, " +
         "sha = null, plan = null where id = ? and (status <> 'analyzing' or updatedAt <= ?)",
    args: [iso(now), serviceId, iso(now - staleMs)],
  });
  return rs.rowsAffected === 1;
}

// Records one analysis against the per-user cap, atomically: false once `max` were
// recorded in the last 24 h.
export async function recordAnalysis(client, { id, userId }, max, now = Date.now()) {
  const rs = await client.execute({
    sql: "insert into cx_analysis (id, userId, at) select ?, ?, ? where " +
         "(select count(*) from cx_analysis where userId = ? and at > ?) < ?",
    args: [id, userId, iso(now), userId, iso(now - 24 * 3600_000), max],
  });
  return rs.rowsAffected === 1;
}

export async function forgetAnalysis(client, id) {
  await client.execute({ sql: "delete from cx_analysis where id = ?", args: [id] });
}

export async function countAnalyses(client, userId, now = Date.now()) {
  const row = await one(client, "select count(*) as c from cx_analysis where userId = ? and at > ?",
                        [userId, iso(now - 24 * 3600_000)]);
  return Number(row.c);
}

// The log so far, while the analysis runs (updatedAt stays the start time).
export async function saveLog(client, serviceId, log) {
  await client.execute({ sql: "update cx_service set log = ? where id = ? and status = 'analyzing'",
                         args: [log, serviceId] });
}

export async function finishService(client, serviceId, { status, sha = null, plan = null,
                                                         log, error = null }, now = Date.now()) {
  await client.execute({
    sql: "update cx_service set status = ?, sha = ?, plan = ?, log = ?, error = ?, updatedAt = ? " +
         "where id = ?",
    args: [status, sha, plan === null ? null : JSON.stringify(plan), log, error, iso(now), serviceId],
  });
}

export async function getService(client, serviceId) {
  return one(client, "select * from cx_service where id = ?", [serviceId]);
}

// ---------- admin ----------

export async function counts(client) {
  const row = await one(client, `select
    (select count(*) from "user") as users,
    (select count(*) from "user" where emailVerified = 1) as verifiedUsers,
    (select count(*) from cx_project) as projects,
    (select count(*) from cx_service) as services,
    (select count(*) from cx_service where status = 'planned') as planned,
    (select count(*) from cx_github) as connections`);
  return Object.fromEntries(Object.entries(row).map(([k, v]) => [k, Number(v)]));
}
