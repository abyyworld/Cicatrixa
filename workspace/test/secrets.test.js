// Settings encryption (the platform's import decrypts the same bytes) and the signed
// state tokens that tie a GitHub round trip to the account that started it.
import assert from "node:assert/strict";
import { test } from "node:test";

import { decryptSetting, encryptSetting, readState, signState } from "../secrets.js";

const SECRET = "test-secret-that-is-long-enough-0123456789";

// Shared with the platform's Python tests (app.import_online): same secret, same
// plaintext, same stored value. iv = 000102030405060708090a0b.
const VECTOR = {
  secret: "cicatrixa-test-vector-secret",
  plaintext: "whsec-vector ✓ 2026",
  stored: "enc1:AAECAwQFBgcICQoLhQsGLtxOUP-DNop4XqdeUcYzlrMqxcNXMCTMfC3uE-saDg26CA",
};

test("a setting round-trips, and the stored value never shows the plaintext", () => {
  const pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow…\n-----END RSA PRIVATE KEY-----\n";
  const stored = encryptSetting(SECRET, pem);
  assert.match(stored, /^enc1:[A-Za-z0-9_-]+$/);
  assert.ok(!stored.includes("PRIVATE"));
  assert.equal(decryptSetting(SECRET, stored), pem);
  assert.notEqual(encryptSetting(SECRET, pem), stored, "a fresh IV every time");
});

test("the fixed vector decrypts, and encrypts to the same bytes with its IV", () => {
  assert.equal(decryptSetting(VECTOR.secret, VECTOR.stored), VECTOR.plaintext);
  const iv = Buffer.from("000102030405060708090a0b", "hex");
  assert.equal(encryptSetting(VECTOR.secret, VECTOR.plaintext, iv), VECTOR.stored);
});

test("plain values pass through; a wrong secret or a flipped byte is refused", () => {
  assert.equal(decryptSetting(SECRET, "12345"), "12345");
  assert.throws(() => decryptSetting("another secret", VECTOR.stored), /BETTER_AUTH_SECRET/);
  const raw = Buffer.from(VECTOR.stored.slice(5), "base64url");
  raw[14] ^= 1;
  assert.throws(() => decryptSetting(VECTOR.secret, `enc1:${raw.toString("base64url")}`));
  assert.throws(() => decryptSetting(SECRET, "enc1:AAAA"), /truncated/);
  assert.throws(() => encryptSetting("", "x"), /BETTER_AUTH_SECRET/);
});

test("a state token gives back its payload after the prefix", () => {
  const token = signState(SECRET, "gh:user_123");
  assert.equal(readState(SECRET, token, "gh:"), "user_123");
  const app = signState(SECRET, "ghapp:user_123");
  assert.equal(readState(SECRET, app, "ghapp:"), "user_123");
});

test("one kind of state is never accepted as another", () => {
  assert.equal(readState(SECRET, signState(SECRET, "ghapp:u1"), "gh:"), null);
  assert.equal(readState(SECRET, signState(SECRET, "gh:u1"), "ghapp:"), null);
  assert.equal(readState(SECRET, signState(SECRET, "xgh:u1"), "gh:"), null);
  assert.equal(readState(SECRET, signState(SECRET, "gh:"), "gh:"), null, "empty user id");
});

test("an expired state is refused", () => {
  const hourAgo = Date.now() - 3601 * 1000;
  assert.equal(readState(SECRET, signState(SECRET, "gh:u1", 3600, hourAgo), "gh:"), null);
  const short = signState(SECRET, "gh:u1", 60);
  assert.equal(readState(SECRET, short, "gh:"), "u1");
  assert.equal(readState(SECRET, short, "gh:", Date.now() + 61_000), null);
});

test("a tampered state is refused", () => {
  const token = signState(SECRET, "gh:alice");
  const [payload, exp, mac] = [token.slice(0, token.indexOf(".")),
                               ...token.slice(token.indexOf(".") + 1).split(".")];
  assert.equal(readState(SECRET, `gh:mallory.${exp}.${mac}`, "gh:"), null, "other user");
  assert.equal(readState(SECRET, `${payload}.${Number(exp) + 9999}.${mac}`, "gh:"), null, "longer life");
  const flipped = mac.slice(0, -1) + (mac.at(-1) === "A" ? "B" : "A");
  assert.equal(readState(SECRET, `${payload}.${exp}.${flipped}`, "gh:"), null, "bad MAC");
  assert.equal(readState("another secret", token, "gh:"), null, "other key");
  for (const junk of [null, undefined, "", "gh:alice", "gh:alice.123", 42, `${token}.x`]) {
    assert.equal(readState(SECRET, junk, "gh:"), null, String(junk));
  }
});

test("user ids with dots or colons survive the format", () => {
  const token = signState(SECRET, "gh:a.b:c");
  assert.equal(readState(SECRET, token, "gh:"), "a.b:c");
});
