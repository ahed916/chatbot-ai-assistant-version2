"""
llm.py — LLM factory. One place to configure and swap the language model.

All agents import get_llm() from here.
To switch models: change LLM_MODEL in .env. Nothing else changes.
"""
from langchain_openai import ChatOpenAI
from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_TEMPERATURE, LLM_MAX_TOKENS, LLM_TIMEOUT


def get_llm() -> ChatOpenAI:
    """
    Return a configured LLM instance.
    Uses OpenAI-compatible API (works with OpenRouter, Together, Ollama, etc.)
    """
    return ChatOpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        model=LLM_MODEL,
        temperature=LLM_TEMPERATURE,

        timeout=LLM_TIMEOUT,
        # Improves reliability on free-tier models
        max_retries=2,
    )
