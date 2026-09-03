"""Test content generators for token estimator calibration.

Each generator produces representative content types that real LLM providers
process.  Content types are chosen to exercise different chars-per-token ratios.

Simulated "provider actual" values use realistic ratios:
  - English: ~0.75 chars/token (1 token ≈ 1.33 chars)
  - Chinese: ~1.5 chars/token (1 token ≈ 0.67 chars)
  - JSON keys: ~3.5 chars/token (structured text, many short tokens)
  - Code: ~2.0 chars/token (mix of keywords and symbols)
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import ClassVar, Protocol


class ContentGenerator(Protocol):
    name: str
    chars_per_token: float  # realistic provider ratio

    def generate(self, *, target_tokens: int, seed: int = 42) -> str: ...


def _estimate_tokens(text: str, cpt: float) -> int:
    return max(1, int(len(text) / cpt))


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


class EnglishText:
    name = "english_text"
    chars_per_token = 0.75

    def __init__(self) -> None:
        self._words = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog", "a", "journey", "of", "a", "thousand", "miles", "begins", "with", "a", "single", "step", "to", "be", "or", "not", "to", "be", "that", "is", "the", "question", "all", "that", "glitters", "is", "not", "gold", "the", "only", "thing", "we", "have", "to", "fear", "is", "fear", "itself", "in", "the", "beginning", "was", "the", "word", "and", "the", "word", "was", "with", "god", "technology", "is", "best", "when", "it", "brings", "people", "together", "the", "purpose", "of", "computation", "is", "insight", "not", "numbers"]

    def generate(self, *, target_tokens: int, seed: int = 42) -> str:
        rng = random.Random(seed)
        words = [rng.choice(self._words) for _ in range(target_tokens)]
        return " ".join(words)


class ChineseText:
    name = "chinese_text"
    chars_per_token = 1.5

    def __init__(self) -> None:
        self._chars = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"

    def generate(self, *, target_tokens: int, seed: int = 42) -> str:
        rng = random.Random(seed)
        char_count = int(target_tokens * self.chars_per_token)
        return "".join(rng.choice(self._chars) for _ in range(char_count))


class JsonToolSchema:
    name = "json_tool_schema"
    chars_per_token = 3.5

    _TOOL_TEMPLATE: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search through a knowledge base of documents using semantic similarity matching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 10,
                    },
                    "filters": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "date_range": {"type": "string"},
                        },
                    },
                },
                "required": ["query"],
            },
        },
    }

    def generate(self, *, target_tokens: int, seed: int = 42) -> str:
        tools = []
        accumulated_tokens = 0
        tool_idx = 0
        while accumulated_tokens < target_tokens:
            tool = json.loads(json.dumps(self._TOOL_TEMPLATE))
            tool["function"]["name"] = f"tool_{tool_idx}"
            tool_str = json.dumps(tool, separators=(",", ":"))
            tools.append(tool_str)
            accumulated_tokens = _estimate_tokens(json.dumps(tools, separators=(",", ":")), self.chars_per_token)
            tool_idx += 1
        return json.dumps(tools, separators=(",", ":"))


class PythonCode:
    name = "python_code"
    chars_per_token = 2.0

    _SNIPPETS: ClassVar[list[str]] = [
        "def calculate_fibonacci(n: int) -> int:\n    if n <= 1:\n        return n\n    return calculate_fibonacci(n - 1) + calculate_fibonacci(n - 2)\n\n",
        "class DataProcessor:\n    def __init__(self, config: dict) -> None:\n        self.config = config\n        self.results: list[dict] = []\n\n    def process(self, items: list) -> list[dict]:\n        for item in items:\n            result = self._transform(item)\n            self.results.append(result)\n        return self.results\n\n",
        "async def fetch_data(url: str) -> dict:\n    async with httpx.AsyncClient() as client:\n        response = await client.get(url)\n        response.raise_for_status()\n        return response.json()\n\n",
        "from typing import Protocol\nfrom dataclasses import dataclass, field\n\n@dataclass\nclass TokenEstimator(Protocol):\n    def estimate(self, text: str) -> int: ...\n\n    @property\n    def name(self) -> str: ...\n",
    ]

    def generate(self, *, target_tokens: int, seed: int = 42) -> str:
        rng = random.Random(seed)
        parts: list[str] = []
        while _estimate_tokens("".join(parts), self.chars_per_token) < target_tokens:
            parts.append(rng.choice(self._SNIPPETS))
        return "".join(parts)


class MixedPrompt:
    """System prompt + tools + user message — the typical LLM invocation shape."""

    name = "mixed_prompt"
    chars_per_token = 1.8

    def generate(self, *, target_tokens: int, seed: int = 42) -> str:
        system = (
            "You are a helpful enterprise assistant. You have access to the following tools. "
            "Always use the most appropriate tool to answer user queries. "
            "Be concise and accurate in your responses. "
            "If you are unsure about something, say so rather than making up an answer. "
        )
        tool_schema = JsonToolSchema().generate(target_tokens=max(1, target_tokens // 3))
        user_msg = (
            "Please search our knowledge base for recent updates on "
            "token estimation algorithms and provide a summary of the findings. "
            "Focus on accuracy improvements and calibration methodologies."
        )
        combined = json.dumps(
            {"system": system, "tools": tool_schema, "user": user_msg},
            separators=(",", ":"),
        )
        return combined


class LongDocument:
    """Stress test: very long content for 100K+ char inputs."""

    name = "long_document"
    chars_per_token = 1.0  # mixed content, rough average

    def generate(self, *, target_tokens: int, seed: int = 42) -> str:
        base = (
            "This is a sample sentence for stress testing token estimation. "
            "The document contains repetitive content to reach the target token count. "
        )
        char_count = int(target_tokens * self.chars_per_token)
        repeats = (char_count // len(base)) + 1
        return (base * repeats)[:char_count]


ALL_GENERATORS: list[ContentGenerator] = [
    EnglishText(),
    ChineseText(),
    JsonToolSchema(),
    PythonCode(),
    MixedPrompt(),
    LongDocument(),
]


def simulate_provider_actual(text: str, cpt: float) -> int:
    """Simulate what a provider's count_tokens API would return.

    Uses the chars_per_token ratio with small realistic noise.
    This is NOT a real tokenizer — it simulates provider behavior for
    the spike's calibration methodology validation.
    """
    base = len(text) / cpt
    rng = random.Random(int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big"))
    noise = rng.uniform(-0.05, 0.05) * base
    return max(1, round(base + noise))
