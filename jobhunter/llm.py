"""Single LLM call: local Ollama or the Claude API, with an optional schema-validated JSON response."""
from __future__ import annotations

import json
from typing import TypeVar

import anthropic
import requests
from pydantic import BaseModel, ValidationError
from .i18n import tr, ui_language_name

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


def _cap_arrays(node, limit: int = 20):
    """For Ollama, cap list lengths in the schema: without maxItems, the local model
    may keep generating items forever under grammar-constrained decoding."""
    if isinstance(node, dict):
        if node.get("type") == "array" and "maxItems" not in node:
            node["maxItems"] = limit
        for v in node.values():
            _cap_arrays(v, limit)
    elif isinstance(node, list):
        for v in node:
            _cap_arrays(v, limit)
    return node


def _ollama(settings: dict, system: str, user: str, schema: dict | None) -> str:
    body = {
        "model": settings["ollama_model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 16384, "num_predict": 6000, "repeat_penalty": 1.1},
    }
    if schema:
        body["format"] = _cap_arrays(json.loads(json.dumps(schema)))
    try:
        r = requests.post(f"{settings['ollama_url'].rstrip('/')}/api/chat", json=body, timeout=900)
    except requests.ConnectionError:
        raise LLMError(tr("Ollama is not running (ollama serve)"))
    except requests.Timeout:
        raise LLMError(tr("The local model did not answer within 15 minutes — try again or switch the agent "
                          "to Claude"))
    if r.status_code == 404:
        raise LLMError(tr("Ollama has no model {0} (ollama pull {1})", settings['ollama_model'], settings['ollama_model']))
    r.raise_for_status()
    return r.json()["message"]["content"]


def _claude(settings: dict, system: str, user: str, schema: dict | None, effort: str) -> str:
    output_config: dict = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    try:
        client = anthropic.Anthropic()
        # Streaming mode: long responses don't hit HTTP timeouts
        with client.beta.messages.stream(
            model=settings["claude_model"],
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            # if the model refuses, the request is retried automatically on a fallback model
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            resp = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise LLMError(tr("Claude API: missing or invalid ANTHROPIC_API_KEY (Settings → keys)"))
    except anthropic.PermissionDeniedError as e:
        raise LLMError(tr("Claude API: access denied — {0}", e.message))
    except anthropic.RateLimitError:
        raise LLMError(tr("Claude API: rate limit exceeded, try again in a minute"))
    except anthropic.BadRequestError as e:
        raise LLMError(tr("Claude API: bad request — {0}", e.message))
    except anthropic.APIStatusError as e:
        raise LLMError(tr("Claude API: server error {0}", e.status_code))
    except anthropic.APIConnectionError:
        raise LLMError(tr("Claude API: no internet connection"))
    except TypeError as e:
        # neither an API key nor an `ant auth login` profile
        raise LLMError(tr("Claude API is not configured: set ANTHROPIC_API_KEY in the «Settings» tab or "
                          "switch the agent to ollama ({0})", e))

    if resp.stop_reason == "refusal":
        raise LLMError(tr("Claude refused the request"))
    if resp.stop_reason == "max_tokens":
        raise LLMError(tr("Claude's answer was cut off by the length limit"))
    return "".join(b.text for b in resp.content if b.type == "text")


def _with_ui_language(system: str) -> str:
    # prompts say "in the interface language": tell the model which one it is
    return (f"{system}\n\nInterface language: {ui_language_name()}. "
            "Write all explanations for the user in this language.")


def ask_text(settings: dict, backend: str, system: str, user: str, effort: str = "medium") -> str:
    system = _with_ui_language(system)
    if backend == "claude":
        return _claude(settings, system, user, None, effort).strip()
    return _ollama(settings, system, user, None).strip()


def ask_json(settings: dict, backend: str, system: str, user: str, model: type[T], effort: str = "medium") -> T:
    system = _with_ui_language(system)
    schema = model.model_json_schema()
    raw = (_claude(settings, system, user, schema, effort) if backend == "claude"
           else _ollama(settings, system, user, schema))
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        # the local model sometimes wraps JSON in text — try to extract the object
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return model.model_validate(json.loads(raw[start:end + 1]))
            except (ValidationError, json.JSONDecodeError):
                pass
        raise LLMError(tr("The model returned an answer that does not match the schema: {0}", raw[:300]))


def ollama_status(settings: dict) -> tuple[bool, str]:
    try:
        r = requests.get(f"{settings['ollama_url'].rstrip('/')}/api/tags", timeout=5)
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return False, tr("Ollama is not responding")
    want = settings["ollama_model"]
    if want in names or f"{want}:latest" in names:
        return True, tr("Ollama OK, model {0}", want)
    return False, tr("Ollama is running, but model {0} is missing. Available: {1}", want, ', '.join(names))
