from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE = {429, 500, 502, 503, 504}
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


class LLMFatal(LLMError):
    pass


def extract_json(text: str) -> Any:
    """Premier JSON valide dans `text` : bloc ``` ou premier objet/tableau trouvé."""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("aucun JSON dans la réponse")


class LLMClient:
    def __init__(self, api_key: str, model: str, max_calls: int | None = None,
                 post=requests.post, sleep=time.sleep):
        self.api_key = api_key
        self.model = model
        self.max_calls = max_calls
        self._post = post
        self._sleep = sleep
        self.calls = 0

    def _request(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceeded(f"plafond de {self.max_calls} appels LLM atteint")
        self.calls += 1
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        last = None
        for attempt in range(3):
            try:
                r = self._post(ENDPOINT, headers=headers, json=payload, timeout=60)
            except requests.exceptions.RequestException as e:
                last = f"réseau: {e}"
                self._sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                body = r.json()
                if "choices" not in body:
                    raise LLMError(f"réponse sans choices: {str(body.get('error') or body)[:200]}")
                content = body["choices"][0]["message"]["content"]
                if not content:
                    raise LLMError("réponse vide")
                return content
            if r.status_code in RETRYABLE:
                last = f"HTTP {r.status_code}"
                self._sleep(2 ** attempt)
                continue
            if r.status_code in (400, 401, 402, 403):
                raise LLMFatal(f"HTTP {r.status_code} (fatal: clé, crédit ou requête invalide)")
            raise LLMError(f"HTTP {r.status_code} (non retentable)")
        raise LLMError(f"échec après 3 tentatives: {last}")

    def chat(self, system: str, user: str, temperature: float = 0.0,
             max_tokens: int = 800) -> str:
        return self._request([{"role": "system", "content": system},
                              {"role": "user", "content": user}], temperature, max_tokens)

    def chat_json(self, system: str, user: str, **kw) -> Any:
        first = self.chat(system, user, **kw)
        try:
            return extract_json(first)
        except ValueError as e:
            retry_user = (f"{user}\n\nTa réponse précédente n'était pas du JSON valide "
                          f"({e}). Réponds UNIQUEMENT avec du JSON.")
            second = self.chat(system, retry_user, **kw)
            try:
                return extract_json(second)
            except ValueError:
                raise LLMError("JSON invalide après deux tentatives")
