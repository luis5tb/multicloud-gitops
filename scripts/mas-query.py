#!/usr/bin/env python3
"""Ask MAS a question over A2A 1.0, without running the ACME agent.

Uses only the Python standard library. See scripts/README.md for credentials,
SPIFFE token commands, and examples. The full bearer token is printed locally.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import math
import os
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

TERMINAL = {"COMPLETED", "FAILED", "CANCELED", "REJECTED"}


class QueryError(Exception):
    """A user-facing configuration, authentication, or protocol error."""


class Unauthorized(QueryError):
    """The server rejected the bearer token with HTTP 401."""


def say(text: str) -> None:
    print(text, flush=True)


def required(label: str, default: str = "", *, secret: bool = False) -> str:
    if default and secret:
        return default
    while True:
        prompt = f"{label}" + (f" [{default}]" if default else "") + ": "
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if value or default:
            return value or default
        say("A value is required.")


def choose(label: str, options: list[str], default: int = 1) -> int:
    say(label)
    for index, option in enumerate(options, 1):
        say(f"  {index}) {option}")
    while True:
        value = input(f"Choice [{default}]: ").strip() or str(default)
        if value.isdigit() and 1 <= int(value) <= len(options):
            return int(value)
        say(f"Choose 1–{len(options)}.")


def validate_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise QueryError("Use an HTTP(S) URL without credentials, query, or fragment.")
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise QueryError(
            "Use HTTPS for remote endpoints; HTTP is supported only on loopback."
        )
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward bearer tokens or OAuth credentials to redirected URLs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self, ca_bundle: str, timeout: float):
        context = ssl.create_default_context()
        if ca_bundle:
            context.load_verify_locations(cafile=ca_bundle)
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )
        self.timeout = timeout

    def open(self, request):
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            # OAuth descriptions explain service-account/credential failures.
            # Do not dump response bodies, which can echo sensitive input.
            detail = ""
            try:
                with error:
                    payload = json.loads(error.read())
                if isinstance(payload.get("error"), str):
                    detail = ": " + str(
                        payload.get("error_description") or payload["error"]
                    )
            except (ValueError, AttributeError):
                pass
            if error.code == 401:
                raise Unauthorized(
                    f"HTTP 401: token or client credentials rejected{detail}"
                ) from None
            raise QueryError(f"HTTP {error.code}{detail}") from None


def clean_token(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or any(char.isspace() for char in token):
        raise QueryError("Expected one nonempty bearer token without whitespace.")
    return token


@dataclass
class Credentials:
    http: Http
    token_command: str = ""
    provider: str = ""
    environment: str = ""
    token_url: str = ""
    form: dict = field(default_factory=dict)
    basic: str = ""
    assertion_file: str = ""
    token: str = ""

    def select(self) -> str:
        if choose("Token", ["Paste a token", "Get a token"]) == 1:
            self.provider = "pasted"
            return self.display(
                required("Paste token (or Bearer <token>)", secret=True)
            )
        selection = choose(
            "Token provider", ["Keycloak", "Red Hat SSO", "Red Hat SSO stage"]
        )
        self.provider = "keycloak" if selection == 1 else "redhat"
        self.environment = (
            "" if selection == 1 else ("production" if selection == 2 else "stage")
        )
        if not self.token_command:
            self.configure()
        return self.mint()

    def configure(self) -> None:
        prefix = (
            "KEYCLOAK"
            if self.provider == "keycloak"
            else f"REDHAT_SSO_{self.environment.upper()}"
        )
        self.token_url = validate_url(
            required("Token endpoint URL", os.getenv(prefix + "_TOKEN_URL", ""))
        )
        client_id = required("Client ID", os.getenv(prefix + "_CLIENT_ID", ""))
        self.form = {"grant_type": "client_credentials"}
        scope = os.getenv(
            "KEYCLOAK_SCOPE" if self.provider == "keycloak" else "REDHAT_SSO_SCOPE"
        )
        if scope is None:
            scope = "" if self.provider == "keycloak" else "openid api.ask_red_hat"
        if scope:
            self.form["scope"] = scope
        method = "client_secret_post"
        if self.provider == "keycloak":
            methods = [
                "client_secret_post",
                "client_secret_basic",
                "client_assertion_post",
            ]
            configured = os.getenv("KEYCLOAK_CLIENT_AUTH_METHOD", method)
            if configured not in methods:
                raise QueryError("Unsupported KEYCLOAK_CLIENT_AUTH_METHOD.")
            method = methods[
                choose(
                    "Keycloak client authentication",
                    methods,
                    methods.index(configured) + 1,
                )
                - 1
            ]
        if method == "client_assertion_post":
            self.assertion_file = required(
                "JWT-SVID/assertion file", os.getenv("ZTO_IDENTITY_TOKEN_FILE", "")
            )
            self.form["client_assertion_type"] = os.getenv(
                "KEYCLOAK_CLIENT_ASSERTION_TYPE",
                "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
            )
            # Keycloak resolves a SPIFFE assertion's client from sub. Supplying
            # client_id here fails when that sub differs from the OAuth ID.
        else:
            secret = required(
                "Client secret", os.getenv(prefix + "_CLIENT_SECRET", ""), secret=True
            )
            if method == "client_secret_basic":
                encoded_id = urllib.parse.quote_plus(client_id)
                encoded_secret = urllib.parse.quote_plus(secret)
                self.basic = base64.b64encode(
                    f"{encoded_id}:{encoded_secret}".encode()
                ).decode()
            else:
                self.form.update(client_id=client_id, client_secret=secret)

    def display(self, token: str) -> str:
        self.token = clean_token(token)
        source = self.provider + (f" {self.environment}" if self.environment else "")
        say(f"\nToken used ({source}):\n{self.token}\n")
        return self.token

    def mint(self) -> str:
        if self.token_command:
            # Split before substitution: each placeholder remains one argument;
            # no shell is involved and no bearer is placed on the command line.
            command = [
                arg.replace("{provider}", self.provider).replace(
                    "{environment}", self.environment
                )
                for arg in shlex.split(self.token_command)
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=60
            )
            if result.returncode:
                raise QueryError(f"Token command failed (exit {result.returncode}).")
            return self.display(result.stdout)
        form = dict(self.form)
        if self.assertion_file:
            form["client_assertion"] = Path(self.assertion_file).read_text().strip()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.basic:
            headers["Authorization"] = f"Basic {self.basic}"
        request = urllib.request.Request(
            self.token_url, data=urllib.parse.urlencode(form).encode(), headers=headers
        )
        with self.http.open(request) as response:
            payload = json.load(response)
        if not isinstance(payload.get("access_token"), str):
            raise QueryError("Token response did not contain an access_token.")
        return self.display(payload["access_token"])

    def renew(self) -> None:
        say("Fresh credentials required for the same caller principal.")
        if self.provider == "pasted":
            self.display(
                required("Paste a fresh token for the same principal", secret=True)
            )
        else:
            self.mint()


def sse_payloads(response):
    """Read SSE data, including comments, CRLF, multiline data and EOF."""
    lines = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if lines:
                yield json.loads("\n".join(lines))
                lines = []
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
    if lines:
        yield json.loads("\n".join(lines))


@dataclass
class Client:
    http: Http
    base_url: str
    credentials: Credentials

    def streaming(self) -> bool:
        request = urllib.request.Request(self.base_url + "/.well-known/agent-card.json")
        with self.http.open(request) as response:
            card = json.load(response)
        return bool(card.get("capabilities", {}).get("streaming", False))

    def rpc(self, method: str, params: dict):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": method,
                "params": params,
            }
        ).encode()
        for attempt in range(2):
            request = urllib.request.Request(
                self.base_url + "/a2a/jsonrpc",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream, application/json",
                    "Authorization": f"Bearer {self.credentials.token}",
                    "A2A-Version": "1.0",
                },
            )
            try:
                response = self.http.open(request)
                break
            except Unauthorized:
                if attempt:
                    raise
                self.credentials.renew()
        with response:
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                payloads = sse_payloads(response)
            else:
                payloads = [json.load(response)]
            for payload in payloads:
                if "error" in payload:
                    error = payload["error"]
                    raise QueryError(
                        f"A2A error {error.get('code')}: {error.get('message')}"
                    )
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise QueryError("A2A response is missing an object result.")
                yield result


def parts(node: dict) -> tuple[list[str], list[str]]:
    thoughts, answers = [], []
    for part in node.get("parts", []):
        metadata = part.get("metadata", {})
        if isinstance(part.get("text"), str):
            target = (
                thoughts
                if metadata.get("adk_thought") or metadata.get("is_thought")
                else answers
            )
            target.append(part["text"])
        elif "data" in part:
            answers.append(json.dumps(part["data"], indent=2))
    return thoughts, answers


@dataclass
class Output:
    task_id: str = ""
    context_id: str = ""
    state: str = ""
    status_answer: str = ""
    message_answer: str = ""
    pause_key: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    seen: set = field(default_factory=set)

    def show_parts(self, node: dict, *, progress: bool = False) -> str:
        thoughts, answers = parts(node)
        for label, texts in [
            ("Thinking", thoughts),
            ("Progress", answers if progress else []),
        ]:
            for text in texts:
                if text.strip() and (label, text) not in self.seen:
                    self.seen.add((label, text))
                    say(f"[{label}] {text}")
        return "\n".join(answers)

    def ingest(self, result: dict) -> None:
        node = next(
            (
                result[key]
                for key in ("task", "statusUpdate", "artifactUpdate", "message")
                if key in result
            ),
            result,
        )
        is_task = "id" in node and "status" in node
        task_id = node.get("id" if is_task else "taskId", "")
        if task_id and task_id != self.task_id:
            self.task_id = task_id
            say(f"Task: {task_id}")
        context_id = node.get("contextId", self.context_id)
        if context_id and context_id != self.context_id:
            self.context_id = context_id
            say(f"Context: {context_id}")
        if "status" in node:
            status = node["status"]
            state = (
                status.get("state", "")
                .removeprefix("TASK_STATE_")
                .upper()
                .replace("-", "_")
            )
            if state != self.state:
                self.state = state
                say(f"[State] {state}")
            if state in {"INPUT_REQUIRED", "AUTH_REQUIRED"}:
                message = status.get("message", {})
                self.pause_key = message.get("messageId") or json.dumps(
                    message, sort_keys=True
                )
            text = self.show_parts(
                status.get("message", {}), progress=state != "COMPLETED"
            )
            if state in TERMINAL:
                self.status_answer = text
        if is_task:
            for message in node.get("history", []):
                if message.get("role") in {"ROLE_AGENT", "agent"}:
                    text = self.show_parts(message)
                    if text:
                        self.message_answer = text
            if "artifacts" in node:
                self.artifacts.clear()
            for artifact in node.get("artifacts", []):
                self.artifacts[artifact.get("artifactId", "answer")] = self.show_parts(
                    artifact
                )
        elif "artifact" in node:
            artifact = node["artifact"]
            key = artifact.get("artifactId", "answer")
            text = self.show_parts(artifact)
            self.artifacts[key] = (
                self.artifacts.get(key, "") if node.get("append") else ""
            ) + text
        elif "parts" in node:
            self.message_answer = self.show_parts(node)

    def answer(self) -> str:
        return (
            "\n\n".join(filter(None, self.artifacts.values()))
            or self.status_answer
            or self.message_answer
        )


def message_params(text: str, output: Output | None = None) -> dict:
    message = {
        "messageId": uuid.uuid4().hex,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
        "metadata": {"ols_mode": os.getenv("OLS_A2A_MODE", "troubleshooting")},
    }
    if output:
        message.update(taskId=output.task_id, contextId=output.context_id)
    return {"message": message, "configuration": {"returnImmediately": True}}


def run_query(
    client: Client, question: str, timeout: float, poll_interval: float
) -> int:
    streaming = client.streaming()
    say("\nThinking / progress (as exposed by MAS):")
    output = Output()
    send_method = "SendStreamingMessage" if streaming else "SendMessage"
    method, params = send_method, message_params(question)
    deadline = time.monotonic() + timeout
    renewals = 0
    answered_pause = None
    while time.monotonic() < deadline:
        try:
            for result in client.rpc(method, params):
                output.ingest(result)
                if time.monotonic() >= deadline:
                    break
                # A resumed stream can start with the old paused snapshot.
                # Keep reading so the worker's next event can clear that pause.
                if output.state in TERMINAL:
                    break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if not output.task_id:
                raise QueryError(
                    "Connection lost before a task ID was received; submission may have succeeded."
                ) from None
            say(f"Connection interrupted; recovering task {output.task_id}.")
        if output.state in TERMINAL or (output.message_answer and not output.task_id):
            say("\nResult:\n" + (output.answer() or "(No result text returned.)"))
            return 0 if output.state in {"", "COMPLETED"} else 1
        if not output.task_id:
            raise QueryError("MAS returned neither a task ID nor an answer.")
        pause = (output.state, output.pause_key)
        if (
            output.state in {"INPUT_REQUIRED", "AUTH_REQUIRED"}
            and pause == answered_pause
        ):
            # A reply may be accepted while the snapshot still shows its old
            # gate. Reattach instead of asking for the same decision again.
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            method = "SubscribeToTask" if streaming else "GetTask"
            params = {"id": output.task_id}
        elif output.state == "AUTH_REQUIRED":
            renewals += 1
            if renewals > 2:
                raise QueryError(
                    "MAS still requires authentication after two renewals."
                )
            client.credentials.renew()
            answered_pause = pause
            method, params = (
                send_method,
                message_params("Renewed credentials attached.", output),
            )
            output.state = ""
        elif output.state == "INPUT_REQUIRED":
            reply = required(
                "MAS needs input (approve/reject, or requested information)"
            )
            answered_pause = pause
            method, params = send_method, message_params(reply, output)
            output.state = ""
        else:
            # Reattach after stream closure, including after approval/renewal.
            # Never infer completion from EOF, and never resend the question.
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            method = (
                "SubscribeToTask" if streaming and method == "GetTask" else "GetTask"
            )
            params = {"id": output.task_id}
    raise QueryError(
        f"Timed out; task {output.task_id or '(unknown)'} may still be running (context {output.context_id})."
    )


def positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mas-url",
        default=os.getenv("MAS_URL", ""),
        help="MAS A2A Route base URL (or MAS_URL)",
    )
    parser.add_argument(
        "--ca-bundle",
        default=os.getenv("A2A_CA_BUNDLE", ""),
        help="Additional trusted CA PEM bundle",
    )
    parser.add_argument(
        "--token-command",
        default=os.getenv("MAS_TOKEN_COMMAND", ""),
        help="Optional mint command, with {provider} and {environment} placeholders; no shell",
    )
    parser.add_argument(
        "--timeout",
        type=positive,
        default=1200,
        help="Overall query timeout in seconds (default: 1200)",
    )
    parser.add_argument(
        "--request-timeout",
        type=positive,
        default=330,
        help="Socket timeout in seconds (default: 330)",
    )
    parser.add_argument(
        "--poll-interval",
        type=positive,
        default=2,
        help="Poll/reconnect delay in seconds (default: 2)",
    )
    args = parser.parse_args(argv)
    try:
        http = Http(args.ca_bundle, args.request_timeout)
        credentials = Credentials(http, token_command=args.token_command)
        credentials.select()
        question = required(
            "Question to ask (include the cluster API URL for cluster investigations)"
        )
        url = args.mas_url or required("MAS A2A URL")
        base_url = (
            validate_url(url)
            .removesuffix("/a2a/jsonrpc")
            .removesuffix("/.well-known/agent-card.json")
        )
        return run_query(
            Client(http, base_url, credentials),
            question,
            args.timeout,
            args.poll_interval,
        )
    except (QueryError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "\nStopped; any submitted MAS task may still be running.", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
