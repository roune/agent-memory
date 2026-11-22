#!/usr/bin/env python3
"""Tokens and identity, without Starlette: auth.py, the token table, and `agent-memory-server token`.

test_server_app.py covers the same rules over HTTP when the server extra is installed.
"""

import io
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_memory_server import auth, cli
from agent_memory_server.store import ServerStore


class TokenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {name: os.environ.get(name) for name in ("HOME", "AGENT_MEMORY_DIR")}
        os.environ["HOME"] = str(Path(self.tmp.name) / "no-such-home")
        self.addCleanup(self._restore)
        quiet = logging.NullHandler()
        logging.getLogger("agent_memory_server").addHandler(quiet)
        self.addCleanup(logging.getLogger("agent_memory_server").removeHandler, quiet)
        self.data = Path(self.tmp.name) / "data"
        self.store = ServerStore(self.data / "memories.sqlite")

    def _restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def mint(self, person="miguel", scopes=("read", "write"), **extra):
        return self.store.create_token(person, "Acme", "Platform", scopes, **extra)


class SecretsTest(TokenCase):
    def test_only_the_hash_is_stored(self):
        token_id, secret = self.mint()
        self.assertTrue(secret.startswith("am_"))
        self.assertGreater(len(secret), 40)
        connection = sqlite3.connect(self.store.path)
        try:
            dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        self.assertNotIn(secret, dump)
        self.assertNotIn(secret[3:], dump)
        self.assertIn(auth.hash_secret(secret), dump)
        self.assertEqual(self.store.token_for_secret(secret)["id"], token_id)
        self.assertIsNone(self.store.token_for_secret(secret + "x"))

    def test_scopes_and_bearer_parsing(self):
        self.assertEqual(auth.parse_scopes("write, READ"), ("read", "write"))
        for bad in ("", "root", "read,owner"):
            with self.assertRaises(ValueError):
                auth.parse_scopes(bad)
        self.assertEqual(auth.bearer("Bearer am_x"), "am_x")
        self.assertEqual(auth.bearer("bearer   am_x "), "am_x")
        for header in (None, "", "Basic am_x", "Bearer", "am_x"):
            self.assertIsNone(auth.bearer(header))


class RevokeTest(TokenCase):
    def test_a_revoked_token_and_its_children_stop_working(self):
        parent_id, parent = self.mint()
        child_id, child = self.mint(scopes=("read",), label="cursor", parent_id=parent_id)
        grandchild_id, grandchild = self.mint(scopes=("read",), label="x", parent_id=child_id)
        self.assertIsNotNone(self.store.token_for_secret(child))
        self.assertEqual(self.store.revoke_token(parent_id), 3)
        for secret in (parent, child, grandchild):
            self.assertIsNone(self.store.token_for_secret(secret))
        self.assertEqual(self.store.revoke_token(parent_id), 0)

    def test_a_child_of_a_revoked_parent_is_refused_even_if_not_marked(self):
        parent_id, _ = self.mint()
        _, child = self.mint(scopes=("read",), label="cursor", parent_id=parent_id)
        connection = sqlite3.connect(self.store.path)
        connection.execute("UPDATE token SET revoked_at = '2026-11-22T00:00:00+00:00' WHERE id = ?", (parent_id,))
        connection.commit()
        connection.close()
        self.assertIsNone(self.store.token_for_secret(child))

    def test_the_same_child_label_replaces_the_earlier_token(self):
        parent_id, _ = self.mint()
        _, first = self.mint(scopes=("read",), label="claude-code", parent_id=parent_id)
        _, other = self.mint(scopes=("read",), label="cursor", parent_id=parent_id)
        _, second = self.mint(scopes=("read",), label="claude-code", parent_id=parent_id)
        self.assertIsNone(self.store.token_for_secret(first))
        self.assertIsNotNone(self.store.token_for_secret(second))
        self.assertIsNotNone(self.store.token_for_secret(other))

    def test_a_second_top_level_token_for_a_person_needs_same_person(self):
        self.mint()
        with self.assertRaisesRegex(auth.AuthError, "miguel already has an active token"):
            self.mint()
        self.mint(same_person=True)
        self.mint(person="ana")


class IdentityTest(unittest.TestCase):
    caller = auth.Caller(
        token_id="t_1",
        person="miguel",
        org="Acme",
        team="Platform",
        allowed_orgs=("Acme EU",),
        allowed_teams=("Infra",),
        scopes=("read", "write"),
    )

    def test_the_token_pins_person_and_fills_org_and_team(self):
        pinned = auth.pin_identity(self.caller, {"text": "x"})
        self.assertEqual((pinned["person"], pinned["org"], pinned["team"]), ("miguel", "Acme", "Platform"))
        self.assertEqual(auth.pin_identity(self.caller, {"person": "miguel"})["person"], "miguel")
        with self.assertRaises(auth.AuthError) as raised:
            auth.pin_identity(self.caller, {"person": "ana"})
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("writes as miguel", str(raised.exception))

    def test_org_and_team_only_from_the_tokens_lists(self):
        allowed = auth.pin_identity(self.caller, {"org": "Acme EU", "team": "Infra"})
        self.assertEqual((allowed["org"], allowed["team"]), ("Acme EU", "Infra"))
        for body, message in (({"org": "Other"}, "org 'Other'"), ({"team": "Sales"}, "team 'Sales'")):
            with self.assertRaisesRegex(auth.AuthError, message):
                auth.pin_identity(self.caller, body)

    def test_with_auth_off_the_body_is_used(self):
        body = {"person": "anyone", "org": "Any", "team": "Any"}
        self.assertEqual(auth.pin_identity(auth.Caller.local(), body), body)

    def test_scopes_are_not_implied(self):
        writer = auth.Caller(token_id="t", person="p", org="o", team="t", scopes=("write",))
        writer.require("write")
        for scope in ("read", "admin"):
            with self.assertRaises(auth.AuthError) as raised:
                writer.require(scope)
            self.assertEqual(raised.exception.status, 403)


class TokenCommandTest(TokenCase):
    def run_cli(self, *argv):
        out, err = [], io.StringIO()
        code = cli.run(
            ["token", *argv],
            environ={"AGENT_MEMORY_SERVER_DATA": str(self.data), "AGENT_MEMORY_EMBEDDER": "openai"},
            out=out.append,
            err=err,
        )
        return code, out, err.getvalue()

    def test_create_prints_the_secret_once_and_list_never_does(self):
        code, out, _ = self.run_cli(
            "create", "--person", "miguel", "--org", "Acme", "--team", "Platform",
            "--scopes", "read,write", "--allow-team", "Infra", "--label", "laptop",
        )
        self.assertEqual(code, 0)
        secret = out[1]
        self.assertTrue(secret.startswith("am_"))
        token_id = out[0].split()[1]
        caller = auth.Caller.from_row(self.store.token_for_secret(secret))
        self.assertEqual((caller.person, caller.scopes, caller.allowed_teams), ("miguel", ("read", "write"), ("Infra",)))
        code, listed, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn(token_id, listed[0])
        self.assertIn("[laptop]", listed[0])
        self.assertNotIn(secret, "\n".join(listed))
        self.assertNotIn(auth.hash_secret(secret), "\n".join(listed))

    def test_create_refuses_bad_input_and_an_existing_person(self):
        base = ("create", "--person", "miguel", "--org", "Acme", "--team", "Platform")
        self.assertEqual(self.run_cli(*base)[0], 0)
        code, _, err = self.run_cli(*base)
        self.assertEqual(code, 1)
        self.assertIn("--same-person", err)
        self.assertEqual(self.run_cli(*base, "--same-person")[0], 0)
        self.assertIn("unknown scope", self.run_cli(*base, "--scopes", "root")[2])
        self.assertIn("single path segment", self.run_cli("create", "--person", "a/b", "--org", "A", "--team", "T")[2])

    def test_revoke(self):
        _, out, _ = self.run_cli("create", "--person", "miguel", "--org", "Acme", "--team", "Platform")
        token_id = out[0].split()[1]
        self.assertEqual(self.run_cli("revoke", token_id)[:2], (0, ["revoked 1 token"]))
        self.assertEqual(self.run_cli("revoke", token_id)[0], 1)
        self.assertIn("revoked", self.run_cli("list")[1][0])

    def test_token_commands_need_no_embedder(self):
        # AGENT_MEMORY_EMBEDDER=openai would stop serve and reindex; token doesn't load a model.
        self.assertEqual(self.run_cli("list")[:2], (0, ["no tokens"]))


if __name__ == "__main__":
    unittest.main()
