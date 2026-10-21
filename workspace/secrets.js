// Two small pieces of cryptography the workspace needs, both keyed off BETTER_AUTH_SECRET
// (the one secret Vercel already has), each through its own derived key so neither can
// stand in for the other or for Better Auth's own use of the secret.
//
// - Settings encryption: the GitHub App's private key, webhook secret and client secret
//   live in Turso (cx_setting). A leaked database dump must not hand over the App.
//   The platform decrypts the same format at import (python: AESGCM), so the format is
//   fixed: "enc1:" + base64url(iv[12] ‖ ciphertext ‖ tag[16]), AES-256-GCM, no AAD,
//   key = SHA-256("cicatrixa-settings\0" + secret).
// - Signed state tokens: what binds a GitHub round trip to the account that started it
//   (gh:<userId> for install/authorize, ghapp:<userId> for creating the App).
import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes,
         timingSafeEqual } from "node:crypto";

const ENC_PREFIX = "enc1:";

function deriveKey(label, secret) {
  if (!secret) throw new Error("BETTER_AUTH_SECRET is not set");
  return createHash("sha256").update(`${label}\0${secret}`, "utf8").digest();
}

export function encryptSetting(secret, plaintext, iv = randomBytes(12)) {
  const cipher = createCipheriv("aes-256-gcm", deriveKey("cicatrixa-settings", secret), iv);
  const ct = Buffer.concat([cipher.update(String(plaintext), "utf8"), cipher.final()]);
  return ENC_PREFIX + Buffer.concat([iv, ct, cipher.getAuthTag()]).toString("base64url");
}

// Values without the prefix are plain (gh_app_id, gh_app_slug, …) and come back as-is.
export function decryptSetting(secret, stored) {
  if (typeof stored !== "string" || !stored.startsWith(ENC_PREFIX)) return stored;
  const raw = Buffer.from(stored.slice(ENC_PREFIX.length), "base64url");
  if (raw.length < 12 + 16) throw new Error("cannot decrypt a setting: value is truncated");
  const decipher = createDecipheriv("aes-256-gcm", deriveKey("cicatrixa-settings", secret),
                                    raw.subarray(0, 12));
  decipher.setAuthTag(raw.subarray(raw.length - 16));
  try {
    return Buffer.concat([decipher.update(raw.subarray(12, raw.length - 16)),
                          decipher.final()]).toString("utf8");
  } catch {
    // Almost always a changed BETTER_AUTH_SECRET: the stored App keys are then lost.
    throw new Error("cannot decrypt a setting (was BETTER_AUTH_SECRET changed?)");
  }
}

function mac(secret, body) {
  return createHmac("sha256", deriveKey("cicatrixa-state", secret)).update(body, "utf8")
    .digest("base64url");
}

// `${payload}.${exp}.${mac}`, exp in epoch seconds.
export function signState(secret, payload, ttlSeconds = 3600, now = Date.now()) {
  const exp = Math.floor(now / 1000) + ttlSeconds;
  return `${payload}.${exp}.${mac(secret, `${payload}.${exp}`)}`;
}

// The part after `prefix`, or null. The prefix must match exactly ("gh:" is never
// "ghapp:"), the MAC must be ours and the token unexpired.
export function readState(secret, token, prefix, now = Date.now()) {
  if (typeof token !== "string" || !secret) return null;
  const last = token.lastIndexOf(".");
  const middle = token.lastIndexOf(".", last - 1);
  if (last < 0 || middle < 0) return null;
  const payload = token.slice(0, middle);
  const exp = token.slice(middle + 1, last);
  if (!/^\d{1,12}$/.test(exp)) return null;
  const given = Buffer.from(token.slice(last + 1), "utf8");
  const want = Buffer.from(mac(secret, `${payload}.${exp}`), "utf8");
  if (given.length !== want.length || !timingSafeEqual(given, want)) return null;
  if (Number(exp) * 1000 <= now) return null;
  if (!payload.startsWith(prefix)) return null;
  return payload.slice(prefix.length) || null;
}
