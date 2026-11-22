"""`tc live` (T20): a tela "agora", focada no que está acontecendo nas sessões ativas.

Duas camadas, como no painel e no `tc sessao`: `coletar` só consulta o banco e devolve números e metadados (nenhum texto
de conversa); `em_texto` (aqui, só biblioteca padrão) e `painel.renderizar_agora` (rich) desenham os mesmos dados.

Blocos: sessões ativas (atividade nos últimos `live_sessao_ativa_min`), alertas, custo de hoje por projeto e custo por hora
do dia; blocos sem dados somem. Os alertas reaproveitam o que o `tc sessao` já mostra (`detalhe.analisar`, que usa as regras e
os limites do `tc diagnostico`) mais os sinais `sessao_cara` e `pressao_contexto` do diagnóstico. O branch git vem do log, lido
na hora, em memória, e nunca vai para o banco.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import consultas, detalhe, diagnostico
from .db import ERRO_REAL
from .fontes import claude_code

MAX_ALERTAS = 8
SINAIS_DO_DIAGNOSTICO = ("sessao_cara", "pressao_contexto")   # os demais já vêm do `tc sessao`
BLOCOS = "▁▂▃▄▅▆▇█"


def agora_utc() -> datetime:
    """Relógio da tela (um ponto só, para os testes poderem fixar a hora)."""
    return datetime.now(timezone.utc)


def _t(ts):
    return diagnostico._t(ts) if ts else None


# ---------- sessões ativas ----------

def _ativo_s(horarios: list[datetime], pausa_s: float) -> float:
    """Tempo ativo: soma dos intervalos entre eventos que não passam de `pausa_s` (a mesma regra do `tc sessoes`)."""
    ordenados = sorted(horarios)
    return sum(s for s in ((b - a).total_seconds() for a, b in zip(ordenados, ordenados[1:])) if s <= pausa_s)


def sessoes_ativas(con: sqlite3.Connection, momento: datetime, lim: dict, projeto: str | None = None,
                   ferramenta: str | None = None, ler_branch=claude_code.branch_do_log) -> list[dict]:
    """Sessões com atividade nos últimos `live_sessao_ativa_min`, da mais recente para a mais antiga."""
    corte = (momento - timedelta(minutes=lim["live_sessao_ativa_min"])).isoformat()
    cond, p = consultas.Filtro(projeto=projeto, ferramenta=ferramenta).sql()
    ids = [r[0] for r in con.execute(f"""
        SELECT DISTINCT sessao_id FROM (
            SELECT sessao_id, projeto, ferramenta, ts FROM eventos WHERE tipo = 'atividade'
            UNION ALL SELECT sessao_id, projeto, ferramenta, ts FROM chamadas)
        WHERE julianday(ts) >= julianday(?) AND {cond}""", [corte, *p])]
    saida = []
    for sessao in ids:
        atividade = [t for t in (_t(r[0]) for r in con.execute(
            "SELECT ts FROM eventos WHERE tipo = 'atividade' AND sessao_id = ?", (sessao,))) if t]
        chamadas = con.execute("""SELECT ts, projeto, custo_usd, erro_api, arquivo, subagente FROM chamadas
                                  WHERE sessao_id = ? ORDER BY ts""", (sessao,)).fetchall()
        tempos = atividade + [t for t in (_t(c["ts"]) for c in chamadas) if t]
        if not tempos:
            continue
        inicio, ultimo = min(tempos), max(tempos)
        custo = sum(c["custo_usd"] for c in chamadas if c["custo_usd"] is not None)
        ativo = _ativo_s(atividade or [t for t in (_t(c["ts"]) for c in chamadas) if t], lim["pausa_s"])
        arquivo = next((c["arquivo"] for c in reversed(chamadas) if not c["subagente"]), None)
        if arquivo is None:
            linha = con.execute("SELECT arquivo FROM eventos WHERE sessao_id = ? ORDER BY ts DESC LIMIT 1", (sessao,)).fetchone()
            arquivo = linha[0] if linha else None
        proj = next((c["projeto"] for c in reversed(chamadas) if c["projeto"]), None) or (con.execute(
            "SELECT MAX(projeto) FROM eventos WHERE sessao_id = ?", (sessao,)).fetchone()[0])
        erros_tool = con.execute(f"SELECT COALESCE(SUM({ERRO_REAL}), 0) FROM tool_calls WHERE sessao_id = ?", (sessao,)).fetchone()[0]
        saida.append({
            "sessao": sessao, "projeto": proj or "?", "inicio": inicio.isoformat(), "ultimo": ultimo.isoformat(),
            "atras_s": max(0.0, (momento - ultimo).total_seconds()), "ativo_s": ativo,
            "custo_usd": custo, "sem_preco": sum(c["custo_usd"] is None and not c["erro_api"] for c in chamadas),
            # só com 1 min de atividade para cima: antes disso, qualquer chamada extrapolada daria um valor sem sentido
            "custo_hora_usd": custo / (ativo / 3600) if ativo >= 60 else None,
            "erros_tool": erros_tool, "erros_api": sum(c["erro_api"] for c in chamadas),
            "branch": ler_branch(Path(arquivo)) if (ler_branch and arquivo) else None,
        })
    return sorted(saida, key=lambda s: s["atras_s"])


# ---------- alertas ----------

def alertas(con: sqlite3.Connection, ids: list[str], momento: datetime, lim: dict,
            cache: dict | None = None) -> tuple[list[dict], int]:
    """Alertas das sessões dadas, do mais para o menos grave; devolve (os `MAX_ALERTAS` primeiros, quantos ficaram de fora).

    O cálculo pesado (`detalhe.analisar` e o diagnóstico) só é refeito quando o banco muda; o filtro de recência, que
    depende do relógio, é refeito sempre.
    """
    versao = tuple(con.execute("""SELECT (SELECT COUNT(*) FROM chamadas), (SELECT COUNT(*) FROM tool_calls),
                                         (SELECT COUNT(*) FROM eventos)""").fetchone())
    chave = (tuple(sorted(ids)), versao)
    if cache is not None and chave in cache:
        todos = cache[chave]
    else:
        todos = []
        for sessao in ids:
            d = detalhe.analisar(con, sessao, lim)
            todos += [{"sessao": sessao, "projeto": d["projeto"] or "?", "tipo": p["tipo"], "titulo": detalhe.TITULOS[p["tipo"]],
                       "motivo": p["motivo"], "severidade": p["severidade"], "custo_usd": p["custo_usd"], "ts": p["ts"]}
                      for p in d["problemas"]]
        todos += [{"sessao": r["sessao"], "projeto": r["projeto"] or "?", "tipo": r["sinal"],
                   "titulo": diagnostico.ROTULOS[r["sinal"]], "motivo": r["evidencia"], "severidade": r["severidade"],
                   "custo_usd": r["impacto_usd"], "ts": None}
                  for r in diagnostico.diagnosticar(con, lim, ids) if r["sinal"] in SINAIS_DO_DIAGNOSTICO]
        if cache is not None:
            cache.clear()
            cache[chave] = todos
    janela = timedelta(minutes=lim["live_alerta_janela_min"])
    recentes = []
    for a in todos:
        t = _t(a["ts"])
        if t is None or momento - t <= janela:      # sem horário = estado da sessão (cache baixo, sessão cara): sempre vale
            recentes.append({**a, "comando": f"tc sessao {a['sessao'][:8]}"})
    recentes.sort(key=lambda a: (diagnostico.SEVERIDADES[a["severidade"]], -(a["custo_usd"] or 0), a["sessao"], a["tipo"]))
    return recentes[:MAX_ALERTAS], max(0, len(recentes) - MAX_ALERTAS)


# ---------- hoje ----------

# custo do dia por projeto e por hora: a consulta é a mesma do tc web (consultas.por_hora)
custo_de_hoje = consultas.por_hora


def mini_grafico(horas: list[float]) -> str:
    """Uma coluna por hora (24): altura proporcional ao maior valor; hora sem custo vira `·`."""
    maior = max(horas) if horas else 0
    saida = []
    for v in horas:
        if v <= 0 or not maior:
            saida.append("·")
        else:
            saida.append(BLOCOS[min(len(BLOCOS) - 1, int(v / maior * len(BLOCOS) - 1e-9))])
    return "".join(saida)


def eixo_do_grafico() -> str:
    """Rótulos das horas 0, 6, 12, 18 e 23 sob as 24 colunas do gráfico."""
    eixo = [" "] * 24
    for h in (0, 6, 12, 18):
        for k, c in enumerate(str(h)):
            eixo[h + k] = c
    eixo[23] = "h"
    return "".join(eixo)


# ---------- tudo junto ----------

def coletar(con: sqlite3.Connection, momento: datetime, lim: dict, projeto: str | None = None,
            ferramenta: str | None = None, cache: dict | None = None, ler_branch=claude_code.branch_do_log) -> dict:
    """Tudo que a tela "agora" mostra, no `momento` dado (data e hora de hoje vêm do horário local do notebook)."""
    local = momento.astimezone()
    hoje = custo_de_hoje(con, local.date(), projeto, ferramenta)
    sessoes = sessoes_ativas(con, momento, lim, projeto, ferramenta, ler_branch)
    lista, ocultos = alertas(con, [s["sessao"] for s in sessoes], momento, lim, cache) if sessoes else ([], 0)
    maior = max(range(24), key=lambda h: hoje["horas"][h]) if hoje["custo_usd"] else None
    return {
        "momento": momento.isoformat(), "hoje": local.date().isoformat(), "hora_atual": local.hour,
        "sessoes": sessoes, "alertas": lista, "alertas_ocultos": ocultos,
        "projetos": hoje["projetos"], "custo_hoje_usd": hoje["custo_usd"], "sem_preco_hoje": hoje["sem_preco"],
        "horas": hoje["horas"], "hora_de_pico": maior,
        "ativa_min": lim["live_sessao_ativa_min"], "alerta_janela_min": lim["live_alerta_janela_min"],
    }


# ---------- texto simples (sem rich) ----------

def _usd(v) -> str:
    return detalhe.usd(v)


def atras(segundos: float) -> str:
    """'agora', 'há 40s', 'há 2min10s'."""
    return "agora" if segundos < 10 else f"há {diagnostico._dur(segundos)}"


def coluna_branch(s: dict) -> str:
    """Branch git se o log trouxe; senão a hora de início da sessão."""
    if s["branch"]:
        return s["branch"]
    inicio = _t(s["inicio"])
    return f"desde {inicio.astimezone().strftime('%H:%M')}" if inicio else "-"


def erros(s: dict) -> str:
    texto = str(s["erros_tool"])
    return texto + (f" +{s['erros_api']} API" if s["erros_api"] else "")


def em_texto(dados: dict) -> str:
    """A mesma tela em texto puro (`tc live --simples` ou sem o rich). Blocos sem dados não aparecem."""
    hora = (_t(dados["momento"]) or datetime.now(timezone.utc)).astimezone().strftime("%H:%M:%S")
    linhas = [f"token-controler · agora · {hora} · hoje {_usd(dados['custo_hoje_usd'])}", ""]
    if dados["sessoes"]:
        linhas.append(f"Sessões ativas (últimos {dados['ativa_min']} min)")
        cab = ["projeto", "branch / início", "última atividade", "custo", "$/h", "erros", "sessão"]
        corpo = [[s["projeto"], coluna_branch(s), atras(s["atras_s"]), _usd(s["custo_usd"]) + ("*" if s["sem_preco"] else ""),
                  _usd(s["custo_hora_usd"]), erros(s), s["sessao"][:8]] for s in dados["sessoes"]]
        larg = [max(len(str(c)) for c in col) for col in zip(cab, *corpo)]
        for linha in (cab, *corpo):
            linhas.append("  " + "  ".join(str(c).ljust(w) for c, w in zip(linha, larg)).rstrip())
    else:
        linhas.append(f"Nenhuma sessão ativa nos últimos {dados['ativa_min']} min.")
    if dados["alertas"]:
        linhas += ["", "Alertas"]
        for a in dados["alertas"]:
            custo = f" · {_usd(a['custo_usd'])}" if a["custo_usd"] else ""
            linhas.append(f"  ⚠ {a['projeto']} · {a['titulo']}: {a['motivo']}{custo}  →  {a['comando']}")
        if dados["alertas_ocultos"]:
            linhas.append(f"  + {dados['alertas_ocultos']} alerta(s) (veja `tc sessao <id>` de cada sessão)")
    if dados["projetos"]:
        linhas += ["", "Hoje por projeto"]
        largura = max(len(p["projeto"]) for p in dados["projetos"])
        linhas += [f"  {p['projeto'].ljust(largura)}  {_usd(p['custo_usd']):>8}  {p['sessoes']} sessão(ões)" for p in dados["projetos"][:8]]
    if dados["custo_hoje_usd"]:
        pico = dados["hora_de_pico"]
        linhas += ["", "Custo por hora do dia" + (f" (pico às {pico}h: {_usd(dados['horas'][pico])})" if pico is not None else ""),
                   "  " + mini_grafico(dados["horas"]), "  " + eixo_do_grafico()]
    if dados["sem_preco_hoje"]:
        linhas += ["", f"* {dados['sem_preco_hoje']} chamada(s) de hoje sem preço ficam fora dos custos."]
    linhas += ["", "Ctrl+C para sair. Visão por período: `tc painel`; detalhe de uma sessão: `tc sessao <id>`."]
    return "\n".join(linhas)
