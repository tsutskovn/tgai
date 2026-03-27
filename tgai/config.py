"""Configuration management for tgai."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import questionary

TGAI_DIR = Path.home() / ".tgai"
CONFIG_PATH = TGAI_DIR / "config.json"

DEFAULT_PERSONA = """\
Ты — умный AI-ассистент, помогающий отвечать на сообщения в Telegram.
Пиши коротко, по делу, в том же стиле, что и собеседник.
Отвечай на том языке, на котором написано входящее сообщение.
Не добавляй лишних вежливостей и воды.
"""

DEFAULT_CONFIG: dict = {
    "telegram": {
        "api_id": None,
        "api_hash": None,
    },
    "defaults": {
        "aggregate_hours": 24,
        "batch_interval": "30m",
        "persona": "default",
    },
}


def _config_is_complete(config: dict) -> bool:
    """Check that all required fields are filled."""
    try:
        tg_ok = bool(config["telegram"]["api_id"]) and bool(config["telegram"]["api_hash"])
    except (KeyError, TypeError):
        return False
    return tg_ok


# Available LLM providers
PROVIDER_INFO = {
    "yandexgpt": {"label": "Yandex GPT", "needs_url": False, "needs_key": True, "needs_folder_id": True},
}

DEFAULT_MODELS = {
    "yandexgpt": "yandexgpt-lite",
}


def _pause_after_message(text_mode: bool = False) -> None:
    """Wait for Enter so setup errors stay visible on screen."""
    prompt = "Нажмите Enter, чтобы продолжить..."
    if text_mode:
        input(f"{prompt}\n")
        return

    try:
        questionary.text(prompt, default="").ask()
    except Exception:
        input(f"{prompt}\n")


def load_config() -> dict:
    """Load config from disk, running first-run setup if needed."""
    if not CONFIG_PATH.exists():
        return run_first_run_setup()
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if not _config_is_complete(config):
        print("Некоторые настройки не заполнены. Запускаю настройку...\n")
        return run_first_run_setup()
    return config


def save_config(config: dict) -> None:
    """Save config to disk."""
    TGAI_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def run_first_run_setup(text_mode: bool = False) -> dict:
    """Interactive first-run setup wizard."""
    from tgai.claude import validate_yandex_credentials

    print("\nДобро пожаловать в tgai!\n")

    if text_mode:
        api_id_str = input("Telegram API ID: ").strip()
        api_hash = input("Telegram API Hash: ").strip()
    else:
        api_id_str = questionary.text("Telegram API ID:").ask()
        if api_id_str is None:
            sys.exit(0)
        api_hash = questionary.text("Telegram API Hash:").ask()
        if api_hash is None:
            sys.exit(0)

    try:
        api_id = int(api_id_str.strip())
    except (ValueError, AttributeError):
        print("Ошибка: API ID должен быть числом.")
        sys.exit(1)

    # Collect LLM providers
    llm_list = []
    print("\nНастройка YandexGPT для AI-функций (можно пропустить):\n")
    for provider_name, info in PROVIDER_INFO.items():
        if text_mode:
            key = input(f"{info['label']} API Key (Enter чтобы пропустить): ").strip()
        else:
            key = questionary.password(f"{info['label']} API Key (Enter чтобы пропустить):").ask()
            if key is None:
                key = ""
            key = key.strip()
        if key or not info.get("needs_key", True):
            entry = {
                "provider": provider_name,
                "api_key": key,
                "model": DEFAULT_MODELS[provider_name],
            }
            if provider_name == "openrouter":
                entry["base_url"] = "https://openrouter.ai/api/v1"
            if info.get("needs_folder_id"):
                if text_mode:
                    folder_id = input("Yandex Folder ID: ").strip()
                else:
                    folder_id = questionary.text("Yandex Folder ID:").ask()
                    if folder_id is None:
                        folder_id = ""
                    folder_id = folder_id.strip()
                if not folder_id:
                    continue
                entry["folder_id"] = folder_id
            ok, error = validate_yandex_credentials(
                entry.get("api_key", ""),
                entry.get("folder_id", ""),
                entry.get("model", ""),
            )
            if not ok:
                print(error)
                print("YandexGPT не был сохранен. Проверь ключ и folder_id в README.\n")
                _pause_after_message(text_mode)
                continue
            llm_list.append(entry)
            print(f"  + {info['label']}")

    config = {
        "telegram": {
            "api_id": api_id,
            "api_hash": api_hash.strip(),
        },
        "llm": llm_list,
        "defaults": {
            "aggregate_hours": 24,
            "batch_interval": "30m",
            "persona": "default",
            "context_level": "base",
        },
    }

    save_config(config)
    print(f"\nКонфигурация сохранена в {CONFIG_PATH}")

    # Create default persona
    personas_dir = TGAI_DIR / "personas"
    personas_dir.mkdir(parents=True, exist_ok=True)
    default_persona_path = personas_dir / "default.txt"
    if not default_persona_path.exists():
        default_persona_path.write_text(DEFAULT_PERSONA, encoding="utf-8")
        print(f"Персона по умолчанию создана: {default_persona_path}")

    # Create other directories
    (TGAI_DIR / "history").mkdir(parents=True, exist_ok=True)
    (TGAI_DIR / "digests").mkdir(parents=True, exist_ok=True)

    print("\nПодключение к Telegram...")
    return config


def ensure_directories() -> None:
    """Ensure all required directories exist."""
    for sub in ("personas", "history", "digests"):
        (TGAI_DIR / sub).mkdir(parents=True, exist_ok=True)

    default_persona_path = TGAI_DIR / "personas" / "default.txt"
    if not default_persona_path.exists():
        default_persona_path.write_text(DEFAULT_PERSONA, encoding="utf-8")
