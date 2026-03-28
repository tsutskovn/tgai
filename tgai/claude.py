"""LLM abstraction layer for tgai.

Despite the historical filename, this module is not Anthropic-only anymore.
It contains:
- the shared prompt and history logic used by every provider
- provider-specific adapters
- dependency checks and optional auto-install helpers
- fallback selection when multiple providers are configured
"""

from __future__ import annotations

import re
import sys
import threading
import io
from contextlib import redirect_stdout, redirect_stderr
from queue import Queue, Empty
from typing import Any, Callable, Optional


MAX_MSG_CHARS = 500  # truncate individual messages before sending to API

CONTEXT_LEVELS = {
    "base": 5,
    "medium": 10,
    "extended": 20,
}
DEFAULT_CONTEXT_LEVEL = "base"


def _truncate_msg(text: str) -> str:
    """Truncate a message to save tokens."""
    if len(text) <= MAX_MSG_CHARS:
        return text
    return text[:MAX_MSG_CHARS] + "… [обрезано]"


def _call_with_timeout(fn, timeout_seconds: float):
    """Run a blocking callable in a daemon thread and wait up to timeout_seconds."""
    q: Queue = Queue(maxsize=1)

    def _runner():
        try:
            q.put((True, fn()))
        except Exception as exc:
            q.put((False, exc))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    try:
        ok, value = q.get(timeout=timeout_seconds)
    except Empty as exc:
        raise TimeoutError(f"timeout after {timeout_seconds:.1f}s") from exc
    if ok:
        return value
    raise value


def _g4f_create_silently(g4f_module, model, messages, provider, timeout):
    """Run g4f completion while swallowing noisy provider stdout/stderr."""
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return g4f_module.ChatCompletion.create(
            model=model,
            messages=messages,
            provider=provider,
            timeout=timeout,
        )

# Provider → pip package name
_PROVIDER_DEPS: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openrouter": "openai",
    "gemini": "google-genai",
    "google": "google-genai",
    "g4f": "g4f",
    "yandexgpt": "",  # stdlib only, no extra package
}

# Provider → import check
_PROVIDER_IMPORTS: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openrouter": "openai",
    "gemini": "google.genai",
    "google": "google.genai",
    "g4f": "g4f",
    "yandexgpt": "",  # no import needed
}


class InsufficientCreditsError(Exception):
    """Raised when the API key has no remaining credits."""


def check_provider_deps(provider: str) -> bool:
    """Check if the required package for a provider is installed."""
    import importlib
    mod = _PROVIDER_IMPORTS.get(provider)
    if mod is None:
        return False  # unknown provider
    if not mod:
        return True  # no package needed (e.g. yandexgpt uses stdlib)
    try:
        importlib.import_module(mod)
        return True
    except ImportError:
        return False


def ensure_provider_deps(provider: str) -> None:
    """Install missing dependencies for a provider via pip."""
    if check_provider_deps(provider):
        return
    pkg = _PROVIDER_DEPS.get(provider, "")
    if not pkg:
        return  # no package needed
    print(f"Устанавливаю зависимость для {provider}: {pkg}...")
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", pkg],
        stdout=subprocess.DEVNULL,
    )
    print(f"  {pkg} установлен.")


# ======================================================================
# Base class — all prompt logic lives here
# ======================================================================

class LLMClient:
    """
    Base class for all tgai providers.

    Subclasses only need to implement the low-level completion methods.
    Everything above that level stays shared:
    - chat history handling
    - summarization prompts
    - reply drafting
    - reformulation
    - tool-calling fallback behavior
    """

    provider_name: str = "unknown"

    def __init__(self, model: str, context_level: str = DEFAULT_CONTEXT_LEVEL) -> None:
        self.model = model
        self.max_history = CONTEXT_LEVELS.get(context_level, CONTEXT_LEVELS[DEFAULT_CONTEXT_LEVEL])
        self._histories: dict[int, list[dict]] = {}

    @property
    def display_name(self) -> str:
        """Human-readable name showing model, e.g. 'claude-sonnet-4-6' or 'gpt-4o'."""
        return self.model or self.provider_name

    # --- subclass overrides -------------------------------------------

    def _complete(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 1024,
    ) -> str:
        raise NotImplementedError

    def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
        max_tokens: int = 2048,
    ) -> Any:
        raise NotImplementedError

    def _prompt_based_tool_calling(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
        max_tokens: int = 2048,
    ) -> Any:
        """Simulate tool calling via prompt engineering for providers without native support.

        Returns an OpenAI-compatible response object (SimpleNamespace).
        """
        import json as _json
        import re
        from types import SimpleNamespace

        tool_list = _json.dumps([{
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {}),
        } for t in tools], ensure_ascii=False, indent=2)

        tool_system = f"""{system}

У тебя есть инструменты. Чтобы вызвать инструмент, ответь ТОЛЬКО одним JSON объектом:
{{"tool_call": {{"name": "имя_инструмента", "arguments": {{...}}}}}}

Если инструмент не нужен, отвечай обычным текстом БЕЗ JSON.

Доступные инструменты:
{tool_list}"""

        # Convert messages: role=tool → user, strip tool_calls from assistant
        converted = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "tool":
                tid = m.get("tool_call_id", "?")
                converted.append({"role": "user", "content": f"[Результат инструмента {tid}]:\n{content}"})
            elif role == "assistant" and m.get("tool_calls"):
                tcs = m["tool_calls"]
                parts = []
                for tc in (tcs if isinstance(tcs, list) else [tcs]):
                    fn = tc.get("function", tc) if isinstance(tc, dict) else getattr(tc, "function", tc)
                    fname = fn.get("name", "?") if isinstance(fn, dict) else getattr(fn, "name", "?")
                    fargs = fn.get("arguments", "{}") if isinstance(fn, dict) else getattr(fn, "arguments", "{}")
                    parts.append(f'{{"tool_call": {{"name": "{fname}", "arguments": {fargs}}}}}')
                converted.append({"role": "assistant", "content": "\n".join(parts)})
            elif role == "model":
                converted.append({"role": "assistant", "content": str(content)})
            else:
                converted.append({"role": role, "content": str(content) if content else ""})

        text = self._complete(converted, system=tool_system, max_tokens=max_tokens)

        # Try to parse tool call from response — find JSON with balanced braces
        try:
            # Strip markdown code fences before parsing
            text_clean = re.sub(r'```[a-z]*\n?', '', text).strip()
            # Find start of JSON object containing "tool_call"
            idx = text_clean.find('"tool_call"')
            if idx >= 0:
                # Walk backwards to find opening {
                start = text_clean.rfind('{', 0, idx)
                if start >= 0:
                    # Find matching closing brace (handle truncated JSON)
                    depth = 0
                    end = -1
                    for ci in range(start, len(text_clean)):
                        if text_clean[ci] == '{':
                            depth += 1
                        elif text_clean[ci] == '}':
                            depth -= 1
                            if depth == 0:
                                end = ci + 1
                                break
                    if end == -1:
                        # JSON truncated — complete with missing closing braces
                        json_str = text_clean[start:] + '}' * depth
                    else:
                        json_str = text_clean[start:end]
                    parsed = _json.loads(json_str)
                    tc = parsed.get("tool_call", {})
                    if tc.get("name"):
                        tool_call = SimpleNamespace(
                            id=f"call_{tc['name']}",
                            function=SimpleNamespace(
                                name=tc["name"],
                                arguments=_json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                            ),
                            type="function",
                        )
                        msg = SimpleNamespace(content=None, tool_calls=[tool_call], role="assistant")
                        choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
                        return SimpleNamespace(choices=[choice])
        except Exception:
            pass

        msg = SimpleNamespace(content=text, tool_calls=None, role="assistant")
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        return SimpleNamespace(choices=[choice])

    # --- history management -------------------------------------------

    def _trim_history(self, history: list[dict]) -> list[dict]:
        return history[-self.max_history:]

    def get_history(self, chat_id: int) -> list[dict]:
        return self._histories.get(chat_id, [])

    def set_history(self, chat_id: int, history: list[dict]) -> None:
        self._histories[chat_id] = self._trim_history(history)

    def clear_history(self, chat_id: int) -> None:
        self._histories.pop(chat_id, None)

    # --- core ask -----------------------------------------------------

    def ask(
        self,
        chat_id: int,
        message: str,
        system: str = "",
        history: Optional[list[dict]] = None,
    ) -> str:
        if history is not None:
            self._histories[chat_id] = self._trim_history(history)

        current = self._histories.get(chat_id, [])
        current = current + [{"role": "user", "content": message}]

        reply = self._complete(current, system=system)

        updated = current + [{"role": "assistant", "content": reply}]
        self._histories[chat_id] = self._trim_history(updated)
        return reply

    # --- propose reply ------------------------------------------------

    def propose_reply(
        self,
        incoming_message: str,
        chat_history: list[dict],
        persona: str,
    ) -> str:
        history_text = ""
        if chat_history:
            lines = []
            for msg in chat_history[-self.max_history:]:
                lines.append(f"{msg.get('sender', '?')}: {_truncate_msg(msg.get('text', ''))}")
            history_text = "\nИстория переписки:\n" + "\n".join(lines) + "\n"

        prompt = (
            f"{history_text}\n"
            f"Новое входящее сообщение:\n{_truncate_msg(incoming_message)}\n\n"
            "Предложи естественный ответ — коротко, по-человечески, без формальностей. "
            "Пиши только текст ответа, без пояснений и кавычек."
        )
        return self._complete(
            [{"role": "user", "content": prompt}],
            system=persona,
            max_tokens=256,
        ).strip()

    # --- draft reply (user provides hint/draft) -----------------------

    def draft_reply(
        self,
        user_draft: str,
        chat_history: list[dict],
        persona: str,
    ) -> str:
        """
        User typed a draft or instruction; Claude turns it into a polished reply
        taking into account the conversation context.
        """
        history_text = ""
        if chat_history:
            lines = []
            for msg in chat_history[-self.max_history:]:
                lines.append(f"{msg.get('sender', '?')}: {_truncate_msg(msg.get('text', ''))}")
            history_text = "Переписка:\n" + "\n".join(lines) + "\n\n"

        prompt = (
            f"{history_text}"
            f"Мой набросок ответа: {user_draft}\n\n"
            "Улучши и допиши мой ответ — естественно, по-человечески, без лишних деталей. "
            "Сохрани мой стиль. Пиши только готовый текст, без пояснений."
        )
        return self._complete(
            [{"role": "user", "content": prompt}],
            system=persona,
            max_tokens=256,
        ).strip()

    # --- reformulate --------------------------------------------------

    def reformulate(self, original: str, instruction: str, persona: str) -> str:
        prompt = (
            f"Переформулируй следующее сообщение согласно инструкции.\n\n"
            f"Исходное сообщение:\n{original}\n\n"
            f"Инструкция: {instruction}\n\n"
            "Напиши только переформулированный текст, без пояснений."
        )
        return self._complete(
            [{"role": "user", "content": prompt}],
            system=persona,
            max_tokens=512,
        ).strip()

    # --- inline comments ----------------------------------------------

    def process_inline_comments(
        self, message: str, context: str, persona: str
    ) -> str:
        comments = re.findall(r"\{([^}]+)\}", message)
        clean_message = re.sub(r"\s*\{[^}]+\}", "", message).strip()

        if not comments:
            return clean_message

        comments_text = "; ".join(comments)
        # Truncate context to save tokens
        truncated_context = context[:1000] + "…" if len(context) > 1000 else context
        prompt = (
            f"Пользователь хочет отправить сообщение: «{clean_message}»\n\n"
            f"В фигурных скобках — инструкции для тебя (НЕ для собеседника): {comments_text}\n"
            f"Контекст переписки: {truncated_context}\n\n"
            "Выполни инструкции и встрой результат в сообщение. "
            "Если инструкция требует информации, которой у тебя нет "
            "(например из другого чата) — оставь эту часть как вопрос собеседнику. "
            "Пиши только готовое сообщение, естественно и по-человечески."
        )
        return self._complete(
            [{"role": "user", "content": prompt}],
            system=persona,
            max_tokens=512,
        ).strip()

    # --- image description --------------------------------------------

    def describe_image(self, image_bytes: bytes, prompt: str = "") -> str:
        """Analyze an image and return a text description. Subclasses should override."""
        raise NotImplementedError("Этот провайдер не поддерживает анализ изображений.")

    def ocr_image(self, image_bytes: bytes) -> str:
        """Extract text from image. Subclasses should override."""
        return ""

    def clean_ocr_text(self, raw_text: str) -> str:
        """Refine messy OCR text into a coherent summary. Subclasses should override."""
        return ""

    # --- summarize ----------------------------------------------------

    def summarize_messages(self, messages: list[dict]) -> str:
        if not messages:
            return "Нет сообщений."

        lines = []
        for msg in messages:
            sender = msg.get("sender", "?")
            text = _truncate_msg(msg.get("text", ""))
            date_str = msg.get("date", "")
            if date_str:
                lines.append(f"[{date_str}] {sender}: {text}")
            else:
                lines.append(f"{sender}: {text}")

        prompt = (
            "Вот переписка из Telegram:\n\n" + "\n".join(lines) + "\n\n"
            "Сделай краткий лаконичный дайджест по-русски.\n"
            "Пиши просто саммари без отдельных секций, без строки «Важно:», "
            "без заголовков и без служебных пометок.\n"
            "Если в переписке есть срочность, просьба, договоренность или план, "
            "включи это прямо в основной текст саммари."
        )
        return self._complete(
            [{"role": "user", "content": prompt}],
            system="Ты — помощник, который составляет краткие дайджесты переписки.",
            max_tokens=300,
        ).strip()


class NoAIClient(LLMClient):
    """Sentinel client used when YandexGPT is not configured."""

    provider_name = "none"

    def __init__(self) -> None:
        super().__init__(model="disabled", context_level=DEFAULT_CONTEXT_LEVEL)

    @property
    def display_name(self) -> str:
        return "Без ИИ"

    def _complete(self, messages, system="", max_tokens=1024) -> str:
        raise RuntimeError("AI-функции доступны только с подключенным YandexGPT.")

    def _complete_with_tools(self, messages, tools, system="", max_tokens=2048):
        raise RuntimeError("AI-функции доступны только с подключенным YandexGPT.")


# ======================================================================
# Anthropic
# ======================================================================

class AnthropicClient(LLMClient):
    """Anthropic Claude API."""

    provider_name = "anthropic"
    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(self, api_key: str, model: str | None = None, context_level: str = DEFAULT_CONTEXT_LEVEL) -> None:
        import anthropic
        super().__init__(model or self.DEFAULT_MODEL, context_level)
        self.client = anthropic.Anthropic(api_key=api_key)

    def _check_credits(self, exc: Exception) -> None:
        if "credit balance is too low" in str(exc):
            raise InsufficientCreditsError(
                "Anthropic: баланс исчерпан"
            ) from exc

    def _complete(self, messages, system="", max_tokens=1024) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "messages": messages,
        }
        if system:
            kwargs["system"] = system
        try:
            response = self.client.messages.create(**kwargs)
        except Exception as exc:
            self._check_credits(exc)
            raise
        return response.content[0].text

    def _complete_with_tools(self, messages, tools, system="", max_tokens=2048):
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens,
            "messages": messages, "tools": tools,
        }
        if system:
            kwargs["system"] = system
        try:
            return self.client.messages.create(**kwargs)
        except Exception as exc:
            self._check_credits(exc)
            raise


# ======================================================================
# OpenAI-compatible
# ======================================================================

class OpenAIClient(LLMClient):
    """OpenAI-compatible API (OpenAI, OpenRouter, Ollama, vLLM, etc.)."""

    provider_name = "openai"
    DEFAULT_MODEL = "gpt-4o"

    def __init__(self, api_key: str, model: str | None = None, base_url: str | None = None, context_level: str = DEFAULT_CONTEXT_LEVEL) -> None:
        from openai import OpenAI
        super().__init__(model or self.DEFAULT_MODEL, context_level)
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)

    def _check_credits(self, exc: Exception) -> None:
        err = str(exc).lower()
        # Only match specific quota/billing exhaustion errors, not transient ones
        if ("insufficient_quota" in err
                or "exceeded your current quota" in err
                or "billing hard limit" in err
                or "account deactivated" in err):
            raise InsufficientCreditsError("OpenAI: квота исчерпана") from exc

    def _complete(self, messages, system="", max_tokens=1024) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        try:
            response = self.client.chat.completions.create(
                model=self.model, max_tokens=max_tokens, messages=msgs,
            )
        except Exception as exc:
            self._check_credits(exc)
            raise
        return response.choices[0].message.content

    def _complete_with_tools(self, messages, tools, system="", max_tokens=2048):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        oai_tools = [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        } for t in tools]
        try:
            return self.client.chat.completions.create(
                model=self.model, max_tokens=max_tokens, messages=msgs, tools=oai_tools,
            )
        except Exception as exc:
            self._check_credits(exc)
            raise


# ======================================================================
# Google Gemini
# ======================================================================

class GeminiClient(LLMClient):
    """Google Gemini API via google-genai SDK."""

    provider_name = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, api_key: str, model: str | None = None, context_level: str = DEFAULT_CONTEXT_LEVEL) -> None:
        from google import genai
        super().__init__(model or self.DEFAULT_MODEL, context_level)
        self.client = genai.Client(api_key=api_key)

    def _check_credits(self, exc: Exception) -> None:
        err = str(exc).lower()
        if "quota" in err or "billing" in err or "exceeded" in err:
            raise InsufficientCreditsError("Gemini: квота исчерпана") from exc

    def _complete(self, messages, system="", max_tokens=1024) -> str:
        config: dict[str, Any] = {"max_output_tokens": max_tokens}
        if system:
            config["system_instruction"] = system
        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=contents, config=config,
            )
        except Exception as exc:
            self._check_credits(exc)
            raise
        return response.text

    def _complete_with_tools(self, messages, tools, system="", max_tokens=2048):
        config: dict[str, Any] = {"max_output_tokens": max_tokens}
        if system:
            config["system_instruction"] = system
        func_decls = []
        for tool in tools:
            schema = tool.get("input_schema", {})
            props = schema.get("properties", {})
            gemini_props = {}
            for name, prop in props.items():
                gemini_props[name] = {
                    "type": prop.get("type", "string").upper(),
                    "description": prop.get("description", ""),
                }
            func_decls.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": {
                    "type": "OBJECT",
                    "properties": gemini_props,
                    "required": schema.get("required", []),
                },
            })
        config["tools"] = [{"function_declarations": func_decls}]
        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            if isinstance(msg.get("content"), list):
                parts = []
                for item in msg["content"]:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        parts.append({"function_response": {
                            "name": item.get("tool_use_id", "unknown"),
                            "response": {"result": item.get("content", "")},
                        }})
                    else:
                        parts.append({"text": str(item)})
                contents.append({"role": role, "parts": parts})
            else:
                contents.append({"role": role, "parts": [{"text": str(msg["content"])}]})
        try:
            return self.client.models.generate_content(
                model=self.model, contents=contents, config=config,
            )
        except Exception as exc:
            self._check_credits(exc)
            raise


# ======================================================================
# Fallback wrapper — auto-switch on credits exhaustion
# ======================================================================

class FallbackClient(LLMClient):
    """
    Wraps multiple LLMClients.  On InsufficientCreditsError, switches
    to the next provider and retries transparently.
    """

    def __init__(self, clients: list[LLMClient]) -> None:
        assert clients, "Нужен хотя бы один LLM-клиент"
        super().__init__(clients[0].model)
        self._clients = clients
        self._idx = 0

    @property
    def _active(self) -> LLMClient:
        return self._clients[self._idx]

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        return self._active.provider_name

    @property
    def display_name(self) -> str:
        return self._active.display_name

    # Proxy history to active client
    def get_history(self, chat_id):
        return self._active.get_history(chat_id)

    def set_history(self, chat_id, history):
        # Sync to all clients so fallback preserves context
        for c in self._clients:
            c.set_history(chat_id, history)

    def clear_history(self, chat_id):
        for c in self._clients:
            c.clear_history(chat_id)

    def _switch(self) -> bool:
        """Try to switch to next provider.  Returns False if none left."""
        if self._idx + 1 >= len(self._clients):
            return False
        old = self._active.provider_name
        self._idx += 1
        new = self._active.provider_name
        self.model = self._active.model
        print(f"\n{old} исчерпан -> переключаюсь на {new} ({self._active.model})\n")
        return True

    def _complete(self, messages, system="", max_tokens=1024) -> str:
        while True:
            try:
                return self._active._complete(messages, system, max_tokens)
            except InsufficientCreditsError:
                if not self._switch():
                    raise

    def _complete_with_tools(self, messages, tools, system="", max_tokens=2048):
        while True:
            try:
                return self._active._complete_with_tools(messages, tools, system, max_tokens)
            except InsufficientCreditsError:
                if not self._switch():
                    raise

    # Override ask to route through active client's history
    def ask(self, chat_id, message, system="", history=None):
        while True:
            try:
                return self._active.ask(chat_id, message, system, history)
            except InsufficientCreditsError:
                if not self._switch():
                    raise
                # Copy history to new active client
                hist = self._clients[self._idx - 1].get_history(chat_id)
                if hist:
                    self._active.set_history(chat_id, hist)


# ======================================================================
# GPT4Free (g4f) — free, no API key needed
# ======================================================================

class G4FClient(LLMClient):
    """GPT4Free — free AI models, no API key required."""

    provider_name = "g4f"
    DEFAULT_MODEL = "gpt-4o"
    _PROVIDER_TIMEOUT = 12.0

    def __init__(self, model: str | None = None, context_level: str = DEFAULT_CONTEXT_LEVEL, **kwargs) -> None:
        super().__init__(model or self.DEFAULT_MODEL, context_level)

    def _ensure_installed(self) -> None:
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                import g4f  # noqa: F401
        except ImportError:
            import subprocess
            print("Устанавливаю g4f...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "g4f"],
                                  stdout=subprocess.DEVNULL)

    # Free providers tried in order (no auth/cookies required)
    _FREE_PROVIDERS = [
        "PollinationsAI",
        "DDGS",
        "You",
        "Copilot",
        "TeachAnything",
        "Yqcloud",
    ]

    def _complete(self, messages, system="", max_tokens=1024) -> str:
        self._ensure_installed()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            import g4f
            import g4f.Provider as _Providers
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        # Use g4f default model (auto-selects working free model)
        g4f_model = g4f.models.default
        last_exc: Exception = RuntimeError("g4f: нет доступных провайдеров")
        for pname in self._FREE_PROVIDERS:
            provider = getattr(_Providers, pname, None)
            if provider is None:
                continue
            try:
                resp = _call_with_timeout(
                    lambda p=provider: _g4f_create_silently(
                        g4f, g4f_model, msgs, p, int(self._PROVIDER_TIMEOUT)
                    ),
                    self._PROVIDER_TIMEOUT + 1.0,
                )
                if isinstance(resp, str):
                    return resp
                return str(resp)
            except Exception as exc:
                last_exc = exc
                continue
        raise RuntimeError(f"g4f ошибка: {last_exc}") from last_exc

    def _complete_with_tools(self, messages, tools, system="", max_tokens=2048):
        return self._prompt_based_tool_calling(messages, tools, system, max_tokens)


# ======================================================================
# Yandex GPT (REST API, stdlib only)
# ======================================================================

class YandexGPTClient(LLMClient):
    """Yandex GPT via Yandex Foundation Models REST API."""

    provider_name = "yandexgpt"
    DEFAULT_MODEL = "yandexgpt-lite"
    _BASE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(self, api_key: str, folder_id: str, model: str | None = None, context_level: str = DEFAULT_CONTEXT_LEVEL, **kwargs) -> None:
        super().__init__(model or self.DEFAULT_MODEL, context_level)
        self.api_key = api_key
        self.folder_id = folder_id

    def _complete(self, messages, system="", max_tokens=1024) -> str:
        import json as _json
        import urllib.error
        import urllib.request

        model_uri = f"gpt://{self.folder_id}/{self.model}"
        msgs = []
        if system:
            msgs.append({"role": "system", "text": system})
        for m in messages:
            msgs.append({"role": m["role"], "text": m["content"]})

        body = _json.dumps({
            "modelUri": model_uri,
            "completionOptions": {"stream": False, "temperature": 0.6, "maxTokens": str(max_tokens)},
            "messages": msgs,
        }).encode("utf-8")

        req = urllib.request.Request(
            self._BASE_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {self.api_key}",
                "x-folder-id": self.folder_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403, 402, 429):
                raise InsufficientCreditsError(f"YandexGPT {exc.code}: {body_text[:120]}") from exc
            raise RuntimeError(f"YandexGPT HTTP {exc.code}: {body_text[:200]}") from exc
        except Exception as exc:
            raise RuntimeError(f"YandexGPT ошибка: {exc}") from exc

        try:
            return data["result"]["alternatives"][0]["message"]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"YandexGPT: неожиданный ответ: {data}") from exc

    def describe_image(self, image_bytes: bytes, prompt: str = "") -> str:
        """Analyze an image with YandexGPT Vision."""
        import json as _json
        import urllib.error
        import urllib.request
        import base64

        # Use vision model
        model_uri = f"gpt://{self.folder_id}/yandexgpt-vision/latest"
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        prompt_text = prompt or "Опиши кратко, что на этой картинке. Будь лаконичен, от 6 до 20 слов. Пиши по-русски."
        
        body = _json.dumps({
            "modelUri": model_uri,
            "completionOptions": {"stream": False, "temperature": 0.2, "maxTokens": "500"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image", "image": image_b64}
                    ]
                }
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            self._BASE_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {self.api_key}",
                "x-folder-id": self.folder_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            return data["result"]["alternatives"][0]["message"]["text"].strip()
        except Exception:
            return ""

    def ocr_image(self, image_bytes: bytes) -> str:
        """Extract text from image using Yandex Vision OCR."""
        import json as _json
        import urllib.request
        import base64

        url = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        body = _json.dumps({
            "folderId": self.folder_id,
            "analyze_specs": [{
                "content": image_b64,
                "features": [{
                    "type": "TEXT_DETECTION",
                    "text_detection_config": {"language_codes": ["ru", "en"]}
                }]
            }]
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {self.api_key}"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
                # Extract all text lines
                results = data.get("results", [])
                if not results: return ""
                
                text_blocks = []
                pages = results[0].get("results", [{}])[0].get("textDetection", {}).get("pages", [])
                for page in pages:
                    for block in page.get("blocks", []):
                        for line in block.get("lines", []):
                            line_text = " ".join([w.get("text", "") for w in line.get("words", [])])
                            if line_text: text_blocks.append(line_text)
                
                return "\n".join(text_blocks).strip()
        except Exception:
            return ""

    def clean_ocr_text(self, raw_text: str) -> str:
        """Refine messy OCR text into a coherent summary using LLM."""
        if not raw_text or len(raw_text) < 3:
            return ""
            
        prompt = (
            f"Ниже приведен сырой текст после распознавания картинки (OCR). "
            f"Сделай его логичным и связным. Исправь ошибки, если они очевидны. "
            f"Напиши результат ОДНОЙ строкой (до 10-12 слов). "
            f"Если в тексте полная бессмыслица или нет смысла — не пиши ничего, верни пустую строку.\n\n"
            f"Текст:\n{raw_text}"
        )
        try:
            # Use standard completion for this
            return self._complete([{"role": "user", "content": prompt}], max_tokens=100).strip()
        except Exception:
            return ""

    def _complete_with_tools(self, messages, tools, system="", max_tokens=2048):
        return self._prompt_based_tool_calling(messages, tools, system, max_tokens)


def validate_yandex_credentials(
    api_key: str,
    folder_id: str,
    model: str | None = None,
    timeout_seconds: float = 12.0,
) -> tuple[bool, str]:
    """Validate YandexGPT credentials with a small live completion request."""
    key = (api_key or "").strip()
    folder = (folder_id or "").strip()
    chosen_model = (model or YandexGPTClient.DEFAULT_MODEL).strip() or YandexGPTClient.DEFAULT_MODEL

    if not key:
        return False, "Не указан API ключ YandexGPT."
    if not folder:
        return False, "Не указан folder_id YandexGPT."

    client = YandexGPTClient(api_key=key, folder_id=folder, model=chosen_model)
    try:
        _call_with_timeout(
            lambda: client._complete(
                [{"role": "user", "content": "ping"}],
                max_tokens=8,
            ),
            timeout_seconds,
        )
        return True, ""
    except InsufficientCreditsError as exc:
        text = str(exc)
        if "Unknown api key" in text:
            return False, "Ошибка настройки YandexGPT: неверный API ключ."
        return False, f"Ошибка настройки YandexGPT: {text}"
    except Exception as exc:
        text = str(exc)
        if "folder" in text.lower() and ("not found" in text.lower() or "invalid" in text.lower()):
            return False, "Ошибка настройки YandexGPT: неверный folder_id."
        return False, f"Ошибка настройки YandexGPT: {text}"


# ======================================================================
# Factory
# ======================================================================

# Backwards compat alias
ClaudeClient = AnthropicClient

class OpenRouterClient(OpenAIClient):
    """OpenRouter API — same as OpenAI but with distinct provider_name."""
    provider_name = "openrouter"
    DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"


PROVIDERS = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "openrouter": OpenRouterClient,
    "gemini": GeminiClient,
    "google": GeminiClient,
    "g4f": G4FClient,
    "yandexgpt": YandexGPTClient,
}

# Priority order for fallback (best quality → cheapest)
DEFAULT_PRIORITY = ["anthropic", "openai", "gemini", "openrouter"]


def _build_client(provider: str, cfg: dict, context_level: str = DEFAULT_CONTEXT_LEVEL) -> LLMClient:
    """Instantiate a single provider client from its config block."""
    api_key = cfg.get("api_key", "")
    model = cfg.get("model")
    base_url = cfg.get("base_url")

    if provider == "openrouter" and not base_url:
        base_url = "https://openrouter.ai/api/v1"

    cls = PROVIDERS[provider]
    kwargs: dict[str, Any] = {"context_level": context_level}
    if model:
        kwargs["model"] = model
    # g4f doesn't need api_key; others do
    if provider not in ("g4f",):
        if not api_key:
            raise ValueError(f"Не задан api_key для {provider}")
        kwargs["api_key"] = api_key
    if base_url and provider not in ("anthropic", "gemini", "google", "g4f", "yandexgpt"):
        kwargs["base_url"] = base_url
    # YandexGPT requires folder_id
    if provider == "yandexgpt":
        folder_id = cfg.get("folder_id", "")
        if not folder_id:
            raise ValueError("Не задан folder_id для YandexGPT")
        kwargs["folder_id"] = folder_id
    return cls(**kwargs)


def create_llm_client(config: dict) -> LLMClient:
    """
    Create an LLM client (possibly with fallback) from config.

    Config formats:

    Single provider (legacy):
        {"anthropic": {"api_key": "..."}}

    Single provider (new):
        {"llm": {"provider": "gemini", "api_key": "...", "model": "gemini-2.5-flash"}}

    Multiple providers with auto-fallback:
        {"llm": [
            {"provider": "anthropic", "api_key": "..."},
            {"provider": "gemini", "api_key": "...", "model": "gemini-2.5-flash"},
            {"provider": "openai", "api_key": "...", "model": "gpt-4o"}
        ]}

    Order in the list = priority.  On InsufficientCreditsError,
    the next provider is used automatically.
    """
    llm_cfg = config.get("llm")
    context_level = config.get("defaults", {}).get("context_level", DEFAULT_CONTEXT_LEVEL)

    entries: list[dict] = []
    if isinstance(llm_cfg, list):
        entries = [e for e in llm_cfg if isinstance(e, dict)]
    elif isinstance(llm_cfg, dict):
        entries = [llm_cfg]

    yandex_entries = [e for e in entries if e.get("provider") == "yandexgpt"]
    if not yandex_entries:
        return NoAIClient()

    ensure_provider_deps("yandexgpt")
    entry = yandex_entries[0]
    try:
        client = _build_client("yandexgpt", entry, context_level)
        print(f"  + yandexgpt ({client.model})")
        return client
    except Exception as e:
        print(f"  x yandexgpt: {e}")
        return NoAIClient()
