"""Consultas ao banco, compartilhadas pelo CLI (tabelas e `--json`), pelo `tc painel`, pelo `tc live` e pelo `tc web`.

Só biblioteca padrão. Cada função devolve dicts e listas com números crus, prontos para formatar: None = sem dado ou não
se aplica (quem desenha mostra '-' ou "sem dados"); frações vão de 0 a 1; horários em ISO 8601 UTC, como no banco.
Nada aqui lê texto de conversa: só o que já está gravado (números, metadados, rótulos fixos e hashes).

O filtro é um `Filtro` (período em dias locais, projeto, ferramenta), o mesmo para todos os comandos.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import comportamento as _comportamento
from . import db, diagnostico, qualidade
from .precos import achar_preco

PAUSA_MAX_S = 300  # pausas acima de 5 min não contam como tempo ativo
DIAS_NO_PAINEL = 8
PERIODOS = ("hoje", "7d", "30d", "mes", "tudo")
DIAS_NO_GRAFICO = 31       # `tc web`: o gráfico de gasto por dia mostra no máximo estes dias (os mais recentes)
LINHAS_NA_TABELA = 50      # `tc web`: sessões na tabela (as mais recentes)

AGRUPAMENTOS = {
    "projeto": "COALESCE(projeto, '?')",
    "dia": "date(ts, 'localtime')",
    "sessao": "sessao_id",
    "ferramenta": "ferramenta",
    "modelo": "COALESCE(modelo, '?')",
}


# ---------- filtro ----------

@dataclass(frozen=True)
class Filtro:
    """Recorte comum das consultas. Datas em AAAA-MM-DD do horário local, inclusivas; None = sem limite."""

    desde: str | None = None
    ate: str | None = None
    projeto: str | None = None
    ferramenta: str | None = None

    def sql(self) -> tuple[str, list]:
        """(condição WHERE, parâmetros) para tabelas com `ts`, `projeto` e `ferramenta`."""
        cond, params = [], []
        if self.desde:
            cond.append("date(ts, 'localtime') >= ?")
            params.append(self.desde)
        if self.ate:
            cond.append("date(ts, 'localtime') <= ?")
            params.append(self.ate)
        if self.projeto:
            cond.append("projeto = ?")
            params.append(self.projeto)
        if self.ferramenta:
            cond.append("ferramenta = ?")
            params.append(self.ferramenta)
        return (" AND ".join(cond) or "1=1"), params


def inicio_do_periodo(periodo: str, hoje: date) -> str | None:
    """Primeiro dia (AAAA-MM-DD, horário local) do período; None = sem limite (tudo)."""
    if periodo == "hoje":
        return hoje.isoformat()
    if periodo == "7d":
        return (hoje - timedelta(days=6)).isoformat()
    if periodo == "30d":
        return (hoje - timedelta(days=29)).isoformat()
    if periodo == "mes":
        return hoje.replace(day=1).isoformat()
    return None


def filtro_do_periodo(periodo: str, hoje: date, projeto: str | None = None, ferramenta: str | None = None) -> Filtro:
    """Filtro de um período do painel (hoje, 7d, 30d, mes, tudo)."""
    return Filtro(desde=inicio_do_periodo(periodo, hoje), projeto=projeto, ferramenta=ferramenta)


def _dia_local(ts: str) -> str | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, AttributeError):
        return None


def _versao(con: sqlite3.Connection) -> tuple:
    """Muda quando o banco ganha ou perde linhas: chave dos caches das telas que se atualizam sozinhas."""
    return tuple(con.execute("""SELECT (SELECT COUNT(*) FROM chamadas), (SELECT COUNT(*) FROM tool_calls),
                                       (SELECT COUNT(*) FROM eventos)""").fetchone())


def ids_de_sessoes(con: sqlite3.Connection, f: Filtro) -> list[str]:
    """Sessões com algum evento (prompt, atividade, compactação) no filtro."""
    where, p = f.sql()
    return [r[0] for r in con.execute(f"SELECT DISTINCT sessao_id FROM eventos WHERE {where}", p)]


# ---------- contabilidade ----------

def resumo(con: sqlite3.Connection, f: Filtro, por: str) -> list[dict]:
    """Resumo agrupado (`tc resumo`), do mais caro para o mais barato. None = não se aplica ou sem dado."""
    g = AGRUPAMENTOS[por]
    where, p = f.sql()
    linhas: dict[str, dict] = {}

    for r in con.execute(f"""
        SELECT {g} AS grupo, COUNT(DISTINCT sessao_id) AS sessoes, COUNT(*) AS chamadas,
               SUM(erro_api) AS erros_api, SUM(input_tokens) AS inp, SUM(output_tokens) AS outp,
               SUM(cache_write_5m + cache_write_1h) AS cw, SUM(cache_read) AS cr,
               SUM(custo_usd) AS custo, SUM(custo_usd IS NULL AND erro_api = 0) AS sem_preco
        FROM chamadas WHERE {where} GROUP BY grupo""", p):
        linhas[r["grupo"]] = dict(r)

    if por != "modelo":
        for r in con.execute(f"""
            SELECT {g} AS grupo, COUNT(*) AS n FROM eventos
            WHERE tipo = 'prompt' AND {where} GROUP BY grupo""", p):
            linhas.setdefault(r["grupo"], {"grupo": r["grupo"]})["prompts"] = r["n"]
        for r in con.execute(f"""
            SELECT {g} AS grupo, COUNT(*) AS n, SUM(CASE WHEN {db.ERRO_REAL} THEN 1 ELSE 0 END) AS erros FROM tool_calls
            WHERE {where} GROUP BY grupo""", p):
            d = linhas.setdefault(r["grupo"], {"grupo": r["grupo"]})
            d["tools"], d["erros_tool"] = r["n"], r["erros"]

    saida = []
    por_modelo = por == "modelo"
    for grupo in sorted(linhas, key=lambda k: -(linhas[k].get("custo") or 0)):
        d = linhas[grupo]
        entrada, cw, cr = d.get("inp") or 0, d.get("cw") or 0, d.get("cr") or 0
        total_in = entrada + cw + cr
        saida.append({
            por: str(grupo),
            "sessoes": d.get("sessoes") or 0,
            "prompts": None if por_modelo else d.get("prompts"),
            "chamadas": d.get("chamadas") or 0,
            "tools": None if por_modelo else d.get("tools"),
            "erros_tool": None if por_modelo else d.get("erros_tool"),
            "erros_api": d.get("erros_api") or 0,
            "input": entrada,
            "output": d.get("outp") or 0,
            "cache_write": cw,
            "cache_read": cr,
            "cache_taxa": cr / total_in if total_in else None,  # fração de 0 a 1
            "custo_usd": d.get("custo"),                        # None = nenhuma chamada com preço
            "chamadas_sem_preco": d.get("sem_preco") or 0,      # a tabela marca com '*'
        })
    return saida


def custo_total(con: sqlite3.Connection, f: Filtro) -> float | None:
    """Soma do custo das chamadas no filtro; None = nenhuma chamada com custo."""
    where, p = f.sql()
    return con.execute(f"SELECT SUM(custo_usd) FROM chamadas WHERE {where}", p).fetchone()[0]


def origens_do_custo(con: sqlite3.Connection, f: Filtro) -> dict[str, int]:
    """Quantas chamadas por `origem_custo` (registrado, calculado, sem_preco); erros de API ficam fora."""
    where, p = f.sql()
    return dict(con.execute(
        f"SELECT origem_custo, COUNT(*) FROM chamadas WHERE origem_custo IS NOT NULL AND {where} "
        "GROUP BY origem_custo", p).fetchall())


def sessoes(con: sqlite3.Connection, f: Filtro, limite: int, com_categoria: bool = True) -> list[dict]:
    """Sessões (`tc sessoes`), da mais recente para a mais antiga. Horário em UTC (ISO 8601); durações em segundos.

    `com_categoria` False pula a categoria e o one-shot (o trecho mais caro); os campos ficam None e 0.
    """
    where, p = f.sql()
    sql = f"""
    WITH ev AS (
        SELECT sessao_id, projeto, ts,
               (julianday(ts) - julianday(LAG(ts) OVER (PARTITION BY sessao_id ORDER BY ts))) * 86400 AS gap
        FROM eventos WHERE tipo = 'atividade' AND {where}
    ), s AS (
        SELECT sessao_id, MAX(projeto) AS projeto, MIN(ts) AS inicio, MAX(ts) AS fim,
               SUM(CASE WHEN gap <= {PAUSA_MAX_S} THEN gap ELSE 0 END) AS ativo
        FROM ev GROUP BY sessao_id
    )
    SELECT s.*,
        (julianday(s.fim) - julianday(s.inicio)) * 86400 AS duracao,
        (SELECT COUNT(*) FROM eventos e WHERE e.sessao_id = s.sessao_id AND e.tipo = 'prompt') AS prompts,
        (SELECT COUNT(*) FROM chamadas c WHERE c.sessao_id = s.sessao_id) AS chamadas,
        (SELECT COUNT(*) FROM tool_calls t WHERE t.sessao_id = s.sessao_id) AS tools,
        (SELECT SUM(CASE WHEN {db.ERRO_REAL} THEN 1 ELSE 0 END) FROM tool_calls t WHERE t.sessao_id = s.sessao_id) AS erros,
        (SELECT SUM(custo_usd) FROM chamadas c WHERE c.sessao_id = s.sessao_id) AS custo
    FROM s ORDER BY inicio DESC LIMIT ?"""
    linhas = [
        {
            "sessao": r["sessao_id"],
            "projeto": r["projeto"] or "?",
            "inicio": r["inicio"],
            # julianday() devolve ruído de ponto flutuante (29.9999982 s): arredonda, não trunca
            "duracao_s": round(r["duracao"] or 0),
            "ativo_s": round(r["ativo"] or 0),
            "prompts": r["prompts"],
            "chamadas": r["chamadas"],
            "tools": r["tools"],
            "erros_tool": r["erros"] or 0,
            "custo_usd": r["custo"],
        }
        for r in con.execute(sql, [*p, limite])
    ]
    por_sessao = _comportamento.por_sessao(con, [l["sessao"] for l in linhas]) if com_categoria else {}
    for l in linhas:  # categoria (T14) e one-shot (T15), calculados do que já está no banco
        c = por_sessao.get(l["sessao"], {})
        l["categoria"] = c.get("categoria")
        l["turnos_edicao"] = c.get("turnos_edicao", 0)
        l["retries"] = c.get("retries", 0)
        l["one_shot"] = c.get("one_shot")  # fração de 0 a 1; None se a sessão não teve edição
    return linhas


def tools(con: sqlite3.Connection, f: Filtro) -> list[dict]:
    """Uso por tool (`tc tools`). taxa_erro é uma fração de 0 a 1 e NÃO conta as recusas do usuário (T23).

    `recusadas`: tools que o usuário negou na tela de permissão. `categorias`: erros por categoria (design 6.4);
    `sem_categoria` = erros gravados antes da T23 (rode `tc ingest --tudo`).
    """
    where, p = f.sql()
    categorias: dict[str, dict[str, int]] = {}
    for r in con.execute(f"""
            SELECT tool, COALESCE(erro_categoria, 'sem_categoria') AS cat, COUNT(*) AS n FROM tool_calls
            WHERE erro = 1 AND {where} GROUP BY tool, cat""", p):
        categorias.setdefault(r["tool"], {})[r["cat"]] = r["n"]
    return [
        {
            "tool": r["tool"],
            "chamadas": r["n"],
            "erros": r["erros"],
            "taxa_erro": r["erros"] / r["n"] if r["n"] else None,
            "recusadas": categorias.get(r["tool"], {}).get("recusado_usuario", 0),
            "categorias": {c: n for c, n in categorias.get(r["tool"], {}).items() if c != "recusado_usuario"},
        }
        for r in con.execute(f"""
            SELECT tool, COUNT(*) AS n, SUM(CASE WHEN {db.ERRO_REAL} THEN 1 ELSE 0 END) AS erros FROM tool_calls
            WHERE {where} GROUP BY tool ORDER BY n DESC""", p)
    ]


def turnos_por_categoria(con: sqlite3.Connection, f: Filtro) -> list[dict]:
    """Turnos por categoria (T14) com one-shot (T15) e custo, das sessões com atividade no filtro (`tc comportamento`)."""
    return _comportamento.por_categoria(_comportamento.montar_turnos(con, ids_de_sessoes(con, f)))


# ---------- período (painel e tc web) ----------

def periodo(con: sqlite3.Connection, periodo: str, hoje: date, limites: dict, projeto: str | None = None,
            ferramenta: str | None = None, cache: dict | None = None) -> dict:
    """Todos os números de um período (`tc painel`). `cache` evita refazer o trabalho pesado se o banco não mudou."""
    f = filtro_do_periodo(periodo, hoje, projeto, ferramenta)
    desde = f.desde
    cond, p = f.sql()
    sem_periodo, p0 = Filtro(projeto=projeto, ferramenta=ferramenta).sql()

    r = con.execute(f"""SELECT COUNT(*) AS chamadas, COUNT(DISTINCT sessao_id) AS sessoes, SUM(custo_usd) AS custo,
                        SUM(input_tokens) AS entrada, SUM(output_tokens) AS saida,
                        SUM(cache_write_5m + cache_write_1h) AS cw, SUM(cache_read) AS cr,
                        SUM(erro_api = 0 AND custo_usd IS NULL) AS sem_preco
                        FROM chamadas WHERE {cond}""", p).fetchone()
    entrada, cw, cr = r["entrada"] or 0, r["cw"] or 0, r["cr"] or 0
    total_in = entrada + cw + cr
    resumo_ = {"chamadas": r["chamadas"], "sessoes": r["sessoes"], "custo": r["custo"], "entrada": entrada,
               "saida": r["saida"] or 0, "cache_escrito": cw, "cache_lido": cr,
               "cache_taxa": cr / total_in if total_in else None, "sem_preco": r["sem_preco"] or 0}

    dias = [dict(x) for x in con.execute(f"""SELECT date(ts, 'localtime') AS dia, SUM(custo_usd) AS custo,
                COUNT(*) AS chamadas FROM chamadas WHERE {sem_periodo} GROUP BY dia
                ORDER BY dia DESC LIMIT {DIAS_NO_PAINEL}""", p0)]
    projetos = [dict(x) for x in con.execute(f"""SELECT COALESCE(projeto, '?') AS projeto, SUM(custo_usd) AS custo,
                COUNT(DISTINCT sessao_id) AS sessoes FROM chamadas WHERE {cond}
                GROUP BY 1 ORDER BY SUM(custo_usd) DESC LIMIT 8""", p)]
    modelos = [dict(x) for x in con.execute(f"""SELECT COALESCE(modelo, '?') AS modelo, SUM(custo_usd) AS custo,
                COUNT(*) AS chamadas, SUM(cache_read) AS cr,
                SUM(input_tokens + cache_write_5m + cache_write_1h + cache_read) AS total
                FROM chamadas WHERE erro_api = 0 AND {cond} GROUP BY 1 ORDER BY SUM(custo_usd) DESC LIMIT 8""", p)]
    for m in modelos:
        m["cache_taxa"] = (m["cr"] or 0) / m["total"] if m["total"] else None
    lista_tools = [dict(x) for x in con.execute(f"""SELECT tool, COUNT(*) AS n,
                SUM(CASE WHEN {db.ERRO_REAL} THEN 1 ELSE 0 END) AS erros
                FROM tool_calls WHERE {cond} GROUP BY tool ORDER BY n DESC""", p)]
    mcp: dict[str, int] = {}
    for t in lista_tools:
        if t["tool"].startswith("mcp__"):
            servidor = t["tool"].split("__")[1] if t["tool"].count("__") >= 2 else t["tool"]
            mcp[servidor] = mcp.get(servidor, 0) + t["n"]
    shell = [dict(x) for x in con.execute(f"""SELECT subtipo, COUNT(*) AS n FROM tool_calls
                WHERE subtipo IS NOT NULL AND {cond} GROUP BY subtipo ORDER BY n DESC""", p)]

    # a parte pesada (turnos e diagnóstico) só é refeita quando o banco muda
    chave = (periodo, projeto, ferramenta, hoje, _versao(con))
    if cache is not None and chave in cache:
        atividades, recomendacoes = cache[chave]
    else:
        ids = ids_de_sessoes(con, f)
        turnos_ = [t for t in _comportamento.montar_turnos(con, ids)
                   if desde is None or (t.inicio and (_dia_local(t.inicio) or "") >= desde)]
        atividades = _comportamento.por_categoria(turnos_)
        recomendacoes = diagnostico.diagnosticar(con, limites, ids)
        if cache is not None:
            cache.clear()
            cache[chave] = (atividades, recomendacoes)

    return {"periodo": periodo, "resumo": resumo_, "dias": dias, "projetos": projetos, "modelos": modelos,
            "tools": lista_tools, "mcp": sorted(mcp.items(), key=lambda kv: -kv[1]), "shell": shell,
            "atividades": atividades, "recomendacoes": recomendacoes,
            "qualidade": qualidade.coletar(con, ferramenta), "modo": db.ler_modo(con)}


def por_hora(con: sqlite3.Connection, dia: date, projeto: str | None = None, ferramenta: str | None = None) -> dict:
    """Custo de um dia (local) por projeto e por hora local (24 posições). Chamadas sem custo só entram em `sem_preco`."""
    cond, p = Filtro(projeto=projeto, ferramenta=ferramenta).sql()
    por_projeto: dict[str, dict] = defaultdict(lambda: {"custo_usd": 0.0, "sessoes": set()})
    horas = [0.0] * 24
    total, sem_preco = 0.0, 0
    for r in con.execute(f"""SELECT ts, COALESCE(projeto, '?') AS projeto, sessao_id, custo_usd, erro_api FROM chamadas
                             WHERE date(ts, 'localtime') = ? AND {cond}""", [dia.isoformat(), *p]):
        if r["custo_usd"] is None:
            sem_preco += not r["erro_api"]
            continue
        t = diagnostico._t(r["ts"]) if r["ts"] else None
        if t is None:
            continue
        horas[t.astimezone().hour] += r["custo_usd"]
        por_projeto[r["projeto"]]["custo_usd"] += r["custo_usd"]
        por_projeto[r["projeto"]]["sessoes"].add(r["sessao_id"])
        total += r["custo_usd"]
    projetos = [{"projeto": k, "custo_usd": v["custo_usd"], "sessoes": len(v["sessoes"])} for k, v in por_projeto.items()]
    projetos.sort(key=lambda x: (-x["custo_usd"], x["projeto"]))
    return {"projetos": projetos, "horas": horas, "custo_usd": total, "sem_preco": sem_preco}


def custo_por_tipo(con: sqlite3.Connection, f: Filtro) -> dict:
    """Custo dividido por tipo de token (input, output, gravação e leitura de cache).

    Só dá para dividir o custo `calculado` (tokens x `precos.py`). O custo `registrado` pelo log vem num número só e fica
    em `registrado_usd`, sem divisão. `sem_cache_usd` é o que as mesmas chamadas calculadas custariam se todo o contexto
    fosse cobrado como input (sem gravação nem leitura de cache): uma conta com os tokens reais, não uma estimativa de tokens.
    """
    where, p = f.sql()
    tipos = {"output": 0.0, "cache_write": 0.0, "cache_read": 0.0, "input": 0.0}
    registrado, n_registrado, sem_cache = 0.0, 0, 0.0
    for r in con.execute(f"""SELECT modelo, input_tokens AS i, output_tokens AS o, cache_write_5m AS w5,
                                    cache_write_1h AS w1, cache_read AS cr, custo_usd, origem_custo
                             FROM chamadas WHERE erro_api = 0 AND custo_usd IS NOT NULL AND {where}""", p):
        preco = achar_preco(r["modelo"], r["i"] + r["w5"] + r["w1"] + r["cr"]) if r["origem_custo"] == "calculado" else None
        if preco is None:
            registrado += r["custo_usd"]
            n_registrado += 1
            continue
        p_in, p_w5, p_w1, p_read, p_out = preco
        tipos["input"] += r["i"] * p_in / 1e6
        tipos["output"] += r["o"] * p_out / 1e6
        tipos["cache_write"] += (r["w5"] * p_w5 + r["w1"] * p_w1) / 1e6
        tipos["cache_read"] += r["cr"] * p_read / 1e6
        sem_cache += ((r["i"] + r["w5"] + r["w1"] + r["cr"]) * p_in + r["o"] * p_out) / 1e6
    return {"tipos": tipos, "calculado_usd": sum(tipos.values()), "registrado_usd": registrado,
            "chamadas_registradas": n_registrado, "sem_cache_usd": sem_cache}


def projetos_do_banco(con: sqlite3.Connection, ferramenta: str | None = None, limite: int = 12) -> list[str]:
    """Nomes de projeto com chamadas, do mais recente para o mais antigo (as opções do filtro de projeto)."""
    cond, p = ("AND ferramenta = ?", [ferramenta]) if ferramenta else ("", [])
    return [r[0] for r in con.execute(f"""SELECT projeto FROM chamadas WHERE projeto IS NOT NULL {cond}
                                          GROUP BY projeto ORDER BY MAX(ts) DESC, projeto LIMIT ?""", [*p, limite])]


def custo_por_projeto(con: sqlite3.Connection, f: Filtro) -> list[dict]:
    """Custo e número de sessões por projeto no filtro, do mais caro para o mais barato (só projetos com custo)."""
    where, p = f.sql()
    return [{"projeto": r["projeto"], "custo_usd": r["custo"], "sessoes": r["n"]} for r in con.execute(f"""
            SELECT COALESCE(projeto, '?') AS projeto, SUM(custo_usd) AS custo, COUNT(DISTINCT sessao_id) AS n
            FROM chamadas WHERE {where} GROUP BY 1 HAVING SUM(custo_usd) IS NOT NULL
            ORDER BY SUM(custo_usd) DESC, 1""", p)]


def custo_por_dia(con: sqlite3.Connection, f: Filtro, hoje: date) -> dict:
    """Custo de cada dia local do filtro, até `hoje`, com os dias sem custo valendo 0 (o gráfico "gasto por dia").

    Mostra no máximo `DIAS_NO_GRAFICO` dias, os mais recentes; `truncado` avisa quando houve corte.
    """
    where, p = f.sql()
    custos = {r[0]: r[1] or 0.0 for r in con.execute(
        f"SELECT date(ts, 'localtime'), SUM(custo_usd) FROM chamadas WHERE {where} GROUP BY 1", p)}
    inicio = date.fromisoformat(f.desde) if f.desde else (date.fromisoformat(min(custos)) if custos else hoje)
    inicio = min(inicio, hoje)
    dias = [inicio + timedelta(days=k) for k in range((hoje - inicio).days + 1)]
    truncado = len(dias) > DIAS_NO_GRAFICO
    return {"dias": [{"dia": d.isoformat(), "custo_usd": custos.get(d.isoformat(), 0.0)}
                     for d in dias[-DIAS_NO_GRAFICO:]], "truncado": truncado}


def visao(con: sqlite3.Connection, periodo: str, hoje: date, momento: datetime, limites: dict,
          projeto: str | None = None, ferramenta: str | None = None) -> dict:
    """Tudo que a tela do `tc web` (Hoje, 7 dias, 30 dias, mês, tudo) mostra, no `momento` dado (UTC, com fuso).

    `periodo` é um de `PERIODOS`; os dias são os locais. `sessoes` traz as `LINHAS_NA_TABELA` mais recentes (de hoje, em ordem
    cronológica; dos demais períodos, da mais nova para a mais antiga), com o modelo principal (o de maior custo), a taxa de
    cache, se está em andamento (atividade nos últimos `live_sessao_ativa_min`) e os sinais do diagnóstico. Os totais de
    duração e tempo ativo somam TODAS as sessões do período (a mesma conta do `tc sessoes`). `serie` é o gasto por hora (hoje)
    ou por dia (os demais períodos).
    """
    if periodo not in PERIODOS:
        raise ValueError(f"período inválido: {periodo!r}")
    desde = inicio_do_periodo(periodo, hoje)
    f = Filtro(desde=desde, ate=hoje.isoformat() if periodo == "hoje" else None, projeto=projeto, ferramenta=ferramenta)
    where, p = f.sql()
    r = con.execute(f"""SELECT COUNT(*) AS chamadas, SUM(erro_api) AS erros_api, SUM(input_tokens) AS inp,
                               SUM(output_tokens) AS outp, SUM(cache_write_5m + cache_write_1h) AS cw, SUM(cache_read) AS cr,
                               SUM(custo_usd) AS custo, SUM(custo_usd IS NULL AND erro_api = 0) AS sem_preco,
                               SUM(turno IS NULL) AS sem_sequencia
                        FROM chamadas WHERE {where}""", p).fetchone()
    entrada, cw, cr = r["inp"] or 0, r["cw"] or 0, r["cr"] or 0
    t = con.execute(f"""SELECT COUNT(*) AS n, SUM(CASE WHEN {db.ERRO_REAL} THEN 1 ELSE 0 END) AS erros,
                               SUM(erro = 1 AND erro_categoria = 'recusado_usuario') AS recusadas,
                               SUM(erro = 1 AND erro_categoria IS NULL) AS sem_categoria
                        FROM tool_calls WHERE {where}""", p).fetchone()
    prompts = con.execute(f"SELECT COUNT(*) FROM eventos WHERE tipo = 'prompt' AND {where}", p).fetchone()[0]
    ferramentas = [x[0] for x in con.execute(f"SELECT DISTINCT ferramenta FROM chamadas WHERE {where}", p)]
    modelos = [dict(x) for x in con.execute(f"""SELECT COALESCE(modelo, '?') AS modelo, SUM(custo_usd) AS custo_usd,
                    COUNT(*) AS chamadas, SUM(custo_usd IS NULL) AS sem_preco
                    FROM chamadas WHERE erro_api = 0 AND {where} GROUP BY 1
                    ORDER BY COALESCE(SUM(custo_usd), -1) DESC, COUNT(*) DESC""", p)]

    todas = sessoes(con, f, 1_000_000, com_categoria=False)      # da mais recente para a mais antiga
    lista = todas[:LINHAS_NA_TABELA]
    if periodo == "hoje":
        lista = lista[::-1]
    ids = [s["sessao"] for s in lista]
    por_modelo: dict[str, list] = defaultdict(list)
    cache_sessao: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ultimos: dict[str, str] = {}
    for lote in diagnostico._em_lotes(ids):
        marcas = ",".join("?" * len(lote))
        for x in con.execute(f"""SELECT sessao_id, COALESCE(modelo, '?') AS modelo, COUNT(*) AS n,
                                        COALESCE(SUM(custo_usd), 0) AS custo, SUM(cache_read) AS cr,
                                        SUM(input_tokens + cache_write_5m + cache_write_1h + cache_read) AS total
                                 FROM chamadas WHERE erro_api = 0 AND sessao_id IN ({marcas})
                                 GROUP BY sessao_id, modelo""", lote):
            por_modelo[x["sessao_id"]].append((x["custo"], x["n"], x["modelo"]))
            cache_sessao[x["sessao_id"]][0] += x["cr"] or 0
            cache_sessao[x["sessao_id"]][1] += x["total"] or 0
        ultimos.update(con.execute(f"""SELECT sessao_id, MAX(ts) FROM eventos WHERE tipo = 'atividade'
                                       AND sessao_id IN ({marcas}) GROUP BY sessao_id""", lote).fetchall())
    sinais: dict[str, list[dict]] = defaultdict(list)
    for rec in diagnostico.diagnosticar(con, limites, ids):
        sinais[rec["sessao"]].append({"sinal": rec["sinal"], "severidade": rec["severidade"], "dados": rec["dados"]})
    janela = timedelta(minutes=limites["live_sessao_ativa_min"])
    for s in lista:
        modelos_s = sorted(por_modelo.get(s["sessao"], []), reverse=True)
        s["modelo"] = modelos_s[0][2] if modelos_s else None
        lido, total = cache_sessao.get(s["sessao"], (0, 0))
        s["cache_taxa"] = lido / total if total else None
        ultimo = diagnostico._t(ultimos[s["sessao"]]) if ultimos.get(s["sessao"]) else None
        s["ultimo"] = ultimos.get(s["sessao"])
        s["em_andamento"] = bool(ultimo and timedelta(0) <= momento - ultimo <= janela)
        s["sinais"] = sinais.get(s["sessao"], [])

    if periodo == "hoje":
        serie = {"tipo": "hora", "valores": por_hora(con, hoje, projeto, ferramenta)["horas"]}
    else:
        serie = {"tipo": "dia", **custo_por_dia(con, f, hoje)}
    arquivos = con.execute("SELECT MAX(processado_em), SUM(linhas_ruins) FROM arquivos").fetchone()
    return {
        "periodo": periodo, "hoje": hoje.isoformat(), "desde": desde, "momento": momento.isoformat(),
        "modo": db.ler_modo(con), "ferramentas": ferramentas, "projeto": projeto,
        "projetos_disponiveis": projetos_do_banco(con, ferramenta),
        "custo_usd": r["custo"], "sem_preco": r["sem_preco"] or 0,
        "chamadas": r["chamadas"], "erros_api": r["erros_api"] or 0, "prompts": prompts,
        "tokens": {"input": entrada, "output": r["outp"] or 0, "cache_write": cw, "cache_read": cr},
        "cache_taxa": cr / (entrada + cw + cr) if entrada + cw + cr else None,
        "tools": {"chamadas": t["n"], "erros": t["erros"] or 0, "recusadas": t["recusadas"] or 0},
        "sem_categoria": t["sem_categoria"] or 0, "sem_sequencia": r["sem_sequencia"] or 0,
        "ativo_s": sum(s["ativo_s"] for s in todas), "duracao_s": sum(s["duracao_s"] for s in todas),
        "serie": serie, "projetos": custo_por_projeto(con, f), "custo_por_tipo": custo_por_tipo(con, f),
        "modelos": modelos, "lista_tools": tools(con, f),
        "sessoes": lista, "sessoes_total": len(todas), "sessoes_cortadas": max(0, len(todas) - len(lista)),
        "ultimo_ingest": arquivos[0], "linhas_ilegiveis": arquivos[1] or 0,
    }


# ---------- sequência da sessão (T22; telas da T27 e T28) ----------

def turnos(con: sqlite3.Connection, sessao_id: str) -> dict:
    """Turnos de uma sessão, em ordem: horário do prompt, chamadas, tools, erros (sem recusas) e custo.

    `disponivel` False = a sessão foi gravada antes da T22 (colunas `turno`/`seq` vazias): rode `tc ingest --tudo`.
    O turno N começa no N-ésimo prompt da thread principal (o ingest numera os dois no mesmo ponto); turno 0 é o que
    veio antes do primeiro prompt.
    """
    disponivel = con.execute("""SELECT EXISTS (SELECT 1 FROM chamadas WHERE sessao_id = ? AND turno IS NOT NULL)
                                    OR EXISTS (SELECT 1 FROM tool_calls WHERE sessao_id = ? AND turno IS NOT NULL)""",
                             (sessao_id, sessao_id)).fetchone()[0]
    if not disponivel:
        return {"sessao": sessao_id, "disponivel": False, "turnos": []}
    prompts = [r[0] for r in con.execute(
        "SELECT ts FROM eventos WHERE tipo = 'prompt' AND sessao_id = ? ORDER BY ts", (sessao_id,))]
    por_turno: dict[int, dict] = {}

    def turno(n: int) -> dict:
        if n not in por_turno:
            por_turno[n] = {"turno": n, "prompt_ts": prompts[n - 1] if 1 <= n <= len(prompts) else None,
                            "inicio": None, "fim": None, "chamadas": 0, "tools": 0, "erros": 0, "recusadas": 0,
                            "custo_usd": None, "sem_preco": 0}
        return por_turno[n]

    for r in con.execute("""SELECT turno, MIN(ts) AS ini, MAX(ts) AS fim, COUNT(*) AS n, SUM(custo_usd) AS custo,
                                   SUM(custo_usd IS NULL AND erro_api = 0) AS sem_preco
                            FROM chamadas WHERE sessao_id = ? AND turno IS NOT NULL GROUP BY turno""", (sessao_id,)):
        t = turno(r["turno"])
        t.update(inicio=r["ini"], fim=r["fim"], chamadas=r["n"], custo_usd=r["custo"], sem_preco=r["sem_preco"] or 0)
    for r in con.execute(f"""SELECT turno, MIN(ts) AS ini, MAX(ts) AS fim, COUNT(*) AS n,
                                    SUM(CASE WHEN {db.ERRO_REAL} THEN 1 ELSE 0 END) AS erros,
                                    SUM(erro_categoria = 'recusado_usuario') AS recusadas
                             FROM tool_calls WHERE sessao_id = ? AND turno IS NOT NULL GROUP BY turno""", (sessao_id,)):
        t = turno(r["turno"])
        t["tools"], t["erros"], t["recusadas"] = r["n"], r["erros"] or 0, r["recusadas"] or 0
        t["inicio"] = min(x for x in (t["inicio"], r["ini"]) if x)
        t["fim"] = max(x for x in (t["fim"], r["fim"]) if x)
    for n in range(1, len(prompts) + 1):   # prompt sem nenhuma chamada (interrompido) continua sendo um turno
        turno(n)
    for t in por_turno.values():
        if t["prompt_ts"]:
            t["inicio"] = min(x for x in (t["inicio"], t["prompt_ts"]) if x)
            t["fim"] = t["fim"] or t["prompt_ts"]
    return {"sessao": sessao_id, "disponivel": True, "turnos": [por_turno[n] for n in sorted(por_turno)]}


def _ms_entre(a: str | None, b: str | None) -> int | None:
    ta, tb = (diagnostico._t(a) if a else None), (diagnostico._t(b) if b else None)
    if ta is None or tb is None:
        return None
    return round((tb - ta).total_seconds() * 1000)


def sequencia(con: sqlite3.Connection, sessao_id: str, turno: int) -> dict:
    """Passos de um turno em ordem (`seq`): o prompt, cada chamada ao modelo e cada tool, com início e duração no turno.

    Só o que já está gravado: o prompt aparece só como horário e rótulo; o arquivo, só pelo hash (`alvo_hash`); o shell,
    só pela classe (`subtipo`); erro, pela categoria e o código de saída. `inicio_ms` é a distância do começo do turno;
    `duracao_ms` da tool vai do pedido ao resultado (inclui a espera por aprovação, design 6.5). O custo fica nas chamadas
    ao modelo; a tool aponta para a chamada que a pediu (`chamada_id`).
    """
    info = turnos(con, sessao_id)
    t = next((x for x in info["turnos"] if x["turno"] == turno), None)
    if t is None:
        return {"sessao": sessao_id, "turno": turno, "disponivel": info["disponivel"], "existe": False,
                "inicio": None, "fim": None, "duracao_ms": None, "passos": []}
    passos: list[dict] = []
    if t["prompt_ts"]:
        rotulo = con.execute("SELECT rotulo FROM eventos WHERE tipo = 'prompt' AND sessao_id = ? AND ts = ? LIMIT 1",
                             (sessao_id, t["prompt_ts"])).fetchone()
        passos.append({"tipo": "prompt", "seq": None, "ts": t["prompt_ts"], "rotulo": rotulo[0] if rotulo else None})
    for r in con.execute("""SELECT id, seq, ts, modelo, input_tokens, output_tokens, cache_write_5m, cache_write_1h,
                                   cache_read, custo_usd, origem_custo, erro_api, subagente, subagente_id
                            FROM chamadas WHERE sessao_id = ? AND turno = ?""", (sessao_id, turno)):
        passos.append({"tipo": "modelo", "seq": r["seq"], "ts": r["ts"], "id": r["id"], "modelo": r["modelo"],
                       "input": r["input_tokens"], "output": r["output_tokens"],
                       "cache_write": r["cache_write_5m"] + r["cache_write_1h"], "cache_read": r["cache_read"],
                       "custo_usd": r["custo_usd"], "origem_custo": r["origem_custo"], "erro_api": bool(r["erro_api"]),
                       "no_subagente": bool(r["subagente"]), "subagente_id": r["subagente_id"]})
    for r in con.execute("""SELECT id, seq, ts, tool, erro, erro_categoria, exit_code, duracao_ms, alvo_hash, subtipo,
                                   leitura_ini, leitura_qtd, servidor_mcp, chamada_id, subagente, subagente_id
                            FROM tool_calls WHERE sessao_id = ? AND turno = ?""", (sessao_id, turno)):
        passos.append({"tipo": "tool", "seq": r["seq"], "ts": r["ts"], "id": r["id"], "tool": r["tool"],
                       "erro": r["erro"], "erro_real": db.eh_erro_real(r["erro"], r["erro_categoria"]),
                       "erro_categoria": r["erro_categoria"], "exit_code": r["exit_code"], "duracao_ms": r["duracao_ms"],
                       "alvo_hash": r["alvo_hash"], "subtipo": r["subtipo"], "leitura_ini": r["leitura_ini"],
                       "leitura_qtd": r["leitura_qtd"], "servidor_mcp": r["servidor_mcp"], "chamada_id": r["chamada_id"],
                       "no_subagente": bool(r["subagente"]), "subagente_id": r["subagente_id"]})
    # o prompt abre o turno; depois, a ordem gravada pelo ingest (seq), com o horário como desempate
    passos.sort(key=lambda x: (x["tipo"] != "prompt", x["seq"] if x["seq"] is not None else float("inf"), x["ts"]))
    fim = diagnostico._t(t["fim"]) if t["fim"] else None
    for x in passos:
        x["inicio_ms"] = _ms_entre(t["inicio"], x["ts"])
        comeco = diagnostico._t(x["ts"]) if x["ts"] else None
        if x["tipo"] == "tool" and x["duracao_ms"] is not None and comeco is not None:
            fim_passo = comeco + timedelta(milliseconds=x["duracao_ms"])   # a tool pode terminar depois da última linha
            if fim is None or fim_passo > fim:
                fim = fim_passo
    fim_iso = fim.isoformat().replace("+00:00", "Z") if fim else None
    return {"sessao": sessao_id, "turno": turno, "disponivel": info["disponivel"], "existe": True,
            "inicio": t["inicio"], "fim": fim_iso, "duracao_ms": _ms_entre(t["inicio"], fim_iso), "passos": passos}
