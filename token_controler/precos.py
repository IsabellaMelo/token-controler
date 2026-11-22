"""Tabela de preços por modelo (USD por milhão de tokens).

Fonte: https://platform.claude.com/docs/en/about-claude/pricing (consultada em 2026-11-21).
Atualize aqui quando os preços mudarem e rode `tc ingest --tudo` para recalcular.

Cada entrada: (input, cache_write_5m, cache_write_1h, cache_read, output)
"""

from __future__ import annotations

PRECOS: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-2": (8.00, 10.00, 16.00, 0.20, 40.00),
    "claude-opus-5-5": (4.00, 5.00, 8.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 2.50, 4.00, 0.10, 10.00),
    # Haiku 5.5 tem dois patamares; o de prompt > 100k fica em PRECOS_HAIKU_55_LONGO
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-opus-5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-sonnet-5": (2.00, 2.50, 4.00, 0.20, 10.00),
    "claude-opus-4-8": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-7": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-6": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-1": (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-opus-4": (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-sonnet-4-6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4-5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-haiku-4-5": (1.00, 1.25, 2.00, 0.10, 5.00),
}

PRECOS_HAIKU_55_LONGO = (0.50, 0.625, 1.00, 0.05, 2.50)
LIMITE_HAIKU_55 = 100_000

# Chaves ordenadas da mais longa para a mais curta, para que
# "claude-opus-5-5" case antes de "claude-opus-5".
_CHAVES = sorted(PRECOS, key=len, reverse=True)


def achar_preco(modelo: str | None, prompt_total: int = 0):
    """Devolve a tupla de preços do modelo, ou None se for desconhecido."""
    if not modelo:
        return None
    m = modelo.lower()
    for chave in _CHAVES:
        if m.startswith(chave):
            if chave == "claude-haiku-5-5" and prompt_total > LIMITE_HAIKU_55:
                return PRECOS_HAIKU_55_LONGO
            return PRECOS[chave]
    return None


def calcular_custo(
    modelo: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m: int,
    cache_write_1h: int,
    cache_read: int,
) -> float | None:
    """Custo estimado em USD. None quando o modelo não está na tabela."""
    prompt_total = input_tokens + cache_write_5m + cache_write_1h + cache_read
    p = achar_preco(modelo, prompt_total)
    if p is None:
        return None
    p_in, p_w5, p_w1, p_read, p_out = p
    return (
        input_tokens * p_in
        + cache_write_5m * p_w5
        + cache_write_1h * p_w1
        + cache_read * p_read
        + output_tokens * p_out
    ) / 1_000_000
