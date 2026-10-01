/**
 * Unit tests of the accounts / roles helpers (src/lib/auth.ts, ARCHITECTURE.md §12.2 and
 * §12.6): permissions, e-mail normalisation, local validation of the login, registration,
 * password-change and account-creation forms, per-row actions of the users table.
 * Run with `npm test`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isValid,
  isValidEmail,
  normalizeEmail,
  passwordError,
  pendingCount,
  permissionsFor,
  userNameError,
  userRowActions,
  validateLogin,
  validateNewUser,
  validatePasswordChange,
  validateRegistration,
} from "../src/lib/auth.ts";

test("permissionsFor: admin, member, unknown user, Electron local mode", () => {
  assert.deepEqual(permissionsFor({ role: "admin" }), { role: "admin", isAdmin: true, canWrite: true, canViewCode: true, loading: false });
  assert.deepEqual(permissionsFor({ role: "member" }), { role: "member", isAdmin: false, canWrite: false, canViewCode: false, loading: false });
  // Unknown user (loading, older backend, network error): read-only, never admin.
  assert.deepEqual(permissionsFor(null, { loading: true }), { role: null, isAdmin: false, canWrite: false, canViewCode: false, loading: true });
  assert.deepEqual(permissionsFor(undefined), { role: null, isAdmin: false, canWrite: false, canViewCode: false, loading: false });
  // Electron local mode = server token: admin even without /api/me, never "loading".
  assert.deepEqual(permissionsFor(null, { implicitAdmin: true, loading: true }), { role: "admin", isAdmin: true, canWrite: true, canViewCode: true, loading: false });
  assert.equal(permissionsFor({ role: "member" }, { implicitAdmin: true }).isAdmin, true);
});

test("normalizeEmail / isValidEmail", () => {
  assert.equal(normalizeEmail("  Justin@Example.COM "), "justin@example.com");
  assert.equal(isValidEmail("marie@example.com"), true);
  assert.equal(isValidEmail(" Marie@Example.com "), true);
  for (const bad of ["", "marie", "marie@", "@example.com", "marie@example", "ma rie@example.com"]) assert.equal(isValidEmail(bad), false, bad);
});

test("userNameError mirrors the server identifier rule", () => {
  for (const ok of ["justin", "marie.dupont", "j_2", "Paul-3", "a"]) assert.equal(userNameError(ok), null, ok);
  assert.match(userNameError("") ?? "", /Indiquez/);
  for (const bad of ["-paul", ".x", "ma rie", "é", "a".repeat(65), "x/y"]) assert.match(userNameError(bad) ?? "", /invalide/, bad);
  assert.match(userNameError("admin") ?? "", /réservé/);
  assert.match(userNameError("Admin") ?? "", /réservé/);
});

test("passwordError: 12 to 256 characters, not the e-mail nor the name", () => {
  assert.match(passwordError("") ?? "", /Indiquez/);
  assert.match(passwordError("a".repeat(11)) ?? "", /au moins 12/);
  assert.equal(passwordError("a".repeat(12)), null);
  assert.equal(passwordError("a".repeat(256)), null);
  assert.match(passwordError("a".repeat(257)) ?? "", /256/);
  assert.match(passwordError("Marie@Example.com", { email: "marie@example.com" }) ?? "", /e-mail ou votre identifiant/);
  assert.match(passwordError("marie.dupont1", { name: "Marie.Dupont1" }) ?? "", /e-mail ou votre identifiant/);
  assert.equal(passwordError("une phrase de passe", { email: "marie@example.com", name: "marie" }), null);
});

test("validateLogin", () => {
  assert.deepEqual(validateLogin({ email: "justin@example.com", password: "x" }), {});
  const errs = validateLogin({ email: "", password: "" });
  assert.match(errs.email ?? "", /e-mail/);
  assert.match(errs.password ?? "", /mot de passe/);
  assert.match(validateLogin({ email: "justin", password: "x" }).email ?? "", /invalide/);
});

test("validateRegistration: identifier, e-mail, password policy, confirmation", () => {
  const ok = { name: "zoe", email: "zoe@example.com", password: "une phrase de passe", confirm: "une phrase de passe" };
  assert.deepEqual(validateRegistration(ok), {});
  assert.equal(isValid(validateRegistration(ok)), true);
  const errs = validateRegistration({ name: "admin", email: "zoe@", password: "court", confirm: "court" });
  assert.match(errs.name ?? "", /réservé/);
  assert.match(errs.email ?? "", /invalide/);
  assert.match(errs.password ?? "", /au moins 12/);
  assert.equal(errs.confirm, undefined); // confirmation only checked once the password is acceptable
  assert.match(validateRegistration({ ...ok, confirm: "une autre phrase" }).confirm ?? "", /ne correspondent pas/);
  assert.match(validateRegistration({ ...ok, password: "zoe@example.com", confirm: "zoe@example.com" }).password ?? "", /e-mail/);
  assert.equal(isValid(validateRegistration({ ...ok, name: "" })), false);
});

test("validatePasswordChange", () => {
  const me = { email: "nina@example.com", name: "nina" };
  assert.deepEqual(validatePasswordChange({ current: "temporaire-123", next: "nouvelle phrase 42", confirm: "nouvelle phrase 42" }, me), {});
  const errs = validatePasswordChange({ current: "", next: "court", confirm: "" }, me);
  assert.match(errs.current ?? "", /actuel/);
  assert.match(errs.next ?? "", /au moins 12/);
  assert.match(validatePasswordChange({ current: "même phrase longue", next: "même phrase longue", confirm: "même phrase longue" }, me).next ?? "", /différent/);
  assert.match(validatePasswordChange({ current: "x", next: "nouvelle phrase 42", confirm: "autre" }, me).confirm ?? "", /ne correspondent pas/);
  assert.match(validatePasswordChange({ current: "x", next: "nina@example.com", confirm: "nina@example.com" }, me).next ?? "", /e-mail/);
});

test("validateNewUser (admin « Créer un compte »)", () => {
  assert.deepEqual(validateNewUser({ email: "lucie@example.com", name: "lucie" }), {});
  const errs = validateNewUser({ email: "lucie", name: "lu cie" });
  assert.match(errs.email ?? "", /invalide/);
  assert.match(errs.name ?? "", /invalide/);
});

test("userRowActions: per-status actions, none on one's own account", () => {
  const me = "u_1f3c9a0b7d2e";
  assert.deepEqual(userRowActions({ id: "u_9c3d7e1a2b4f", status: "pending" }, me), {
    self: false,
    approve: true,
    changeRole: false,
    disable: true,
    enable: false,
    resetPassword: false,
  });
  assert.deepEqual(userRowActions({ id: "u_5a8e2c4f6b10", status: "active" }, me), {
    self: false,
    approve: false,
    changeRole: true,
    disable: true,
    enable: false,
    resetPassword: true,
  });
  assert.deepEqual(userRowActions({ id: "u_2e6f8a0c4d1b", status: "disabled" }, me), {
    self: false,
    approve: false,
    changeRole: true,
    disable: false,
    enable: true,
    resetPassword: true,
  });
  const self = userRowActions({ id: me, status: "active" }, me);
  assert.equal(self.self, true);
  assert.equal(Object.entries(self).some(([k, v]) => k !== "self" && v), false);
  // Server token (`Me.id` = ""): no row is "self".
  assert.equal(userRowActions({ id: me, status: "active" }, "").self, false);
  assert.equal(userRowActions({ id: me, status: "active" }, null).self, false);
});

test("pendingCount", () => {
  assert.equal(pendingCount(undefined), 0);
  assert.equal(pendingCount([{ status: "pending" }, { status: "active" }, { status: "pending" }, { status: "disabled" }]), 2);
});
