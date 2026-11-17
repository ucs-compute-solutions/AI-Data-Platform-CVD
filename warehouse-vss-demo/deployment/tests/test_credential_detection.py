"""Literal credential rejection with runtime-built, obviously synthetic data."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import warehouse_deploy as deploy


def synthetic_value():
    return "synthetic-" + "credential-" + "fixture-only"


def assignment(name, value, quote="", operator="="):
    return name + operator + quote + value + quote


def configmap(text, field="bootstrap.sh"):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "synthetic-config"},
            "data": {field: text}}


class CredentialDetectionTests(unittest.TestCase):
    def reject(self, text):
        try:
            deploy.check_credential_text(text, "synthetic-fixture")
        except deploy.Invalid as error:
            self.assertFalse(synthetic_value() in str(error), "Matched credential values must be suppressed")
            self.assertTrue("Possible literal credential" in str(error), "Use a sanitized credential failure")
        else:
            self.fail("A runtime-built synthetic literal credential was accepted")

    def test_prefixed_shell_and_connection_assignment_names(self):
        for index, name in enumerate(("PG_ADMIN_PASSWORD", "PGPASSWORD", "POSTGRES_PASSWORD", "MYSQL_PWD",
                                      "REDIS_PASSWORD", "database.password", "TWILIO_AUTH_TOKEN", "CLIENT_SECRET")):
            with self.subTest(case=index):
                self.reject(assignment(name, synthetic_value()))

    def test_export_readonly_and_declared_assignments(self):
        for index, prefix in enumerate(("export ", "readonly ", "declare -x ", "env ", "env -- ")):
            with self.subTest(case=index):
                self.reject(prefix + assignment("PGPASSWORD", synthetic_value(), '"'))

    def test_shell_word_punctuation_is_not_a_comment_or_configuration_separator(self):
        for index, value in enumerate(("#" + synthetic_value(), "," + synthetic_value(),
                                       "${PG_APP_PASSWORD}#" + synthetic_value(),
                                       "${PG_APP_PASSWORD}," + synthetic_value(),
                                       "${PG_APP_PASSWORD}?" + synthetic_value(),
                                       "${PG_APP_PASSWORD}}" + synthetic_value())):
            with self.subTest(case=index):
                self.reject(assignment("PGPASSWORD", value))
                self.reject("PGPASSWORD=\"${PG_APP_PASSWORD}\"" + value)

    def test_sql_quote_syntax_does_not_make_shell_literal_prefixes_safe(self):
        for index, value in enumerate(("E''", "N\"\"", "$$$$" + synthetic_value())):
            with self.subTest(case=index):
                self.reject(assignment("PGPASSWORD", value))

    def test_shell_word_block_comment_characters_are_not_discarded(self):
        fragment = "/*" + synthetic_value() + "*/"
        for index, value in enumerate((fragment, "${PG_APP_PASSWORD}" + fragment,
                                       '"${PG_APP_PASSWORD}"' + fragment)):
            with self.subTest(case=index):
                self.reject(assignment("PGPASSWORD", value))

    def test_leading_empty_sql_statement_does_not_hide_password_clause(self):
        self.reject("; ALTER ROLE example PASSWORD '" + synthetic_value() + "';")

    def test_multiline_quotes_whitespace_and_line_continuations(self):
        cases = [assignment("PG_ADMIN_PASSWORD", "\n" + synthetic_value() + "\n", '"'),
                 assignment("POSTGRES_PASSWORD", synthetic_value() + "\ncontinued", "'"),
                 "PGPASSWORD=\\\n  '" + synthetic_value() + "'",
                 "PGPASSWORD =\n  \"" + synthetic_value() + '"',
                 "password:\n  '" + synthetic_value() + "'"]
        for index, text in enumerate(cases):
            with self.subTest(case=index):
                self.reject(text)

    def test_json_and_nested_serialized_script_credentials(self):
        self.reject(json.dumps({"database": {"password": synthetic_value()}}))
        embedded = configmap(assignment("PGPASSWORD", synthetic_value(), '"'))
        self.reject(json.dumps({"resources": [embedded]}))
        self.reject(json.dumps({"file.json": json.dumps({"PG_APP_PASSWORD": synthetic_value()})}))

    def test_sql_password_single_double_and_dollar_quoted_clauses(self):
        for index, value in enumerate(("'" + synthetic_value() + "'", '"' + synthetic_value() + '"',
                                       "E'" + synthetic_value() + "'", "N'" + synthetic_value() + "'",
                                       "$$" + synthetic_value() + "$$", "$fixture$" + synthetic_value() + "$fixture$")):
            with self.subTest(case=index):
                self.reject("ALTER ROLE example PASSWORD\n " + value + ";")
                self.reject("CREATE USER example WITH PASSWORD =\n " + value + ";")

    def test_sql_literals_with_quotes_or_comment_markers_are_not_ignored(self):
        for index, token in enumerate(("'/*" + synthetic_value() + "*/'", "'\n# " + synthetic_value() + "\n'",
                                       "$fixture$\n-- " + synthetic_value() + "\n$fixture$",
                                       "'" + synthetic_value() + "''continued'")):
            with self.subTest(case=index):
                self.reject("ALTER USER example PASSWORD " + token + ";")

    def test_uri_userinfo_and_connection_query_credentials(self):
        for index, text in enumerate(("postgresql://example:" + synthetic_value() + "@db.example.org/demo",
                                      "rtsp://example:" + synthetic_value() + "@camera.example.org/live",
                                      "jdbc:postgresql://db.example.org/demo?user=example&password=" + synthetic_value(),
                                      "Server=db.example.org;User ID=example;Password=" + synthetic_value() + ";")):
            with self.subTest(case=index):
                self.reject(text)

    def test_runtime_defaults_suffixes_and_arbitrary_commands_are_not_exempt(self):
        cases = ["${PG_APP_PASSWORD:-" + synthetic_value() + "}",
                 "${PG_APP_PASSWORD-" + synthetic_value() + "}",
                 "${PG_APP_PASSWORD}" + synthetic_value(),
                 "$(printf '%s' '" + synthetic_value() + "')",
                 ":'app_password'" + synthetic_value(), "@@APP_PASSWORD@@" + synthetic_value()]
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                self.reject(assignment("PGPASSWORD", value))
                self.reject(assignment("PGPASSWORD", value, '"'))

    def test_quoted_null_and_boolean_spellings_are_not_exempt(self):
        for index, value in enumerate(("null", "None", "false", "true")):
            with self.subTest(case=index):
                self.reject(assignment("password", value, '"', ": "))
                self.reject(assignment("PGPASSWORD", value))
                self.reject("ALTER ROLE example PASSWORD '" + value + "';")

    def test_empty_quote_followed_by_literal_is_not_exempt(self):
        for index, prefix in enumerate(('""', "''", '"${PG_APP_PASSWORD}"')):
            with self.subTest(case=index):
                self.reject("PGPASSWORD=" + prefix + synthetic_value())

    def test_exact_environment_psql_and_template_references_are_allowed(self):
        for index, value in enumerate(("$PG_APP_PASSWORD", "${PG_APP_PASSWORD}",
                                       "${PG_APP_PASSWORD:?existing application Secret is required}",
                                       "${PG_APP_PASSWORD?existing Secret is required}", "@@APP_PASSWORD@@")):
            with self.subTest(case=index):
                deploy.check_credential_text(assignment("PGPASSWORD", value, '"'), "synthetic-reference")
                deploy.check_credential_text(assignment("PGPASSWORD", value), "synthetic-reference")
        for index, value in enumerate((":'app_password'", ':"app_password"', ":app_password")):
            with self.subTest(case=index):
                deploy.check_credential_text("ALTER ROLE example PASSWORD " + value + ";", "synthetic-reference")
        deploy.check_credential_text("postgresql://${DB_USER}:${PG_APP_PASSWORD}@db.example.org/demo", "synthetic-reference")
        deploy.check_credential_text("postgresql://db.example.org/demo?password=${PG_APP_PASSWORD}&port=5432", "synthetic-reference")

    def test_typed_configuration_null_boolean_and_empty_values_are_allowed(self):
        for index, value in enumerate((None, False, True, "")):
            with self.subTest(case=index):
                deploy.check_credential_text(json.dumps({"password": value}), "synthetic-config")
        for text in ("password: null", "password: false", "password: true", 'PGPASSWORD=""', "PGPASSWORD="):
            deploy.check_credential_text(text, "synthetic-config")

    def test_nonsecret_password_settings_remain_ordinary_configuration(self):
        for text in ("password_encryption = 'scram-sha-256'", "default_password_enabled = true",
                     "max_tokens=4096", 'secretName: application-secret', 'key: app-password',
                     'PGHOST="$socket" PGUSER=postgres PGDATABASE=postgres psql --no-psqlrc'):
            deploy.check_credential_text(text, "synthetic-config")

    def test_inert_vendor_comments_are_ignored(self):
        fixture = assignment("PGPASSWORD", synthetic_value(), '"')
        for index, prefix in enumerate(("# ", "; ", "// ", "-- ")):
            with self.subTest(case=index):
                deploy.check_credential_text(prefix + fixture + "\nport=5432", "synthetic-comment")
        deploy.check_credential_text("/*\n" + fixture + "\n*/\nport=5432", "synthetic-comment")

    def test_guard_checks_configmap_values_and_command_argument_strings(self):
        resources = [configmap(assignment("PGPASSWORD", synthetic_value(), '"')),
                     configmap(synthetic_value(), "PGPASSWORD"),
                     {"kind": "Deployment", "metadata": {"name": "synthetic-workload"}, "spec": {
                         "template": {"spec": {"containers": [{"name": "example", "args": [
                             assignment("PGPASSWORD", synthetic_value())]}]}}}}]
        for index, resource in enumerate(resources):
            with self.subTest(case=index):
                try:
                    deploy.check_no_credentials([resource])
                except deploy.Invalid as error:
                    self.assertFalse(synthetic_value() in str(error), "Matched credential values must be suppressed")
                else:
                    self.fail("A synthetic resource credential was accepted")

    def test_existing_secret_key_refs_are_allowed_without_reading_values(self):
        resource = {"kind": "Deployment", "metadata": {"name": "synthetic-workload"}, "spec": {
            "template": {"spec": {"containers": [{"name": "example", "env": [{"name": "PGPASSWORD", "valueFrom": {
                "secretKeyRef": {"name": "existing-application", "key": "app-password"}}}]}]}}}}
        deploy.check_no_credentials([resource])

    def test_fresh_bootstrap_and_embedded_runtime_bindings_remain_identical_and_allowed(self):
        script = (ROOT / "fresh-postgres-bootstrap.sh").read_text()
        baseline_text = (ROOT / "baseline/baseline.captured.json").read_text()
        baseline = json.loads(baseline_text)
        scripts = [value for resource in baseline["resources"] if resource.get("kind") == "ConfigMap"
                   for value in resource.get("data", {}).values()]
        self.assertTrue(any(script == value for value in scripts), "Embedded runtime bootstrap must remain byte-identical")
        deploy.check_credential_text(script, "fresh-bootstrap")
        deploy.check_credential_text(baseline_text, "captured-baseline")
        deploy.check_no_credentials(baseline["resources"])


if __name__ == "__main__":
    unittest.main()
