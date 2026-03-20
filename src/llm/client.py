"""
llm/client.py — Provider-agnostic LLM interface.

Single Responsibility: wrap any LLM backend behind a single function.
The rest of the bot calls llm_chat() — it never touches SDK specifics.

Supported backends:
  anthropic — native Anthropic SDK (claude-*)
  openai    — any OpenAI-compatible endpoint:
              any local OpenAI-compatible server (LM Studio, vLLM, etc.), Groq, Together, OpenAI
"""

from typing import Optional
from src.config import LLM_BACKEND, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY

try:
    import anthropic as _anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from openai import OpenAI as _OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


def _build_client():
    if LLM_BACKEND == "anthropic":
        if not HAS_ANTHROPIC:
            print("⚠  anthropic not installed: pip install anthropic")
            return None
        if not LLM_API_KEY:
            print("⚠  ANTHROPIC_API_KEY not set")
            return None
        return _anthropic.Anthropic(api_key=LLM_API_KEY)

    if not HAS_OPENAI:
        print("⚠  openai not installed: pip install openai")
        return None
    base = LLM_BASE_URL or "https://api.openai.com/v1"
    return _OpenAI(base_url=base, api_key=LLM_API_KEY or "sk-local")


_client = _build_client()


def llm_chat(prompt: str, max_tokens: int = 500) -> Optional[str]:
    """
    Send a prompt to the configured LLM and return the response text.
    Returns None on any failure — callers must handle the None case gracefully.
    """
    if _client is None:
        return None
    try:
        if LLM_BACKEND == "anthropic":
            resp = _client.messages.create(
                model=LLM_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        else:
            resp = _client.chat.completions.create(
                model=LLM_MODEL,
                max_tokens=max_tokens,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
    except Exception:
        return None


def is_available() -> bool:
    return _client is not None
