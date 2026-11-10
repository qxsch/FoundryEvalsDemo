"""Token prices for the model bake-off, read live from the public Azure Retail Prices API (no sign-in needed)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

API = "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview"


@dataclass(frozen=True)
class Price:
    input: float  # USD per 1M input tokens
    cached: float  # USD per 1M cached input tokens
    output: float  # USD per 1M output tokens
    sku: str
    source: str  # "live <region>" or "static"


# Deployment model name -> (productName, SKU label, input meter, cached-input meter, output meter)
METERS = {
    "gpt-5.4": ("Azure OpenAI GPT5", "Global", "5.4 inp Gl 1M Tokens", "5.4 cd inp Gl 1M Tokens", "5.4 opt Gl 1M Tokens"),
    "gpt-5.4-mini": ("Azure OpenAI GPT5", "Global", "5.4 mini Inp Gl 1M Tokens", "5.4 mini cd Inp Gl 1M Tokens", "5.4 mini Opt Gl 1M Tokens"),
    "gpt-5.4-nano": ("Azure OpenAI GPT5", "Global", "5.4 nano Inp Gl 1M Tokens", "5.4 nano cd Inp Gl 1M Tokens", "5.4 nano Opt Gl 1M Tokens"),
    "DeepSeek-V4-Flash": ("Azure Deepseek Models", "EU Data Zone", "V4 Flash Inp DZ Tokens", "V4 Flash cached DZ Tokens", "V4 Flash Outp DZ Tokens"),
}

# Fallback if the API is unreachable (swedencentral list prices, November 2026).
STATIC = {
    "gpt-5.4": Price(2.50, 0.25, 15.00, "Global", "static"),
    "gpt-5.4-mini": Price(0.75, 0.075, 4.50, "Global", "static"),
    "gpt-5.4-nano": Price(0.20, 0.02, 1.25, "Global", "static"),
    "DeepSeek-V4-Flash": Price(0.21, 0.031, 0.56, "EU Data Zone", "static"),
}

_UNIT = {"1M": 1.0, "1K": 1_000.0, "1": 1_000_000.0}


def _meters(product: str, region: str) -> dict[str, float]:
    """meterName -> USD per 1M tokens for one product."""
    flt = f"serviceName eq 'Foundry Models' and armRegionName eq '{region}' and productName eq '{product}'"
    url: str | None = f"{API}&$filter={urllib.parse.quote(flt)}"
    out: dict[str, float] = {}
    while url:
        with urllib.request.urlopen(url, timeout=20) as resp:
            page = json.load(resp)
        for item in page.get("Items", []):
            if item.get("type") == "Consumption" and item.get("unitOfMeasure") in _UNIT:
                out[item["meterName"]] = item["retailPrice"] * _UNIT[item["unitOfMeasure"]]
        url = page.get("NextPageLink")
    return out


def get_prices(models: list[str], region: str = "swedencentral") -> dict[str, Price]:
    prices: dict[str, Price] = {}
    cache: dict[str, dict[str, float]] = {}
    for model in models:
        spec = METERS.get(model)
        try:
            if not spec:
                raise KeyError(model)
            product, sku, m_in, m_cached, m_out = spec
            if product not in cache:
                cache[product] = _meters(product, region)
            meters = cache[product]
            prices[model] = Price(meters[m_in], meters[m_cached], meters[m_out], sku, f"live {region}")
        except Exception:  # noqa: BLE001 - any API problem falls back to the static table
            if model in STATIC:
                prices[model] = STATIC[model]
    return prices


def cost_usd(price: Price, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float:
    uncached = max(prompt_tokens - cached_tokens, 0)
    return (uncached * price.input + cached_tokens * price.cached + completion_tokens * price.output) / 1_000_000
