"""Единый вызов LLM: локальная Ollama или Claude API, с опциональным JSON-ответом по схеме."""
from __future__ import annotations

import json
from typing import TypeVar

import anthropic
import requests
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


def _cap_arrays(node, limit: int = 20):
    """Для Ollama ограничиваем длину списков в схеме: без maxItems локальная модель
    при генерации по грамматике может бесконечно дописывать элементы."""
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
        raise LLMError("Ollama не запущена (ollama serve)")
    except requests.Timeout:
        raise LLMError("Локальная модель не ответила за 15 минут — попробуйте ещё раз или переключите агента на Claude")
    if r.status_code == 404:
        raise LLMError(f"В Ollama нет модели {settings['ollama_model']} (ollama pull {settings['ollama_model']})")
    r.raise_for_status()
    return r.json()["message"]["content"]


def _claude(settings: dict, system: str, user: str, schema: dict | None, effort: str) -> str:
    output_config: dict = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    try:
        client = anthropic.Anthropic()
        # Потоковый режим: длинные ответы не упираются в HTTP-таймауты
        with client.beta.messages.stream(
            model=settings["claude_model"],
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            # при отказе модели запрос автоматически повторяется на резервной модели
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            resp = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise LLMError("Claude API: нет или неверный ключ ANTHROPIC_API_KEY (Настройки → ключи)")
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"Claude API: нет доступа — {e.message}")
    except anthropic.RateLimitError:
        raise LLMError("Claude API: превышен лимит запросов, попробуйте через минуту")
    except anthropic.BadRequestError as e:
        raise LLMError(f"Claude API: некорректный запрос — {e.message}")
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API: ошибка сервера {e.status_code}")
    except anthropic.APIConnectionError:
        raise LLMError("Claude API: нет соединения с интернетом")
    except TypeError as e:
        # нет ни ключа, ни профиля `ant auth login`
        raise LLMError("Claude API не настроен: задайте ANTHROPIC_API_KEY во вкладке «Настройки» "
                       f"или переключите агента на ollama ({e})")

    if resp.stop_reason == "refusal":
        raise LLMError("Claude отказался выполнять запрос")
    if resp.stop_reason == "max_tokens":
        raise LLMError("Ответ Claude обрезан по лимиту длины")
    return "".join(b.text for b in resp.content if b.type == "text")


def ask_text(settings: dict, backend: str, system: str, user: str, effort: str = "medium") -> str:
    if backend == "claude":
        return _claude(settings, system, user, None, effort).strip()
    return _ollama(settings, system, user, None).strip()


def ask_json(settings: dict, backend: str, system: str, user: str, model: type[T], effort: str = "medium") -> T:
    schema = model.model_json_schema()
    raw = (_claude(settings, system, user, schema, effort) if backend == "claude"
           else _ollama(settings, system, user, schema))
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        # локальная модель иногда оборачивает JSON в текст — пробуем вытащить объект
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return model.model_validate(json.loads(raw[start:end + 1]))
            except (ValidationError, json.JSONDecodeError):
                pass
        raise LLMError(f"Модель вернула ответ не по схеме: {raw[:300]}")


def ollama_status(settings: dict) -> tuple[bool, str]:
    try:
        r = requests.get(f"{settings['ollama_url'].rstrip('/')}/api/tags", timeout=5)
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return False, "Ollama не отвечает"
    want = settings["ollama_model"]
    if want in names or f"{want}:latest" in names:
        return True, f"Ollama OK, модель {want}"
    return False, f"Ollama работает, но модели {want} нет. Есть: {', '.join(names)}"
