"""Relatório de qualidade dos dados: quanto dá para confiar nos números, por ferramenta.

Inspirado no relatório de confiança do AgentTrace. Só conta e classifica o que já está no banco;
nunca estima nem preenche lacunas. Os limites da nota de confiança são uma proposta inicial.
"""

from __future__ import annotations

import sqlite3

from .db import ERRO_REAL
from .precos import achar_preco

# Chamada de erro de API não tem tokens nem custo e fica fora destas contas.
SQL_SEM_TOKENS = "erro_api = 0 AND input_tokens + output_tokens + cache_write_5m + cache_write_1h + cache_read = 0"


def _por_ferramenta(con: sqlite3.Connection, sql: str, ferramenta: str | None) -> dict:
    """Roda uma consulta que devolve `ferramenta` na 1ª coluna e indexa as linhas por ela."""
    return {r["ferramenta"]: dict(r) for r in con.execute(sql, [ferramenta] if ferramenta else [])}


def coletar(con: sqlite3.Connection, ferramenta: str | None = None) -> list[dict]:
    """Uma linha de qualidade por ferramenta, com números crus e a nota de confiança."""
    filtro = "WHERE ferramenta = ?" if ferramenta else ""
    arquivos = _por_ferramenta(con, f"""
        SELECT ferramenta, COUNT(*) AS arquivos, SUM(linhas_ruins) AS linhas_ruins,
               SUM(linhas_ruins IS NULL) AS sem_contagem
        FROM arquivos {filtro} GROUP BY ferramenta""", ferramenta)
    chamadas = _por_ferramenta(con, f"""
        SELECT ferramenta, COUNT(*) AS chamadas, SUM(erro_api) AS erros_api,
               SUM({SQL_SEM_TOKENS}) AS sem_tokens,
               SUM(origem_custo = 'registrado') AS registrado,
               SUM(origem_custo = 'calculado') AS calculado,
               SUM(origem_custo = 'sem_preco') AS sem_preco
        FROM chamadas {filtro} GROUP BY ferramenta""", ferramenta)
    # T23: erros de tool por categoria. `outro` alto = faltam regras em regras_erros.py; sem categoria = ler de novo (`--tudo`)
    erros = _por_ferramenta(con, f"""
        SELECT ferramenta, SUM({ERRO_REAL}) AS erros_tool,
               SUM({ERRO_REAL} AND erro_categoria = 'outro') AS erros_outro,
               SUM({ERRO_REAL} AND erro_categoria IS NULL) AS erros_sem_categoria
        FROM tool_calls {filtro} GROUP BY ferramenta""", ferramenta)
    sem_preco: dict[str, list[dict]] = {}
    for r in con.execute(f"""
        SELECT ferramenta, modelo, COUNT(*) AS n FROM chamadas
        WHERE erro_api = 0 {"AND ferramenta = ?" if ferramenta else ""}
        GROUP BY ferramenta, modelo ORDER BY n DESC""", [ferramenta] if ferramenta else []):
        if achar_preco(r["modelo"]) is None:  # contra a tabela atual, em qualquer modo de custo
            sem_preco.setdefault(r["ferramenta"], []).append({"modelo": r["modelo"], "chamadas": r["n"]})

    saida = []
    for nome in sorted(set(arquivos) | set(chamadas)):
        a, c, e = arquivos.get(nome, {}), chamadas.get(nome, {}), erros.get(nome, {})
        n_arquivos, sem_contagem = a.get("arquivos") or 0, a.get("sem_contagem") or 0
        modelos = sem_preco.get(nome, [])
        linha = {
            "ferramenta": nome,
            "arquivos": n_arquivos,
            "arquivos_sem_contagem": sem_contagem,  # lidos antes da T11: rode `tc ingest --tudo`
            "linhas_ilegiveis": None if n_arquivos == sem_contagem else (a.get("linhas_ruins") or 0),
            "chamadas": c.get("chamadas") or 0,
            "erros_api": c.get("erros_api") or 0,
            "chamadas_sem_tokens": c.get("sem_tokens") or 0,
            "modelos_sem_preco": modelos,
            "chamadas_sem_preco": sum(m["chamadas"] for m in modelos),
            "origem_registrado": c.get("registrado") or 0,
            "origem_calculado": c.get("calculado") or 0,
            "origem_sem_preco": c.get("sem_preco") or 0,
            "erros_tool": e.get("erros_tool") or 0,                  # sem as recusas do usuário
            "erros_tool_outro": e.get("erros_outro") or 0,           # sem regra de categoria (design 6.4)
            "erros_tool_sem_categoria": e.get("erros_sem_categoria") or 0,   # lidos antes da T23: `tc ingest --tudo`
            "nivel_detalhe": None,  # depende da T09 (nível de detalhe por sessão)
        }
        linha["confianca"], linha["motivos"] = avaliar(linha)
        saida.append(linha)
    return saida


def avaliar(q: dict) -> tuple[str, list[str]]:
    """Nota de confiança e os motivos. Regra proposta (a calibrar com uso real):

    - baixa: há chamadas sem preço, chamadas sem tokens ou linhas ilegíveis (pode faltar dado ou custo);
    - média: nada disso, mas há arquivos sem a contagem de linhas ilegíveis;
    - alta: nenhum dos problemas acima.
    """
    if not q["chamadas"]:
        return "sem_dados", ["nenhuma chamada no banco"]
    baixa, media = [], []
    if q["chamadas_sem_preco"]:
        nomes = ", ".join(str(m["modelo"] or "(sem modelo)") for m in q["modelos_sem_preco"])
        baixa.append(f"{q['chamadas_sem_preco']} chamada(s) sem preço: {nomes}")
    if q["chamadas_sem_tokens"]:
        baixa.append(f"{q['chamadas_sem_tokens']} chamada(s) sem contagem de tokens")
    if q["linhas_ilegiveis"]:
        baixa.append(f"{q['linhas_ilegiveis']} linha(s) ilegível(is) nos logs")
    if q["arquivos_sem_contagem"]:
        media.append(f"{q['arquivos_sem_contagem']} arquivo(s) sem contagem de linhas ilegíveis "
                     "(rode `tc ingest --tudo`)")
    if baixa:
        return "baixa", baixa + media
    return ("media", media) if media else ("alta", [])
