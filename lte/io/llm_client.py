"""
lte/io/llm_client.py -- SIDE-EFFECT BOUNDARY. The only module in lte/ that
opens a network connection.

A minimal client for an OpenAI-compatible `POST {base_url}/chat/completions`
endpoint, stdlib only (urllib), so the lte-cas image needs no extra
dependency. One request, one response, no retries, no streaming, no
background state: the process that calls it exits when the answer prints.

The API key is read from the environment by the caller and passed in; this
module never logs it and never writes it anywhere.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_TOKENS = 1024


class LlmClientError(RuntimeError):
    """Transport failure, HTTP error, or an unparseable response."""


@dataclass(frozen=True)
class ChatCompletionClient:
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS

    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def build_request(self, system: str, user: str) -> urllib.request.Request:
        body = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return urllib.request.Request(self.endpoint(), data=body, headers=headers, method="POST")

    def complete(self, system: str, user: str) -> str:
        request = self.build_request(system, user)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LlmClientError("HTTP %d from %s: %s" % (exc.code, self.endpoint(), detail)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmClientError("request to %s failed: %s" % (self.endpoint(), exc)) from exc
        return parse_completion(raw)


def parse_completion(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except (UnicodeDecodeError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmClientError("unexpected completion payload: %s" % exc) from exc
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise LlmClientError("completion content is not text")
    return content
