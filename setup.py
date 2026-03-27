"""Setup configuration for tgai."""

from setuptools import setup, find_packages
from pathlib import Path

long_description = ""
readme_path = Path(__file__).parent / "README.md"
if readme_path.exists():
    long_description = readme_path.read_text(encoding="utf-8")

setup(
    name="tgai",
    version="0.1.0",
    description="Telegram + AI ассистент (Anthropic, OpenAI, OpenRouter)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="tgai contributors",
    python_requires=">=3.10",
    packages=find_packages(exclude=["tests*"]),
    entry_points={
        "console_scripts": [
            "tgai=tgai.cli:main",
        ],
    },
    install_requires=[
        "telethon>=1.34.0",
        "anthropic>=0.25.0",
        "questionary>=2.0.0",
        "prompt_toolkit>=3.0.0",
        "python-dateutil>=2.8.0",
    ],
    extras_require={
        "openai": [
            "openai>=1.0.0",
        ],
        "gemini": [
            "google-genai>=1.0.0",
        ],
        "all": [
            "openai>=1.0.0",
            "google-genai>=1.0.0",
        ],
        "dev": [
            "pytest",
            "pytest-asyncio",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Environment :: Console",
        "Topic :: Communications :: Chat",
    ],
)
