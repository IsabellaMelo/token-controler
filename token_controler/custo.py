"""Modos de custo (auto, calculate, display) e a origem de cada custo.

- registrado: o log da ferramenta trouxe o custo (campo `costUSD` no Claude Code).
- calculado:  tokens x tabela de preços (`precos.py`).
- sem_preco:  não há custo para a chamada: modelo fora da tabela, ou, no modo `display`,
              nenhum custo registrado. Nunca recebe um valor chutado.

Chamada de erro de API não tem custo e fica com origem NULL.
"""

from __future__ import annotations

import math
import sqlite3

from .precos import calcular_custo

MODOS = ("auto", "calculate", "display")
MODO_PADRAO = "auto"

REGISTRADO, CALCULADO, SEM_PRECO = "registrado", "calculado", "sem_preco"


def custo_registrado_valido(valor) -> float | None:
    """O `costUSD` do log, se for um número finito e não negativo; senão None (tratado como ausente)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    if not math.isfinite(valor) or valor < 0:
        return None
    return float(valor)


def decidir(
    modo: str,
    modelo: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m: int,
    cache_write_1h: int,
    cache_read: int,
    registrado: float | None,
    erro_api: bool = False,
) -> tuple[float | None, str | None]:
    """Devolve (custo_usd, origem_custo) da chamada no modo dado."""
    if erro_api:
        return None, None
    if registrado is not None and modo in ("auto", "display"):
        return registrado, REGISTRADO
    if modo == "display":
        return None, SEM_PRECO
    calculado = calcular_custo(
        modelo, input_tokens, output_tokens, cache_write_5m, cache_write_1h, cache_read
    )
    if calculado is None:
        return None, SEM_PRECO
    return calculado, CALCULADO


def recalcular(con: sqlite3.Connection, modo: str) -> int:
    """Refaz custo_usd e origem_custo de todas as chamadas, só com o que está no banco.

    Não lê os logs, que as ferramentas apagam com o tempo. Devolve quantas linhas atualizou.
    """
    linhas = con.execute(
        """SELECT id, modelo, input_tokens, output_tokens, cache_write_5m, cache_write_1h,
                  cache_read, custo_registrado, erro_api FROM chamadas"""
    ).fetchall()
    con.executemany(
        "UPDATE chamadas SET custo_usd = ?, origem_custo = ? WHERE id = ?",
        [
            (*decidir(modo, r["modelo"], r["input_tokens"], r["output_tokens"], r["cache_write_5m"],
                      r["cache_write_1h"], r["cache_read"], r["custo_registrado"], bool(r["erro_api"])),
             r["id"])
            for r in linhas
        ],
    )
    return len(linhas)
