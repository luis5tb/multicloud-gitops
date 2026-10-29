"""Offline protocol/authentication checks for the interactive MAS client."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mas-query.py"
SPEC = importlib.util.spec_from_file_location("mas_query", SCRIPT)
query = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = query
SPEC.loader.exec_module(query)


class Response(io.BytesIO):
    def __init__(self, body, content_type="application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


def task(state="WORKING", **extra):
    return {
        "task": {
            "id": "task-1",
            "contextId": "context-1",
            "status": {"state": "TASK_STATE_" + state},
            **extra,
        }
    }


def status(state, text="", thought=False):
    return {
        "statusUpdate": {
            "taskId": "task-1",
            "contextId": "context-1",
            "status": {
                "state": "TASK_STATE_" + state,
                "message": {
                    "parts": [{"text": text, "metadata": {"is_thought": thought}}]
                },
            },
        }
    }


class ScriptedClient:
    def __init__(self, steps, streaming=True):
        self.steps = iter(steps)
        self.use_stream = streaming
        self.calls = []
        self.credentials = Mock()

    def streaming(self):
        return self.use_stream

    def rpc(self, method, params):
        self.calls.append((method, params))
        expected_method, events = next(self.steps)
        if method != expected_method:
            raise AssertionError(f"Expected {expected_method}, got {method}")
        for event in events:
            if isinstance(event, Exception):
                raise event
            yield event


class MasQueryTests(unittest.TestCase):
    def setUp(self):
        self.stdout = io.StringIO()
        self.print_patch = patch("sys.stdout", self.stdout)
        self.print_patch.start()
        self.addCleanup(self.print_patch.stop)
        self.env_patch = patch.dict("os.environ", {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_paste_token_does_not_request_provider_or_mint(self):
        credentials = query.Credentials(Mock())
        with (
            patch("builtins.input", return_value="1") as prompt,
            patch.object(query.getpass, "getpass", return_value="Bearer pasted-token"),
        ):
            self.assertEqual(credentials.select(), "pasted-token")
        self.assertEqual(prompt.call_count, 1)
        credentials.http.open.assert_not_called()
        self.assertIn("Token used (pasted):\npasted-token", self.stdout.getvalue())

    def test_all_provider_choices_reach_token_command(self):
        for choice, provider, environment in [
            ("1", "keycloak", ""),
            ("2", "redhat", "production"),
            ("3", "redhat", "stage"),
        ]:
            with self.subTest(choice=choice):
                credentials = query.Credentials(
                    Mock(),
                    token_command="mint --provider {provider} --environment '{environment}'",
                )
                with (
                    patch("builtins.input", side_effect=["2", choice]),
                    patch.object(
                        query.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(
                            [], 0, "fresh-token\n", ""
                        ),
                    ) as run,
                ):
                    self.assertEqual(credentials.select(), "fresh-token")
                self.assertEqual(
                    run.call_args.args[0],
                    ["mint", "--provider", provider, "--environment", environment],
                )
                self.assertNotIn("shell", run.call_args.kwargs)

    def test_redhat_grant_has_openid_and_separate_environment_credentials(self):
        for environment in ["production", "stage"]:
            with (
                self.subTest(environment=environment),
                patch.dict(
                    "os.environ",
                    {
                        f"REDHAT_SSO_{environment.upper()}_TOKEN_URL": "https://sso.example.test/token",
                        f"REDHAT_SSO_{environment.upper()}_CLIENT_ID": environment
                        + "-client",
                        f"REDHAT_SSO_{environment.upper()}_CLIENT_SECRET": environment
                        + "-secret",
                    },
                ),
                patch("builtins.input", return_value=""),
            ):
                http = Mock()
                http.open.return_value = Response(b'{"access_token":"minted-token"}')
                credentials = query.Credentials(
                    http, provider="redhat", environment=environment
                )
                credentials.configure()
                credentials.mint()
                request = http.open.call_args.args[0]
                form = urllib.parse.parse_qs(request.data.decode())
                self.assertEqual(
                    form,
                    {
                        "grant_type": ["client_credentials"],
                        "scope": ["openid api.ask_red_hat"],
                        "client_id": [environment + "-client"],
                        "client_secret": [environment + "-secret"],
                    },
                )

    def test_spiffe_assertion_is_reread_and_omits_client_id(self):
        with tempfile.TemporaryDirectory() as directory:
            assertion = Path(directory) / "assertion"
            assertion.write_text("first-assertion")
            http = Mock()
            http.open.side_effect = [
                Response(b'{"access_token":"first-token"}'),
                Response(b'{"access_token":"second-token"}'),
            ]
            credentials = query.Credentials(http, provider="keycloak")
            with patch(
                "builtins.input",
                side_effect=[
                    "https://keycloak.example.test/token",
                    "client",
                    "3",
                    str(assertion),
                ],
            ):
                credentials.configure()
            credentials.mint()
            assertion.write_text("rotated-assertion")
            credentials.renew()
            forms = [
                urllib.parse.parse_qs(call.args[0].data.decode())
                for call in http.open.call_args_list
            ]
            self.assertEqual(forms[0]["client_assertion"], ["first-assertion"])
            self.assertEqual(forms[1]["client_assertion"], ["rotated-assertion"])
            self.assertNotIn("client_id", forms[0])
            self.assertNotIn("client_secret", forms[0])

    def test_keycloak_secret_basic_encodes_reserved_characters(self):
        credentials = query.Credentials(Mock(), provider="keycloak")
        with (
            patch(
                "builtins.input",
                side_effect=["https://keycloak.example.test/token", "client:id", "2"],
            ),
            patch.object(query.getpass, "getpass", return_value="a:b c"),
        ):
            credentials.configure()
        self.assertEqual(
            base64.b64decode(credentials.basic).decode(), "client%3Aid:a%3Ab+c"
        )
        self.assertNotIn("client_id", credentials.form)

    def test_sse_multiline_crlf_comments_and_eof(self):
        response = Response(
            b': heartbeat\r\ndata: {"result":\r\ndata: {"task": {"id": "one"}}}\r\n\r\n'
            b'data: {"result": {"message": {"parts": []}}}'
        )
        self.assertEqual(
            list(query.sse_payloads(response)),
            [
                {"result": {"task": {"id": "one"}}},
                {"result": {"message": {"parts": []}}},
            ],
        )

    def test_rpc_retries_401_with_same_message_and_new_token(self):
        credentials = query.Credentials(Mock(), provider="pasted", token="old-token")
        http = Mock()
        http.open.side_effect = [
            query.Unauthorized("401"),
            Response(b'{"result":{"task":{"id":"one"}}}'),
        ]
        client = query.Client(http, "https://mas.example.test", credentials)
        params = query.message_params("Question")
        with patch.object(query.getpass, "getpass", return_value="new-token"):
            self.assertEqual(
                list(client.rpc("SendMessage", params)), [{"task": {"id": "one"}}]
            )
        first, second = [call.args[0] for call in http.open.call_args_list]
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.get_header("Authorization"), "Bearer old-token")
        self.assertEqual(second.get_header("Authorization"), "Bearer new-token")
        self.assertEqual(second.get_header("A2a-version"), "1.0")
        self.assertIn("new-token", self.stdout.getvalue())

    def test_second_401_is_not_retried(self):
        credentials = Mock(token="invalid")
        client = query.Client(Mock(), "https://mas.example.test", credentials)
        client.http.open.side_effect = query.Unauthorized("401")
        with self.assertRaises(query.Unauthorized):
            list(client.rpc("SendMessage", {}))
        self.assertEqual(client.http.open.call_count, 2)
        credentials.renew.assert_called_once()

    def test_http_errors_explain_oauth_failure_and_do_not_dump_body(self):
        http = query.Http("", 10)
        body = b'{"error":"unauthorized_client","error_description":"Service account disabled","secret":"do-not-print"}'
        http.opener.open = Mock(
            side_effect=urllib.error.HTTPError(
                "https://sso.example.test", 400, "Bad request", {}, io.BytesIO(body)
            )
        )
        with self.assertRaisesRegex(
            query.QueryError, "Service account disabled"
        ) as caught:
            http.open(Mock())
        self.assertNotIn("do-not-print", str(caught.exception))

    def test_ca_bundle_extends_system_trust_and_redirects_are_disabled(self):
        with patch.object(query.ssl, "create_default_context") as create:
            query.Http("extra-ca.pem", 10)
        create.assert_called_once_with()
        create.return_value.load_verify_locations.assert_called_once_with(
            cafile="extra-ca.pem"
        )
        self.assertIsNone(
            query.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.example.test"
            )
        )

    def test_output_separates_thoughts_and_handles_artifact_replacement(self):
        output = query.Output()
        output.ingest(status("WORKING", "Checking pods", thought=True))
        output.ingest(status("WORKING", "Checking pods", thought=True))
        for text, append in [
            ("partial", False),
            (" more", True),
            ("authoritative answer", False),
        ]:
            output.ingest(
                {
                    "artifactUpdate": {
                        "artifact": {"artifactId": "answer", "parts": [{"text": text}]},
                        "append": append,
                    }
                }
            )
        output.ingest(
            {
                "artifactUpdate": {
                    "artifact": {
                        "artifactId": "second",
                        "parts": [{"text": "second result"}],
                    }
                }
            }
        )
        output.ingest(status("COMPLETED", "authoritative answer"))
        self.assertEqual(output.answer(), "authoritative answer\n\nsecond result")
        self.assertEqual(self.stdout.getvalue().count("[Thinking] Checking pods"), 1)

    def test_snapshot_never_uses_user_question_as_result(self):
        output = query.Output()
        output.ingest(
            task(
                "COMPLETED",
                history=[{"role": "ROLE_USER", "parts": [{"text": "question"}]}],
            )
        )
        self.assertEqual(output.answer(), "")

    def run_client(self, client):
        with patch.object(query.time, "sleep"):
            return query.run_query(
                client, "Question about https://api.example.test:6443", 30, 0.01
            )

    def test_stream_disconnect_recovers_existing_task_without_resending(self):
        client = ScriptedClient(
            [
                (
                    "SendStreamingMessage",
                    [task(), status("WORKING", "Delegating"), ConnectionError("lost")],
                ),
                ("GetTask", [task()]),
                ("SubscribeToTask", [status("COMPLETED", "final answer")]),
            ]
        )
        self.assertEqual(self.run_client(client), 0)
        self.assertIn("Result:\nfinal answer", self.stdout.getvalue())
        self.assertIn("[Progress] Delegating", self.stdout.getvalue())
        self.assertEqual(
            [method for method, _ in client.calls].count("SendStreamingMessage"), 1
        )

    def test_polling_when_streaming_is_disabled(self):
        client = ScriptedClient(
            [
                ("SendMessage", [task()]),
                (
                    "GetTask",
                    [
                        task(
                            "COMPLETED",
                            artifacts=[
                                {"artifactId": "result", "parts": [{"text": "answer"}]}
                            ],
                        )
                    ],
                ),
            ],
            streaming=False,
        )
        self.assertEqual(self.run_client(client), 0)
        self.assertTrue(client.calls[0][1]["configuration"]["returnImmediately"])
        self.assertIn("Result:\nanswer", self.stdout.getvalue())

    def test_approval_resume_reads_past_initial_paused_snapshot(self):
        client = ScriptedClient(
            [
                ("SendStreamingMessage", [task("INPUT_REQUIRED")]),
                (
                    "SendStreamingMessage",
                    [
                        task("INPUT_REQUIRED"),
                        status("WORKING", "Approved"),
                        status("COMPLETED", "done"),
                    ],
                ),
            ]
        )
        with patch("builtins.input", return_value="approve") as prompt:
            self.assertEqual(self.run_client(client), 0)
        prompt.assert_called_once()
        original, reply = [params["message"] for _, params in client.calls]
        self.assertEqual(reply["taskId"], "task-1")
        self.assertEqual(reply["contextId"], "context-1")
        self.assertNotEqual(original["messageId"], reply["messageId"])

    def test_auth_required_renews_and_resumes_same_task(self):
        client = ScriptedClient(
            [
                ("SendStreamingMessage", [task("AUTH_REQUIRED")]),
                ("SendStreamingMessage", [status("COMPLETED", "done")]),
            ]
        )
        self.assertEqual(self.run_client(client), 0)
        client.credentials.renew.assert_called_once()
        self.assertEqual(client.calls[1][1]["message"]["taskId"], "task-1")

    def test_accepted_approval_reattaches_when_reply_stream_repeats_old_gate(self):
        client = ScriptedClient(
            [
                ("SendStreamingMessage", [task("INPUT_REQUIRED")]),
                ("SendStreamingMessage", [task("INPUT_REQUIRED")]),
                (
                    "SubscribeToTask",
                    [
                        task("INPUT_REQUIRED"),
                        status("WORKING", "Continuing"),
                        status("COMPLETED", "done"),
                    ],
                ),
            ]
        )
        with patch("builtins.input", return_value="approve") as prompt:
            self.assertEqual(self.run_client(client), 0)
        prompt.assert_called_once()
        self.assertEqual(client.calls[-1], ("SubscribeToTask", {"id": "task-1"}))

    def test_failure_displays_result_and_exits_nonzero(self):
        client = ScriptedClient(
            [("SendStreamingMessage", [status("FAILED", "Delegation denied")])]
        )
        self.assertEqual(self.run_client(client), 1)
        self.assertIn("Result:\nDelegation denied", self.stdout.getvalue())

    def test_remote_http_and_credentials_in_url_are_rejected(self):
        for url in [
            "http://mas.example.test",
            "https://user:secret@mas.example.test",
            "https://mas.example.test?token=x",
        ]:
            with self.subTest(url=url), self.assertRaises(query.QueryError):
                query.validate_url(url)
        self.assertEqual(
            query.validate_url("http://127.0.0.1:8003"), "http://127.0.0.1:8003"
        )

    def test_interactive_main_uses_a2a_stream_and_displays_token_progress_result(self):
        http = Mock()
        events = [
            task(),
            status("WORKING", "Investigating"),
            status("WORKING", "Checking errors", thought=True),
            {
                "artifactUpdate": {
                    "artifact": {
                        "artifactId": "result",
                        "parts": [{"text": "A pod is failing"}],
                    }
                }
            },
            status("COMPLETED"),
        ]
        stream = "".join(
            "data: " + json.dumps({"result": event}) + "\n\n" for event in events
        )
        http.open.side_effect = [
            Response(b'{"capabilities":{"streaming":true}}'),
            Response(stream.encode(), "text/event-stream"),
        ]
        with (
            patch.object(query, "Http", return_value=http),
            patch("builtins.input", side_effect=["1", "What failed?"]),
            patch.object(query.getpass, "getpass", return_value="token-used"),
        ):
            self.assertEqual(
                query.main(["--mas-url", "https://mas.example.test/a2a/jsonrpc"]), 0
            )
        display = self.stdout.getvalue()
        for expected in [
            "Token used (pasted):\ntoken-used",
            "[Progress] Investigating",
            "[Thinking] Checking errors",
            "Result:\nA pod is failing",
        ]:
            self.assertIn(expected, display)
        discovery, dispatch = [call.args[0] for call in http.open.call_args_list]
        self.assertEqual(
            discovery.full_url, "https://mas.example.test/.well-known/agent-card.json"
        )
        self.assertFalse(discovery.has_header("Authorization"))
        self.assertEqual(dispatch.get_header("Authorization"), "Bearer token-used")
        body = json.loads(dispatch.data)
        self.assertEqual(body["method"], "SendStreamingMessage")
        self.assertEqual(body["params"]["message"]["parts"], [{"text": "What failed?"}])


if __name__ == "__main__":
    unittest.main()
