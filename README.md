# tgai

`tgai` is a terminal-first Telegram client with an AI layer on top of Telethon. It helps you read chats, draft replies, build digests, and run monitored auto-reply flows from the command line.

The project is optimized for daily personal use:
- one persistent Telegram session
- fast keyboard-driven UI
- local state in `~/.tgai`
- multiple LLM providers with fallback support
- safe defaults for messaging workflows

## Features

- Interactive main menu with arrow-key navigation and live search
- Full-screen chat mode with scrolling, polling for new messages, and AI draft suggestions
- Digest mode for unread or time-windowed messages across chats and channels
- Live digest updates while the digest viewer is open
- Listen mode for assisted or automatic replies
- Personas for different reply styles
- Local summary cache, digest watermarks, and instant reopen of the last digest

## Installation

### Requirements

- Python 3.10+
- A Telegram account
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- At least one supported LLM provider

### Install from source

```bash
git clone <repo-url>
cd tgai
pip install -e .
```

### Optional extras

```bash
pip install -e .[openai]
pip install -e .[gemini]
pip install -e .[all]
pip install -e .[dev]
```

Base dependencies are defined in [setup.py](/Users/nik/Downloads/tgai/setup.py) and [requirements.txt](/Users/nik/Downloads/tgai/requirements.txt).

## First Run

Run:

```bash
tgai
```

On first launch the app will guide you through configuration and store it in `~/.tgai/config.json`.

You will need:

1. `api_id` and `api_hash` for Telegram.
2. At least one LLM provider configuration.

## Supported Providers

The LLM layer in [claude.py](/Users/nik/Downloads/tgai/tgai/claude.py) supports:

- Anthropic
- OpenAI
- OpenRouter
- Gemini
- g4f
- YandexGPT

The project still uses historical names like `claude.py` in code, but the abstraction is provider-agnostic.

## Quick Start

```bash
tgai
tgai chat
tgai chat @username
tgai aggregate
tgai aggregate --hours 12
tgai aggregate --all
tgai listen --auto
tgai listen --batch 30m
```

## Commands

### `tgai`

Starts the interactive menu and keeps one Telegram connection alive for the full session.

### `tgai chat [target]`

Opens a specific chat or the interactive chat picker.

What chat mode includes:
- full-screen message view
- real-time polling for new messages
- AI reply suggestions
- draft improvement via `Tab`
- AI suggestion reformulation via `п`
- background history loading while scrolling upward

### `tgai aggregate`

Builds a digest over recent messages.

Supported workflows:
- unread-only digest
- digest including read messages
- chat-only, channel-only, or mixed scope
- live digest updates while the viewer is open
- instant reopen of the previous digest from locally saved sections

Useful flags:

```bash
tgai aggregate --hours 1
tgai aggregate --hours 12 --all
tgai aggregate --save
```

### `tgai listen`

Monitors incoming messages and generates replies.

Modes:
- `--auto` for full autopilot
- `--batch 30m` for reply confirmation mode

## UI Notes

### Chat list

- unread chats are shown first
- inside unread/read groups chats are sorted by latest activity
- `-` toggles display of usernames in list views
- `Tab` opens preview from the chat list

### Chat viewer

- shows names by default; usernames are hidden if a name exists
- supports scrolling with arrows, page keys, and mouse wheel
- keeps a bottom gap so the latest message is not glued to the terminal edge

### Digest viewer

- sections with unread messages are shown first
- inside unread/read groups sections are sorted by recency
- unread counters are shown per section
- `-` toggles usernames
- `Esc` and `←` go back

## Personas

Personas are plain text files stored in `~/.tgai/personas`.

Example:

```text
Тон: деловой, короткий.
Не обещай сроков.
Если есть риск недопонимания, уточняй вопросом.
```

Usage:

```bash
tgai chat --persona work
tgai listen --auto --persona work
```

## YandexGPT Setup

`tgai` expects two Yandex-specific values in config:
- `api_key`
- `folder_id`

In code this is enforced in [config.py](/Users/nik/Downloads/tgai/tgai/config.py) and [claude.py](/Users/nik/Downloads/tgai/tgai/claude.py). Internally the model URI is built as:

```text
gpt://<folder_id>/yandexgpt-lite
```

### What you need before creating the key

According to the official Yandex AI Studio docs:
- create or choose a cloud and folder
- create a service account in that folder
- grant the service account access to language models
- create an API key for that service account

Primary references:
- [Setting up access to Yandex AI Studio with API keys](https://yandex.cloud/en/docs/foundation-models/operations/get-api-key)
- [Authentication with the Yandex Foundation Models API](https://yandex.cloud/en/docs/foundation-models/api-ref/authentication)
- [Getting started with YandexGPT](https://yandex.cloud/en/docs/foundation-models/quickstart/yandexgpt)

### Step-by-step: get the YandexGPT API key

1. Sign in to Yandex Cloud and open the folder where you want to use YandexGPT.
2. Make sure billing is enabled for the cloud. Without billing, requests may be unavailable or limited.
3. In the management console, open `Identity and Access Management`.
4. Go to `Service accounts`.
5. Create a new service account if you do not already have one for `tgai`.
6. Grant the service account a role that allows text generation.
Recommended minimum: `ai.languageModels.user`.
7. Open that service account.
8. Click `Create new key` -> `Create API key`.
9. In the key scope, choose a scope that includes text generation.
For `tgai`, the safest option is the broad AI Studio scope `yc.ai.foundationModels.execute`.
10. Save the generated secret key immediately. Yandex shows it only once.

What to paste into `tgai`:
- `Yandex GPT API Key:` the secret value of the API key

### Step-by-step: get the folder ID

The `folder_id` is the ID of the folder where the service account was created and where you are going to use YandexGPT.

You can get it in the management console:

1. Open Yandex Cloud.
2. Switch to the needed folder.
3. Open the folder overview or the top navigation with cloud/folder selection.
4. Copy the folder identifier.
It usually looks like an ID starting with something like `b1g...`.

You can also get it with the Yandex Cloud CLI if you use `yc`:

```bash
yc resource-manager folder list
```

Then copy the `ID` of the folder you want.

What to paste into `tgai`:
- `Yandex Folder ID:` the folder ID, not the cloud ID and not the service account ID

### Common mistakes

- Using the cloud ID instead of the folder ID
- Creating the API key in one folder and using the folder ID from another
- Forgetting to grant the service account access to language models
- Saving the API key ID instead of the API key secret
- Expecting `tgai` to work with only OAuth or IAM token: the current YandexGPT integration in this repo expects `api_key + folder_id`

## Local Data Layout

All user data lives in `~/.tgai/`.

```text
~/.tgai/
├── config.json
├── session.session
├── personas/
├── history/
├── digests/
├── whitelist.json
├── alerts.json
├── summary_cache.json
├── digest_watermark.json
└── last_digest.json
```

What is stored locally:
- provider configuration
- Telegram session
- personas
- per-chat LLM history
- saved digest files
- digest reopen state
- digest cache and watermarks

## Architecture

### Main modules

- [cli.py](/Users/nik/Downloads/tgai/tgai/cli.py): CLI entrypoint, interactive session loop, menu routing
- [telegram.py](/Users/nik/Downloads/tgai/tgai/telegram.py): Telethon wrapper and Telegram-specific helpers
- [claude.py](/Users/nik/Downloads/tgai/tgai/claude.py): provider abstraction and shared prompting logic
- [storage.py](/Users/nik/Downloads/tgai/tgai/storage.py): local persistence layer
- [ui.py](/Users/nik/Downloads/tgai/tgai/ui.py): prompt_toolkit and text UI components
- [chat_view.py](/Users/nik/Downloads/tgai/tgai/chat_view.py): viewport helpers for full-screen chat scrolling

### Command modules

- [chat.py](/Users/nik/Downloads/tgai/tgai/commands/chat.py)
- [aggregate.py](/Users/nik/Downloads/tgai/tgai/commands/aggregate.py)
- [listen.py](/Users/nik/Downloads/tgai/tgai/commands/listen.py)

## Safety and Current Constraints

- Message deletion is explicitly blocked in the Telegram layer.
- Digest and cache keys are still mostly keyed by display name, not stable `chat_id`.
- Telegram session storage uses Telethon's SQLite session file, so multiple suspended `tgai` processes can lock the session.
- The UI is terminal-focused and intentionally does not try to behave like a desktop chat app.

## Development

### Run tests

```bash
pytest -q
```

### Useful checks

```bash
python3 -m py_compile tgai/cli.py tgai/ui.py tgai/telegram.py tgai/storage.py
```

## Packaging Notes

This repository currently uses `setup.py`-based packaging. There is no `pyproject.toml` yet.

## License

No license file is currently present in the repository. If this is intended for production or public distribution, add an explicit license before release.
