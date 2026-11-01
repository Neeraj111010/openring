"""Refresh openring/llm_prices.json: what each model provider charges per million tokens.

Prices come from LiteLLM's price list (MIT) and, for OpenRouter, from OpenRouter's public model list.
Run it now and then; the file it writes ships with OpenRing, so calls never wait on a price lookup:

    python scripts/update_prices.py
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from pathlib import Path

LITELLM = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
OPENROUTER = "https://openrouter.ai/api/v1/models"
OUT = Path(__file__).resolve().parent.parent / "openring" / "llm_prices.json"
# LiteLLM's provider names -> OpenRing's (Vapi's).
PROVIDERS = {
    "openai": "openai", "anthropic": "anthropic", "gemini": "google", "groq": "groq", "deepseek": "deepseek",
    "xai": "xai", "cerebras": "cerebras", "together_ai": "together-ai", "deepinfra": "deepinfra",
    "perplexity": "perplexity-ai", "mistral": "mistral", "fireworks_ai": "fireworks",
}


def fetch(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "OpenRing price update"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def per_million(value: object) -> float | None:
    try:
        price = float(value) * 1_000_000
    except (TypeError, ValueError):
        return None
    return float(f"{price:.6g}") if price >= 0 else None


def main() -> None:
    llm: dict[str, dict[str, list[float]]] = {}
    for key, entry in fetch(LITELLM).items():
        if not isinstance(entry, dict) or entry.get("mode") != "chat":
            continue
        provider = PROVIDERS.get(str(entry.get("litellm_provider")))
        prices = [per_million(entry.get("input_cost_per_token")), per_million(entry.get("output_cost_per_token"))]
        if provider and None not in prices:
            model = key.removeprefix(str(entry["litellm_provider"]) + "/")
            llm.setdefault(provider, {})[model] = prices
    for entry in fetch(OPENROUTER)["data"]:
        pricing = entry.get("pricing") or {}
        prices = [per_million(pricing.get("prompt")), per_million(pricing.get("completion"))]
        if None not in prices:  # OpenRouter marks variable-price routers with -1
            llm.setdefault("openrouter", {})[entry["id"]] = prices
    doc = {
        "updated": date.today().isoformat(),
        "unit": "USD per 1M tokens: [input, output]",
        "sources": [LITELLM, OPENROUTER],
        "llm": {p: dict(sorted(models.items())) for p, models in sorted(llm.items())},
    }
    OUT.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{OUT.name}: {sum(len(m) for m in llm.values())} models from {len(llm)} providers")


if __name__ == "__main__":
    main()
