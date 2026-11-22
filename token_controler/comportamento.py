"""Comportamento das sessões: categoria por turno e sessão (T14) e taxa de one-shot (T15).

Tudo é calculado do que já está no banco (prompts, tool calls, subtipo do shell, hash do alvo,
custo), por regras determinísticas e sem chamar modelo. Este módulo nunca vê texto de prompt: a única
informação que vem dele é o rótulo `debug`, derivado na leitura (ver rotulos.py). Feature, Refatoração e
Brainstorm não existem. Inspirado na classificação por turno do CodeBurn, reimplementada.

Turno = um prompt e tudo que acontece até o próximo prompt, na thread principal. Chamadas de
subagentes entram no custo do turno em que ocorreram, mas não na sequência de tools do turno.
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass, field

from .comandos import BUILD, GIT, TESTE, TOOLS_SHELL
from .comandos import LEITURA as SHELL_LEITURA
from .rotulos import DEBUG

EDICAO = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit", "FileEditTool", "FileWriteTool"})
LEITURA = frozenset({"Read", "Grep", "Glob", "WebSearch", "WebFetch", "ToolSearch",
                     "FileReadTool", "GrepTool", "GlobTool"})
PLANO = frozenset({"EnterPlanMode", "ExitPlanMode"})
TAREFAS = frozenset({"TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop"})
DELEGACAO = frozenset({"Agent", "Task"})

ROTULOS = {
    "codificacao": "Codificação", "debug": "Debug", "exploracao": "Exploração", "testes": "Testes", "git": "Git",
    "build": "Build/Deploy", "delegacao": "Delegação", "planejamento": "Planejamento",
    "conversa": "Conversa", "geral": "Geral",
}
ORDEM = list(ROTULOS)  # ordem fixa, usada para desempatar a categoria da sessão
SEM_ALVO = "__sem_alvo__"


@dataclass
class Turno:
    sessao: str
    projeto: str | None
    indice: int                 # -1 = atividade antes do primeiro prompt
    chamadas: list = field(default_factory=list)   # tools da thread principal, em ordem
    custo: float = 0.0
    chamadas_api: int = 0
    chamadas_sem_custo: int = 0
    categoria: str = "conversa"
    retries: int = 0
    rotulo: str | None = None   # rótulo derivado do prompt que abriu o turno ('debug' ou None); nunca o texto
    inicio: str | None = None   # horário (UTC, ISO) do prompt que abriu o turno ou, sem prompt, da primeira atividade

    @property
    def tem_edicao(self) -> bool:
        return any(c["tool"] in EDICAO for c in self.chamadas)


def categoria_do_turno(chamadas: list[dict], rotulo: str | None = None) -> str:
    """Categoria de um turno pelas tools usadas e, no caso do Debug, pelo rótulo do prompt (nunca pelo texto).

    `debug` refina só turnos de codificação ou exploração (palavras de erro + tools, design §6.2).
    Testes, git, build, delegação e planejamento mantêm a categoria; sem tools, o turno segue `conversa`.
    """
    categoria = _categoria_pelas_tools(chamadas)
    if rotulo == DEBUG and categoria in ("codificacao", "exploracao"):
        return "debug"
    return categoria


def _categoria_pelas_tools(chamadas: list[dict]) -> str:
    tools = {c["tool"] for c in chamadas}
    if not tools:
        return "conversa"
    if tools & PLANO:
        return "planejamento"
    if tools & DELEGACAO:
        return "delegacao"
    shell = [c for c in chamadas if c["tool"] in TOOLS_SHELL]
    editou = bool(tools & EDICAO)
    if shell and not editou:
        subtipos = {c["subtipo"] for c in shell}
        for subtipo, categoria in ((TESTE, "testes"), (GIT, "git"), (BUILD, "build")):
            if subtipo in subtipos:
                return categoria
    if editou:
        return "codificacao"
    leu = bool(tools & LEITURA) or any(t.startswith("mcp__") for t in tools)
    if leu or (shell and all(c["subtipo"] == SHELL_LEITURA for c in shell)):
        return "exploracao"
    if tools & TAREFAS:
        return "planejamento"
    return "geral"


def contar_retries(chamadas: list[dict]) -> int:
    """Retries de um turno: o mesmo arquivo editado de novo depois de um comando de shell no meio.

    Shell só de leitura (rg, cat, git log...) não conta como verificação. Comando sem classe
    (linha antiga, sem subtipo) conta, o padrão conservador. Cada tool vale um passo.
    """
    ultima_edicao: dict[str, int] = {}
    ultima_verificacao = -1
    retries = 0
    for i, c in enumerate(chamadas):
        if c["tool"] in TOOLS_SHELL and c["subtipo"] != SHELL_LEITURA:
            ultima_verificacao = i
        if c["tool"] in EDICAO:
            chave = c["alvo"] or SEM_ALVO
            anterior = ultima_edicao.get(chave)
            if anterior is not None and anterior < ultima_verificacao < i:
                retries += 1
            ultima_edicao[chave] = i
    return retries


def categoria_da_sessao(turnos: list[Turno]) -> str:
    """Categoria mais frequente entre os turnos que usaram tools; empate: mais tool calls, depois a ordem fixa.

    Turnos só de conversa não disputam: `conversa` é a categoria da sessão apenas quando nenhum turno usou tool.
    """
    com_tools = [t for t in turnos if t.chamadas]
    if not com_tools:
        return "conversa"
    contagem: dict[str, list[int]] = {}
    for t in com_tools:
        c = contagem.setdefault(t.categoria, [0, 0])
        c[0] += 1
        c[1] += len(t.chamadas)
    return min(contagem, key=lambda cat: (-contagem[cat][0], -contagem[cat][1], ORDEM.index(cat)))


def _turno(turnos: dict, sessao: str, projeto, indice: int, ts: str | None = None) -> Turno:
    t = turnos.setdefault((sessao, indice), Turno(sessao, projeto, indice))
    t.projeto = t.projeto or projeto
    if ts and (t.inicio is None or ts < t.inicio):
        t.inicio = ts
    return t


def montar_turnos(con: sqlite3.Connection, sessoes: list[str]) -> list[Turno]:
    """Turnos das sessões dadas, com categoria, retries e custo."""
    turnos: dict[tuple, Turno] = {}
    for inicio in range(0, len(sessoes), 500):  # limite de variáveis do SQLite
        lote = sessoes[inicio:inicio + 500]
        marcas = ",".join("?" * len(lote))
        prompts: dict[str, list[str]] = {}
        for r in con.execute(f"""SELECT sessao_id, ts, rotulo FROM eventos
                                 WHERE tipo = 'prompt' AND sessao_id IN ({marcas}) ORDER BY ts""", lote):
            prompts.setdefault(r["sessao_id"], []).append(r["ts"])
            _turno(turnos, r["sessao_id"], None, len(prompts[r["sessao_id"]]) - 1, r["ts"]).rotulo = r["rotulo"]

        def indice(sessao: str, ts: str) -> int:
            return bisect.bisect_right(prompts.get(sessao, []), ts) - 1

        for r in con.execute(f"""SELECT sessao_id, projeto, ts, tool, erro, alvo_hash, subtipo FROM tool_calls
                                 WHERE subagente = 0 AND sessao_id IN ({marcas}) ORDER BY ts, rowid""", lote):
            t = _turno(turnos, r["sessao_id"], r["projeto"], indice(r["sessao_id"], r["ts"]), r["ts"])
            t.chamadas.append({"tool": r["tool"], "erro": r["erro"], "alvo": r["alvo_hash"],
                               "subtipo": r["subtipo"]})
        for r in con.execute(f"""SELECT sessao_id, projeto, ts, custo_usd, erro_api FROM chamadas
                                 WHERE sessao_id IN ({marcas})""", lote):
            t = _turno(turnos, r["sessao_id"], r["projeto"], indice(r["sessao_id"], r["ts"]), r["ts"])
            t.chamadas_api += 1
            if r["custo_usd"] is not None:
                t.custo += r["custo_usd"]
            elif not r["erro_api"]:
                t.chamadas_sem_custo += 1
    saida = sorted(turnos.values(), key=lambda t: (t.sessao, t.indice))
    for t in saida:
        t.categoria = categoria_do_turno(t.chamadas, t.rotulo)
        t.retries = contar_retries(t.chamadas)
    return saida


def resumo_da_sessao(turnos: list[Turno]) -> dict:
    """Categoria da sessão e one-shot: fração dos turnos de edição sem nenhum retry (None se não houve edição)."""
    com_edicao = [t for t in turnos if t.tem_edicao]
    sem_retry = sum(1 for t in com_edicao if t.retries == 0)
    return {
        "categoria": categoria_da_sessao(turnos),
        "turnos": len(turnos),
        "turnos_edicao": len(com_edicao),
        "retries": sum(t.retries for t in com_edicao),
        "one_shot": sem_retry / len(com_edicao) if com_edicao else None,
    }


def por_sessao(con: sqlite3.Connection, sessoes: list[str]) -> dict[str, dict]:
    """{sessao_id: resumo_da_sessao} para as sessões pedidas."""
    agrupado: dict[str, list[Turno]] = {}
    for t in montar_turnos(con, sessoes):
        agrupado.setdefault(t.sessao, []).append(t)
    return {s: resumo_da_sessao(ts) for s, ts in agrupado.items()}


def por_categoria(turnos: list[Turno]) -> list[dict]:
    """Uma linha por categoria de turno: turnos, turnos de edição, one-shot e custo. Mais cara primeiro."""
    acum: dict[str, dict] = {}
    for t in turnos:
        a = acum.setdefault(t.categoria, {"turnos": 0, "turnos_edicao": 0, "sem_retry": 0,
                                          "retries": 0, "custo_usd": 0.0, "chamadas_sem_custo": 0})
        a["turnos"] += 1
        a["custo_usd"] += t.custo
        a["chamadas_sem_custo"] += t.chamadas_sem_custo
        if t.tem_edicao:
            a["turnos_edicao"] += 1
            a["retries"] += t.retries
            a["sem_retry"] += t.retries == 0
    total = len(turnos) or 1
    return [
        {"categoria": cat, "turnos": a["turnos"], "fracao_turnos": a["turnos"] / total,
         "turnos_edicao": a["turnos_edicao"], "retries": a["retries"],
         "one_shot": a["sem_retry"] / a["turnos_edicao"] if a["turnos_edicao"] else None,
         "custo_usd": a["custo_usd"], "chamadas_sem_custo": a["chamadas_sem_custo"]}
        for cat, a in sorted(acum.items(), key=lambda kv: (-kv[1]["custo_usd"], ORDEM.index(kv[0])))
    ]
