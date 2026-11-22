"""Painel do `tc painel`, tela do `tc live` e árvore do `tc sessao`, desenhados com rich (única dependência de runtime, usada só aqui).

Duas camadas: `coletar` só consulta o banco e devolve números; `renderizar` transforma isso em rich.
O painel mostra apenas números e metadados já gravados (nenhum texto de conversa). Inspirado no painel
do CodeBurn: abas de período, resumo no topo e uma grade de caixas.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from . import agora, comportamento, db, diagnostico, qualidade
from .fontes import claude_code
from .formato import fmt_tokens, fmt_usd

try:  # só existe no Windows; no resto usamos select + termios
    import msvcrt
except ImportError:  # pragma: no cover - depende do sistema
    msvcrt = None

PERIODOS = (("hoje", "Hoje"), ("7d", "7 dias"), ("30d", "30 dias"), ("mes", "Mês"), ("tudo", "Tudo"))
NOMES = dict(PERIODOS)
DIAS_NO_PAINEL = 8
GRADIENTE = ("#5f87ff", "#5fafff", "#5fd7d7", "#d7d75f", "#ffaf5f", "#ff875f")  # azul -> laranja
SEVERIDADE = {"alta": ("▲", "bold red"), "media": ("●", "dark_orange"), "baixa": ("○", "grey62")}
CONFIANCA = {"alta": "green", "media": "yellow", "baixa": "red", "sem_dados": "grey62"}


# ---------- coleta ----------

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


def _filtro(desde: str | None, projeto: str | None, ferramenta: str | None) -> tuple[str, list]:
    cond, params = [], []
    if desde:
        cond.append("date(ts, 'localtime') >= ?")
        params.append(desde)
    if projeto:
        cond.append("projeto = ?")
        params.append(projeto)
    if ferramenta:
        cond.append("ferramenta = ?")
        params.append(ferramenta)
    return (" AND ".join(cond) or "1=1"), params


def _dia_local(ts: str) -> str | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, AttributeError):
        return None


def coletar(con: sqlite3.Connection, periodo: str, hoje: date, limites: dict, projeto: str | None = None,
            ferramenta: str | None = None, cache: dict | None = None) -> dict:
    """Todos os números do painel para o período. `cache` evita refazer o trabalho pesado se o banco não mudou."""
    desde = inicio_do_periodo(periodo, hoje)
    cond, p = _filtro(desde, projeto, ferramenta)
    sem_periodo, p0 = _filtro(None, projeto, ferramenta)

    r = con.execute(f"""SELECT COUNT(*) AS chamadas, COUNT(DISTINCT sessao_id) AS sessoes, SUM(custo_usd) AS custo,
                        SUM(input_tokens) AS entrada, SUM(output_tokens) AS saida,
                        SUM(cache_write_5m + cache_write_1h) AS cw, SUM(cache_read) AS cr,
                        SUM(erro_api = 0 AND custo_usd IS NULL) AS sem_preco
                        FROM chamadas WHERE {cond}""", p).fetchone()
    entrada, cw, cr = r["entrada"] or 0, r["cw"] or 0, r["cr"] or 0
    total_in = entrada + cw + cr
    resumo = {"chamadas": r["chamadas"], "sessoes": r["sessoes"], "custo": r["custo"], "entrada": entrada,
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
    tools = [dict(x) for x in con.execute(f"""SELECT tool, COUNT(*) AS n, SUM(COALESCE(erro, 0)) AS erros
                FROM tool_calls WHERE {cond} GROUP BY tool ORDER BY n DESC""", p)]
    mcp: dict[str, int] = {}
    for t in tools:
        if t["tool"].startswith("mcp__"):
            servidor = t["tool"].split("__")[1] if t["tool"].count("__") >= 2 else t["tool"]
            mcp[servidor] = mcp.get(servidor, 0) + t["n"]
    shell = [dict(x) for x in con.execute(f"""SELECT subtipo, COUNT(*) AS n FROM tool_calls
                WHERE subtipo IS NOT NULL AND {cond} GROUP BY subtipo ORDER BY n DESC""", p)]

    # a parte pesada (turnos e diagnóstico) só é refeita quando o banco muda
    versao = tuple(con.execute("""SELECT (SELECT COUNT(*) FROM chamadas), (SELECT COUNT(*) FROM tool_calls),
                                         (SELECT COUNT(*) FROM eventos)""").fetchone())
    chave = (periodo, projeto, ferramenta, hoje, versao)
    if cache is not None and chave in cache:
        atividades, recomendacoes = cache[chave]
    else:
        sessoes = [x[0] for x in con.execute(f"SELECT DISTINCT sessao_id FROM eventos WHERE {cond}", p)]
        turnos = [t for t in comportamento.montar_turnos(con, sessoes)
                  if desde is None or (t.inicio and (_dia_local(t.inicio) or "") >= desde)]
        atividades = comportamento.por_categoria(turnos)
        recomendacoes = diagnostico.diagnosticar(con, limites, sessoes)
        if cache is not None:
            cache.clear()
            cache[chave] = (atividades, recomendacoes)

    return {"periodo": periodo, "resumo": resumo, "dias": dias, "projetos": projetos, "modelos": modelos,
            "tools": tools, "mcp": sorted(mcp.items(), key=lambda kv: -kv[1]), "shell": shell,
            "atividades": atividades, "recomendacoes": recomendacoes,
            "qualidade": qualidade.coletar(con, ferramenta), "modo": db.ler_modo(con)}


# ---------- desenho ----------

def barra(valor, maximo, largura: int) -> Text:
    """Barra horizontal com degradê azul -> laranja; frações em oitavos de bloco."""
    texto = Text(no_wrap=True)
    cheio = (valor or 0) / maximo * largura if maximo else 0
    inteiros = int(cheio)
    for i in range(inteiros):
        texto.append("█", style=GRADIENTE[min(len(GRADIENTE) - 1, i * len(GRADIENTE) // max(largura, 1))])
    resto = int((cheio - inteiros) * 8)
    if resto and inteiros < largura:
        texto.append(" ▏▎▍▌▋▊▉"[resto], style=GRADIENTE[min(len(GRADIENTE) - 1, inteiros * len(GRADIENTE) // max(largura, 1))])
        inteiros += 1
    texto.append(" " * (largura - inteiros))
    return texto


ROTULO = {"ratio": 1, "overflow": "ellipsis", "justify": "left"}   # coluna que estica e corta com reticências
BARRA = {"justify": "left"}                                          # coluna da barra (largura fixa)


def _grade(colunas: list[tuple[str, dict]]) -> Table:
    """Tabela sem borda; cada coluna é (cabeçalho, opções). Sem opções de alinhamento, a coluna fica à direita."""
    t = Table.grid(padding=(0, 1), expand=True)
    for _, opcoes in colunas:
        t.add_column(no_wrap=True, **{"justify": "right", **opcoes})
    cab = [Text(nome, style="dim") for nome, _ in colunas]
    if any(nome for nome, _ in colunas):
        t.add_row(*cab)
    return t


def _linha(t: Table, *celulas) -> None:
    t.add_row(*[c if isinstance(c, Text) else Text(str(c)) for c in celulas])


def _usd(v, sufixo: str = "") -> Text:
    return Text("-" if v is None else fmt_usd(v) + sufixo, style="yellow")


def _painel_dias(d: dict):
    t = _grade([("", {**BARRA, "width": 10}), ("dia", ROTULO), ("custo", {"width": 8}), ("chamadas", {"width": 8})])
    maximo = max((x["custo"] or 0 for x in d["dias"]), default=0)
    for x in d["dias"]:
        _linha(t, barra(x["custo"], maximo, 10), Text(f"{x['dia'][8:]}/{x['dia'][5:7]}", style="dim"),
               _usd(x["custo"]), str(x["chamadas"]))
    if not d["dias"]:
        _linha(t, "", Text("sem dados", style="dim"), "", "")
    return "Por dia", "#5f87ff", t


def _painel_projetos(d: dict):
    t = _grade([("", {**BARRA, "width": 8}), ("projeto", ROTULO), ("custo", {"width": 8}), ("sessões", {"width": 7})])
    maximo = max((x["custo"] or 0 for x in d["projetos"]), default=0)
    for x in d["projetos"][:6]:
        _linha(t, barra(x["custo"], maximo, 8), x["projeto"], _usd(x["custo"]), str(x["sessoes"]))
    if not d["projetos"]:
        _linha(t, "", Text("sem dados", style="dim"), "", "")
    return "Por projeto", "#5fd787", t


def _cor_one_shot(v) -> str:
    return "dim" if v is None else "green" if v >= 0.75 else "yellow" if v >= 0.5 else "dark_orange"


def _painel_atividades(d: dict):
    t = _grade([("categoria", ROTULO), ("custo", {"width": 8}), ("turnos", {"width": 6}), ("1-shot", {"width": 6})])
    for x in d["atividades"][:8]:
        shot = x["one_shot"]
        _linha(t, comportamento.ROTULOS[x["categoria"]], _usd(x["custo_usd"], "*" if x["chamadas_sem_custo"] else ""),
               str(x["turnos"]), Text("-" if shot is None else f"{shot:.0%}", style=_cor_one_shot(shot)))
    if not d["atividades"]:
        _linha(t, Text("sem dados", style="dim"), "", "", "")
    return "Por atividade", "#ffaf5f", t


def _painel_modelos(d: dict):
    t = _grade([("modelo", ROTULO), ("custo", {"width": 8}), ("cache", {"width": 5}), ("chamadas", {"width": 8})])
    for x in d["modelos"][:6]:
        taxa = x["cache_taxa"]
        _linha(t, x["modelo"].replace("claude-", ""), _usd(x["custo"]),
               Text("-" if taxa is None else f"{taxa:.0%}", style="green" if (taxa or 0) >= 0.8 else "yellow"),
               str(x["chamadas"]))
    if not d["modelos"]:
        _linha(t, Text("sem dados", style="dim"), "", "", "")
    return "Por modelo", "#d787ff", t


def _painel_diagnostico(d: dict):
    t = _grade([("", ROTULO)])
    recs = d["recomendacoes"]
    for r in recs[:5]:
        simbolo, estilo = SEVERIDADE[r["severidade"]]
        linha = Text(no_wrap=True, overflow="ellipsis")
        linha.append(f"{simbolo} ", style=estilo)
        linha.append(f"{r['sessao'][:8]} ", style="dim")
        linha.append(diagnostico.ROTULOS[r["sinal"]], style=estilo if r["severidade"] != "baixa" else "default")
        t.add_row(linha)
    if len(recs) > 5:
        t.add_row(Text(f"+ {len(recs) - 5} no `tc diagnostico`", style="dim"))
    if not recs:
        t.add_row(Text("✓ nenhum sinal no período", style="green"))
    return "Diagnóstico", "#ff6b6b", t


def _painel_tools(d: dict):
    t = _grade([("", {**BARRA, "width": 6}), ("tool", ROTULO), ("chamadas", {"width": 8}), ("erro", {"width": 4})])
    maximo = max((x["n"] for x in d["tools"]), default=0)
    for x in d["tools"][:8]:
        taxa = x["erros"] / x["n"] if x["n"] else 0
        _linha(t, barra(x["n"], maximo, 6), x["tool"], str(x["n"]),
               Text(f"{taxa:.0%}" if taxa else "", style="red" if taxa >= 0.1 else "dim"))
    if not d["tools"]:
        _linha(t, "", Text("sem dados", style="dim"), "", "")
    return "Tools mais usadas", "#5fd7d7", t


def _painel_shell(d: dict):
    t = _grade([("", {**BARRA, "width": 6}), ("comando", ROTULO), ("chamadas", {"width": 8})])
    maximo = max((x["n"] for x in d["shell"]), default=0)
    for x in d["shell"]:
        _linha(t, barra(x["n"], maximo, 6), x["subtipo"], str(x["n"]))
    if not d["shell"]:
        _linha(t, "", Text("sem uso de shell", style="dim"), "")
    return "Shell (por classe)", "#ffaf5f", t


def _painel_mcp(d: dict):
    t = _grade([("servidor", ROTULO), ("chamadas", {"width": 8})])
    for nome, n in d["mcp"][:8]:
        _linha(t, nome, str(n))
    if not d["mcp"]:
        t.add_row(Text("sem uso de MCP", style="dim"), Text(""))
    return "MCP", "#d787ff", t


def _painel_qualidade(d: dict):
    t = _grade([("", ROTULO)])
    t.add_row(Text(f"modo de custo: {d['modo']}", style="dim"))
    for q in d["qualidade"]:
        linha = Text(no_wrap=True, overflow="ellipsis")
        linha.append(f"{q['ferramenta']}: ", style="dim")
        linha.append(f"confiança {q['confianca']}", style=CONFIANCA.get(q["confianca"], "default"))
        t.add_row(linha)
        for motivo in q["motivos"][:2]:
            t.add_row(Text(f"· {motivo}", style="dim", no_wrap=True, overflow="ellipsis"))
        origem = f"{q['origem_registrado']} reg · {q['origem_calculado']} calc · {q['origem_sem_preco']} sem preço"
        t.add_row(Text(origem, style="dim", no_wrap=True, overflow="ellipsis"))
    if not d["qualidade"]:
        t.add_row(Text("sem dados", style="dim"))
    return "Qualidade dos dados", "#5fd787", t


TECLAS = "[1-5] período   [r] atualizar   [q] sair"


def _abas(periodo: str, largura: int) -> Table:
    """Abas de período. A dica de teclas só fica no topo se couber; senão vai para o rodapé."""
    t = Table.grid(expand=True)
    t.add_column(ratio=1, no_wrap=True)
    abas = Text(no_wrap=True)
    for chave, nome in PERIODOS:
        abas.append(f"[ {nome} ]" if chave == periodo else f"  {nome}  ", style="bold #ffaf5f" if chave == periodo else "dim")
    if largura >= 100:
        t.add_column(justify="right", no_wrap=True)
        t.add_row(abas, Text(TECLAS, style="dim"))
    else:
        t.add_row(abas)
    return t


def _resumo(d: dict) -> Panel:
    r = d["resumo"]
    linha1 = Text()
    linha1.append(NOMES[d["periodo"]], style="bold #ffaf5f")
    linha1.append("   ")
    linha1.append(fmt_usd(r["custo"]), style="bold yellow")
    linha1.append(" custo   ")
    linha1.append(f"{r['chamadas']}", style="bold")
    linha1.append(" chamadas   ")
    linha1.append(f"{r['sessoes']}", style="bold")
    linha1.append(" sessões   ")
    taxa = r["cache_taxa"]
    linha1.append("-" if taxa is None else f"{taxa:.0%}", style="bold green")
    linha1.append(" cache hit")
    if r["sem_preco"]:
        linha1.append(f"   * {r['sem_preco']} chamada(s) sem preço", style="red")
    linha2 = Text(f"{fmt_tokens(r['entrada'])} entrada   {fmt_tokens(r['saida'])} saída   "
                  f"{fmt_tokens(r['cache_lido'])} cache lido   {fmt_tokens(r['cache_escrito'])} cache gravado", style="dim")
    return Panel(Group(linha1, linha2), title="token-controler", title_align="left", border_style="#ffaf5f", padding=(0, 1))


PAINEIS = (_painel_dias, _painel_projetos, _painel_atividades,
           _painel_modelos, _painel_diagnostico, _painel_tools,
           _painel_shell, _painel_mcp, _painel_qualidade)


def renderizar(d: dict, largura: int, hora: datetime | None = None, intervalo: int | None = None) -> Group:
    """Painel completo para uma tela com `largura` colunas: 3 colunas de caixas, 2 ou 1 se estreito."""
    colunas = 3 if largura >= 110 else 2 if largura >= 72 else 1
    caixas = [construir(d) for construir in PAINEIS]
    grade = Table.grid(expand=True, padding=(0, 0))
    for _ in range(colunas):
        grade.add_column(ratio=1)
    for inicio in range(0, len(caixas), colunas):
        grupo = caixas[inicio:inicio + colunas]
        altura = max(len(t.rows) for _, _, t in grupo) + 2  # linhas do conteúdo + borda: caixas da fileira iguais
        celulas = [Panel(t, title=titulo, title_align="left", border_style=cor, padding=(0, 1), height=altura)
                   for titulo, cor, t in grupo]
        celulas += [Text("")] * (colunas - len(celulas))
        grade.add_row(*celulas)
    rodape = Text(f"atualizado {(hora or datetime.now()):%H:%M:%S}"
                  + (f" · a cada {intervalo}s" if intervalo else "")
                  + (f" · {TECLAS}" if largura < 100 else "")
                  + " · * = há chamadas sem custo · detalhes: tc diagnostico, tc comportamento, tc qualidade", style="dim")
    return Group(_abas(d["periodo"], largura), _resumo(d), grade, rodape)


# ---------- teclado e laço principal ----------

def esperar_tecla(timeout: float) -> str | None:
    """Espera até `timeout` segundos por uma tecla. Sem terminal interativo, só dorme e devolve None."""
    if not sys.stdin or not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    fim = time.monotonic() + timeout
    if msvcrt is not None:
        while time.monotonic() < fim:
            if msvcrt.kbhit():
                tecla = msvcrt.getwch()
                if tecla in ("\x00", "\xe0"):   # tecla especial (setas...): descarta o 2º código
                    msvcrt.getwch()
                    return None
                return tecla
            time.sleep(0.05)
        return None
    import select  # pragma: no cover - POSIX
    pronto, _, _ = select.select([sys.stdin], [], [], timeout)  # pragma: no cover
    return sys.stdin.read(1) if pronto else None  # pragma: no cover


@contextmanager
def teclado():
    """No POSIX, lê teclas sem esperar Enter e restaura o terminal ao sair. No Windows não precisa de nada."""
    if msvcrt is not None or not sys.stdin or not sys.stdin.isatty():
        yield
        return
    import termios, tty  # pragma: no cover - POSIX
    fd = sys.stdin.fileno()  # pragma: no cover
    antigo = termios.tcgetattr(fd)  # pragma: no cover
    try:  # pragma: no cover
        tty.setcbreak(fd)
        yield
    finally:  # pragma: no cover
        termios.tcsetattr(fd, termios.TCSADRAIN, antigo)


def executar(con: sqlite3.Connection, limites: dict, apelidos: dict, intervalo: int = 15, periodo: str = "hoje",
             projeto: str | None = None, ferramenta: str | None = None, console: Console | None = None) -> None:
    """Laço do painel: lê os logs, desenha, espera uma tecla ou o intervalo. `q`, Esc ou Ctrl+C saem."""
    console = console or Console()
    ao_vivo = console.is_terminal
    cache: dict = {}
    with (Live(console=console, screen=True, auto_refresh=False) if ao_vivo else nullcontext()) as live, teclado():
        while True:
            claude_code.ingerir(con, apelidos=apelidos)
            dados = coletar(con, periodo, date.today(), limites, projeto, ferramenta, cache)
            quadro = renderizar(dados, console.size.width, intervalo=intervalo)
            if ao_vivo:
                live.update(quadro, refresh=True)
            else:
                console.print(quadro)
            tecla = esperar_tecla(intervalo)
            if tecla is not None and tecla.lower() == "q" or tecla in ("\x03", "\x1b"):
                return
            if tecla in ("1", "2", "3", "4", "5"):
                periodo = PERIODOS[int(tecla) - 1][0]


# ---------- árvore do `tc sessao` (T19) ----------

ESTILOS_ARVORE = {"titulo": "bold", "dim": "dim", "ok": "green", "erro": "bold red", "aviso": "dark_orange",
                  "custo": "yellow", "tool": "default", "alvo": "cyan", "prompt": "bold #ffaf5f"}


def _texto_da_linha(partes: list) -> Text:
    """Linha de estilo por parte; construída com Text (nunca com markup) para que um nome de arquivo com `[...]` não vire estilo."""
    linha = Text(overflow="fold")
    for texto, estilo in partes:
        linha.append(texto, style=ESTILOS_ARVORE.get(estilo, "default"))
    return linha


def _ramo(arvore: Tree, no) -> None:
    for filho in no.filhos:
        _ramo(arvore.add(_texto_da_linha(filho.partes)), filho)


def renderizar_sessao(visao) -> Group:
    """Cabeçalho, `rich.Tree` e rodapé de `detalhe.Visao`."""
    arvore = Tree(_texto_da_linha(visao.raiz.partes), guide_style="dim")
    _ramo(arvore, visao.raiz)
    return Group(*[_texto_da_linha(l) for l in visao.cabecalho], Text(""), arvore, Text(""),
                 *[_texto_da_linha(l) for l in visao.rodape])


def imprimir_sessao(visao, console: Console | None = None) -> None:
    (console or Console()).print(renderizar_sessao(visao))


# ---------- tela "agora" do `tc live` (T20) ----------

def _bloco(titulo: str, cor: str, conteudo) -> Panel:
    return Panel(conteudo, title=titulo, title_align="left", border_style=cor, padding=(0, 1))


def _bloco_sessoes(d: dict) -> Panel:
    t = _grade([("projeto", ROTULO), ("branch / início", {"width": 18, "justify": "left"}), ("última", {"width": 10}),
                ("custo", {"width": 8}), ("$/h", {"width": 8}), ("erros", {"width": 9}), ("sessão", {"width": 8, "justify": "left"})])
    for s in d["sessoes"]:
        ocioso = s["atras_s"] > 300
        _linha(t, Text(s["projeto"], style="bold"),
               Text(agora.coluna_branch(s), style="cyan" if s["branch"] else "dim", no_wrap=True, overflow="ellipsis"),
               Text(agora.atras(s["atras_s"]), style="dim" if ocioso else "green"),
               _usd(s["custo_usd"], "*" if s["sem_preco"] else ""), _usd(s["custo_hora_usd"]),
               Text(agora.erros(s), style="red" if s["erros_tool"] or s["erros_api"] else "dim"), Text(s["sessao"][:8], style="dim"))
    return _bloco(f"Sessões ativas (últimos {d['ativa_min']} min)", "#5fd787", t)


def _bloco_alertas(d: dict) -> Panel:
    linhas = []
    for a in d["alertas"]:
        simbolo, estilo = SEVERIDADE[a["severidade"]]
        l1 = Text(overflow="fold")
        l1.append(f"{simbolo} ", style=estilo)
        l1.append(a["projeto"], style="bold")
        l1.append(f" · {a['titulo']}: ", style=estilo if a["severidade"] != "baixa" else "default")
        l1.append(a["motivo"])
        if a["custo_usd"]:
            l1.append(f" · {fmt_usd(a['custo_usd'])}", style="yellow")
        l2 = Text("    → ", style="dim")
        l2.append(a["comando"], style="cyan")
        linhas += [l1, l2]
    if d["alertas_ocultos"]:
        linhas.append(Text(f"+ {d['alertas_ocultos']} alerta(s): abra `tc sessao <id>` das sessões acima", style="dim"))
    return _bloco(f"Alertas ({len(d['alertas']) + d['alertas_ocultos']})", "#ff6b6b", Group(*linhas))


def _bloco_projetos(d: dict) -> Panel:
    t = _grade([("", {**BARRA, "width": 8}), ("projeto", ROTULO), ("custo", {"width": 8}), ("sessões", {"width": 7})])
    maximo = max(p["custo_usd"] for p in d["projetos"])
    for p in d["projetos"][:8]:
        _linha(t, barra(p["custo_usd"], maximo, 8), p["projeto"], _usd(p["custo_usd"]), str(p["sessoes"]))
    return _bloco(f"Hoje por projeto · {fmt_usd(d['custo_hoje_usd'])}", "#5fafff", t)


def _bloco_horas(d: dict) -> Panel:
    maior = max(d["horas"])
    colunas = Text(no_wrap=True)
    for h, (v, c) in enumerate(zip(d["horas"], agora.mini_grafico(d["horas"]))):
        nivel = agora.BLOCOS.find(c)
        estilo = GRADIENTE[min(len(GRADIENTE) - 1, max(nivel, 0) * len(GRADIENTE) // len(agora.BLOCOS))] if v > 0 else "dim"
        colunas.append(c, style=("bold underline " if h == d["hora_atual"] else "") + estilo)
    pico = d["hora_de_pico"]
    return _bloco("Custo por hora do dia", "#ffaf5f", Group(
        colunas, Text(agora.eixo_do_grafico(), style="dim", no_wrap=True),
        Text(f"pico às {pico}h: {fmt_usd(d['horas'][pico])} · agora: {d['hora_atual']}h", style="dim", no_wrap=True)))


def renderizar_agora(d: dict, largura: int, hora: datetime | None = None, intervalo: int | None = None) -> Group:
    """Tela "agora" para `largura` colunas. Blocos sem dados somem; sem nada hoje, só o cabeçalho e um aviso."""
    topo = Text()
    topo.append("agora", style="bold #ffaf5f")
    topo.append("   hoje ")
    topo.append(fmt_usd(d["custo_hoje_usd"]), style="bold yellow")
    topo.append(f"   {len(d['sessoes'])} sessão(ões) ativa(s)", style="bold" if d["sessoes"] else "dim")
    if d["alertas"]:
        topo.append(f"   {len(d['alertas']) + d['alertas_ocultos']} alerta(s)", style="bold red")
    blocos = [Panel(topo, title="token-controler", title_align="left", border_style="#ffaf5f", padding=(0, 1))]
    if d["sessoes"]:
        blocos.append(_bloco_sessoes(d))
    else:
        blocos.append(Text(f"Nenhuma sessão ativa nos últimos {d['ativa_min']} min.", style="dim"))
    if d["alertas"]:
        blocos.append(_bloco_alertas(d))
    hoje = [b for b in (_bloco_projetos(d) if d["projetos"] else None, _bloco_horas(d) if d["custo_hoje_usd"] else None) if b]
    if len(hoje) == 2 and largura >= 100:
        grade = Table.grid(expand=True)
        grade.add_column(ratio=1)
        grade.add_column(ratio=1)
        grade.add_row(*hoje)
        blocos.append(grade)
    else:
        blocos += hoje
    if d["sem_preco_hoje"]:
        blocos.append(Text(f"* {d['sem_preco_hoje']} chamada(s) de hoje sem preço ficam fora dos custos", style="red"))
    blocos.append(Text(f"atualizado {(hora or datetime.now()):%H:%M:%S}" + (f" · a cada {intervalo}s" if intervalo else "")
                       + " · [r] atualizar · [q] sair · visão por período: tc painel", style="dim"))
    return Group(*blocos)


def executar_agora(con: sqlite3.Connection, limites: dict, apelidos: dict, intervalo: int = 15, projeto: str | None = None,
                   ferramenta: str | None = None, console: Console | None = None) -> None:
    """Laço da tela "agora": lê os logs, desenha, espera uma tecla ou o intervalo. `q`, Esc ou Ctrl+C saem; `r` atualiza."""
    console = console or Console()
    ao_vivo = console.is_terminal
    cache: dict = {}
    with (Live(console=console, screen=True, auto_refresh=False) if ao_vivo else nullcontext()) as live, teclado():
        while True:
            claude_code.ingerir(con, apelidos=apelidos)
            momento = agora.agora_utc()
            dados = agora.coletar(con, momento, limites, projeto, ferramenta, cache)
            quadro = renderizar_agora(dados, console.size.width, hora=momento.astimezone(), intervalo=intervalo)
            if ao_vivo:
                live.update(quadro, refresh=True)
            else:
                console.print(quadro)
            tecla = esperar_tecla(intervalo)
            if tecla is not None and tecla.lower() == "q" or tecla in ("\x03", "\x1b"):
                return
