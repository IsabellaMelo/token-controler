"""`tc sessao` (T19): o que aconteceu dentro de uma sessão, em árvore cronológica.

Duas camadas, como no painel: `analisar` só consulta o banco e devolve números e metadados (nenhum texto de
conversa); `montar` transforma isso numa árvore neutra (`No`) que o texto simples (`em_texto`, só biblioteca
padrão) e o rich (`painel.imprimir_sessao`) desenham do mesmo jeito.

Os problemas (⚠) reaproveitam as regras e os limites do `tc diagnostico` (`diagnostico.py`): falhas seguidas,
pausas, re-criação e taxa de cache. Só a releitura desnecessária é regra daqui, porque depende de saber se houve
edição ou compactação entre as leituras. Nada é estimado pelo texto: o custo de uma tool é o da chamada do modelo
que a pediu (dividido entre as tools pedidas juntas); o que ela devolveu entra no custo da chamada seguinte.
"""

from __future__ import annotations

import bisect
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import alvo, comportamento, diagnostico
from .comandos import TOOLS_SHELL
from .comportamento import DELEGACAO, EDICAO
from .fontes import claude_code
from .formato import fmt_pct, fmt_tokens
from .precos import achar_preco

LEITURAS_DE_ARQUIVO = alvo.LEITURAS
INFINITO = float("inf")
LINHAS_PADRAO_READ = 2000   # sem `limit`, o Read lê no máximo isto a partir do `offset`
COMANDO_MAX = 100      # tamanho máximo do comando mostrado com `--comandos`

TITULOS = {
    "falha_repetida": "Falha repetida", "releitura": "Releitura desnecessária", "pausa_longa": "Pausa longa",
    "cache_baixo": "Cache baixo", "recriacao_cache": "Re-criação de cache",
}
SUGESTOES = {
    "falha_repetida": "Depois de 2 falhas seguidas da mesma tool, pare e mude a abordagem em vez de repetir.",
    "releitura": "Evite reler arquivo sem mudança: peça para reaproveitar o que já foi lido ou leia só o trecho.",
    "pausa_longa": "Pausa longa expira o cache: retome em sessão nova ou antes do TTL do cache.",
    "cache_baixo": "Não mude o começo do contexto no meio da sessão e abra uma sessão nova ao trocar de assunto.",
    "recriacao_cache": "Evite mudar o começo do contexto (instruções, modelo) no meio do trabalho.",
}
SEVERIDADES = diagnostico.SEVERIDADES


# ---------- árvore neutra (dados de desenho, sem rich) ----------

# Uma parte de linha é (texto, estilo); estilos: titulo, dim, ok, erro, aviso, custo, tool, alvo, prompt


@dataclass
class No:
    """Um nó da árvore: uma linha de texto (lista de partes com estilo) e os nós filhos."""
    partes: list = field(default_factory=list)
    filhos: list = field(default_factory=list)

    def texto(self) -> str:
        return "".join(t for t, _ in self.partes)

    def add(self, *partes) -> "No":
        filho = No(list(partes))
        self.filhos.append(filho)
        return filho


@dataclass
class Visao:
    """O que `tc sessao` mostra: cabeçalho, árvore e rodapé (problemas, sugestões, notas)."""
    cabecalho: list = field(default_factory=list)   # linhas = listas de partes
    raiz: No = field(default_factory=No)
    rodape: list = field(default_factory=list)


# ---------- formatação ----------

def usd(v) -> str:
    """Dólar com 4 casas abaixo de US$ 0,01 (custo de uma tool costuma ser centavos de centavo)."""
    if v is None:
        return "-"
    return f"${v:,.2f}" if abs(v) >= 0.01 or v == 0 else f"${v:.4f}"


def dur_ms(ms) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{int(ms)}ms"
    if ms < 10_000:
        return f"{ms / 1000:.1f}s"
    return diagnostico._dur(ms / 1000)


def hora(ts: str | None) -> str:
    """Horário local (HH:MM:SS) de um timestamp ISO UTC."""
    t = diagnostico._t(ts) if ts else None
    return t.astimezone().strftime("%H:%M:%S") if t else "?"


# ---------- sessão ----------

def ultima_sessao(con: sqlite3.Connection) -> str | None:
    """ID da sessão com a atividade mais recente."""
    linha = con.execute("""SELECT sessao_id FROM (
                               SELECT sessao_id, MAX(ts) AS ultimo FROM eventos GROUP BY sessao_id
                               UNION ALL SELECT sessao_id, MAX(ts) FROM chamadas GROUP BY sessao_id)
                           ORDER BY ultimo DESC LIMIT 1""").fetchone()
    return linha[0] if linha else None


def _custo(chamadas: list[dict]) -> tuple[float, int]:
    """(custo somado das chamadas com custo, quantas chamadas ficaram sem custo)."""
    return (sum(c["custo_usd"] or 0.0 for c in chamadas),
            sum(c["custo_usd"] is None and not c["erro_api"] for c in chamadas))


def _atribuir_custo(chamadas: list[dict], tools: list[dict]) -> int:
    """Dá a cada tool o custo da chamada que a pediu (a última chamada da mesma thread até o horário da tool).

    Tools pedidas na mesma chamada dividem o custo igualmente. Devolve quantas chamadas pediram tools.
    """
    horarios = [c["ts"] for c in chamadas]
    grupos: dict[int, list[dict]] = defaultdict(list)
    for t in tools:
        i = bisect.bisect_right(horarios, t["ts"]) - 1
        t["_chamada"] = i
        grupos[i].append(t)
    for i, grupo in grupos.items():
        custo = chamadas[i]["custo_usd"] if i >= 0 else None
        for t in grupo:
            t["custo"] = None if custo is None else custo / len(grupo)
    return len([i for i in grupos if i >= 0])


def _custo_gravacao_cache(chamadas: list[dict]) -> tuple[float, int]:
    """(US$ gastos gravando cache, tokens gravados), só nas chamadas com preço."""
    total, tokens = 0.0, 0
    for c in chamadas:
        if c["erro_api"]:
            continue
        w5, w1 = c["cache_write_5m"], c["cache_write_1h"]
        preco = achar_preco(c["modelo"], c["input_tokens"] + w5 + w1 + c["cache_read"])
        if preco is not None:
            total += (w5 * preco[1] + w1 * preco[2]) / 1e6
            tokens += w5 + w1
    return total, tokens


def analisar(con: sqlite3.Connection, sessao_id: str, lim: dict) -> dict:
    """Tudo que `tc sessao` mostra de uma sessão, só com números e metadados do banco."""
    s = diagnostico.carregar_sessoes(con, [sessao_id])[0]
    chamadas, tools, eventos = s["chamadas"], s["tools"], s["eventos"]
    prompts = [(r["ts"], r["rotulo"]) for r in con.execute(
        "SELECT ts, rotulo FROM eventos WHERE tipo = 'prompt' AND sessao_id = ? ORDER BY ts", (sessao_id,))]
    compactacoes = [r[0] for r in con.execute(
        "SELECT ts FROM eventos WHERE tipo = 'compactacao' AND sessao_id = ? ORDER BY ts", (sessao_id,))]
    horarios_prompt = [ts for ts, _ in prompts]
    projeto_dos_eventos = con.execute("SELECT MAX(projeto) FROM eventos WHERE sessao_id = ?", (sessao_id,)).fetchone()[0]

    def turno_de(ts: str) -> int:
        return bisect.bisect_right(horarios_prompt, ts) - 1

    # custo por tool, thread a thread (a principal e cada arquivo de subagente)
    por_thread_c: dict = defaultdict(list)
    por_thread_t: dict = defaultdict(list)
    for c in chamadas:
        por_thread_c[c["arquivo"] if c["subagente"] else None].append(c)
    for t in tools:
        por_thread_t[t["arquivo"] if t["subagente"] else None].append(t)
    chamadas_com_tools: dict = {}
    for thread, lista in por_thread_t.items():
        _atribuir_custo(por_thread_c.get(thread, []), lista)
        chamadas_com_tools[thread] = {t["_chamada"] for t in lista if t["_chamada"] >= 0}

    itens = {t["id"]: _item_tool(t, lim) for t in tools}

    # subagentes: um grupo por arquivo, aninhado na chamada Agent/Task da thread principal que o disparou
    grupos: dict = {}
    for thread in por_thread_t.keys() | por_thread_c.keys():
        if thread is None:
            continue
        ts_g = [t["ts"] for t in por_thread_t.get(thread, [])] + [c["ts"] for c in por_thread_c.get(thread, [])]
        custo_g, sem_custo_g = _custo(por_thread_c.get(thread, []))
        grupos[thread] = {
            "tipo": "subagente", "inicio": min(ts_g), "fim": max(ts_g), "custo_usd": custo_g, "sem_custo": sem_custo_g,
            "chamadas": len(por_thread_c.get(thread, [])), "filhos": [itens[t["id"]] for t in por_thread_t.get(thread, [])],
        }
    principais = [t for t in por_thread_t.get(None, [])]
    pais = [t for t in principais if t["tool"] in DELEGACAO]
    livres = list(pais)
    por_item_pai: dict[str, dict] = {}
    for thread, g in sorted(grupos.items(), key=lambda kv: kv[1]["inicio"]):
        candidatos = [t for t in livres if t["ts"] <= g["inicio"]] or [t for t in pais if t["ts"] <= g["inicio"]]
        if candidatos:
            pai = max(candidatos, key=lambda t: t["ts"])
            pai = next(t for t in candidatos if t["ts"] == pai["ts"])     # empate: o primeiro da fila
            if pai in livres:
                livres.remove(pai)
            itens[pai["id"]]["filhos"].append(g)
            por_item_pai[thread] = pai
        g["turno"] = turno_de(por_item_pai[thread]["ts"] if thread in por_item_pai else g["inicio"])

    # turnos (prompt + o que aconteceu até o próximo prompt)
    turnos: dict[int, dict] = {}

    def turno(i: int) -> dict:
        if i not in turnos:
            inicio = prompts[i][0] if i >= 0 else None
            turnos[i] = {"indice": i, "inicio": inicio, "pausa_antes_s": None, "pausa_antes": None, "itens": [],
                         "custo_usd": 0.0, "sem_custo": 0, "chamadas": 0, "tools": 0, "sem_tool_chamadas": 0,
                         "sem_tool_custo_usd": 0.0}
        return turnos[i]

    for i in range(len(prompts)):
        turno(i)
    for thread, lista in por_thread_c.items():
        for n, c in enumerate(lista):
            if thread is None:
                alvo = turno(turno_de(c["ts"]))
            else:
                alvo = turno(grupos[thread]["turno"])
            alvo["chamadas"] += 1
            if c["custo_usd"] is not None:
                alvo["custo_usd"] += c["custo_usd"]
            elif not c["erro_api"]:
                alvo["sem_custo"] += 1
            if thread is None and n not in chamadas_com_tools.get(None, set()) and not c["erro_api"]:
                alvo["sem_tool_chamadas"] += 1
                alvo["sem_tool_custo_usd"] += c["custo_usd"] or 0.0
    for t in principais:
        t_i = turno(turno_de(t["ts"]))
        t_i["itens"].append(itens[t["id"]])
        t_i["tools"] += 1
    for thread, g in grupos.items():
        if thread not in por_item_pai:           # sem Agent/Task que explique de onde veio: fica solto no turno
            turno(g["turno"])["itens"].append(g)
    for i in compactacoes:
        turno(turno_de(i))["itens"].append({"tipo": "compactacao", "ts": i})
    for i in turnos:
        turnos[i]["tools"] += sum(len(g["filhos"]) for g in grupos.values() if g["turno"] == i)

    # pausa entre prompts (do último evento antes do prompt até o prompt)
    for i, (ts, _) in enumerate(prompts):
        k = bisect.bisect_left(eventos, ts) - 1
        t0, t1 = (diagnostico._t(eventos[k]) if k >= 0 else None), diagnostico._t(ts)
        if t0 and t1:
            turnos[i]["pausa_antes_s"] = (t1 - t0).total_seconds()
            turnos[i]["pausa_antes"] = eventos[k]

    problemas: list[dict] = []
    _falhas_repetidas(por_thread_t, itens, lim, problemas)
    _releituras(por_thread_t, tools, compactacoes, itens, lim, problemas)
    _pausas_e_cache(s, lim, compactacoes, horarios_prompt, turnos, problemas)
    problemas.sort(key=lambda p: (SEVERIDADES[p["severidade"]], -(p["custo_usd"] or 0), p["tipo"]))

    for t in turnos.values():   # por horário; no mesmo horário, avisos e pausas vêm antes da tool
        t["itens"].sort(key=lambda it: (it.get("ts") or it.get("inicio") or "", it["tipo"] in ("tool", "subagente")))
    turnos_c = comportamento.montar_turnos(con, [sessao_id])
    categorias = {t.indice: t.categoria for t in turnos_c}
    for i, t in turnos.items():
        t["categoria"] = categorias.get(i)
    todos = [x for x in [e for e in eventos] + [c["ts"] for c in chamadas] + [t["ts"] for t in tools] if x]
    custo_total, sem_custo = _custo(chamadas)
    total_in = sum(c["input_tokens"] + c["cache_write_5m"] + c["cache_write_1h"] + c["cache_read"]
                   for c in chamadas if not c["erro_api"])
    t0, t1 = diagnostico._t(min(todos)) if todos else None, diagnostico._t(max(todos)) if todos else None
    return {
        "sessao": sessao_id, "projeto": s["projeto"] or projeto_dos_eventos, "inicio": min(todos) if todos else None,
        "fim": max(todos) if todos else None, "duracao_s": round((t1 - t0).total_seconds()) if t0 and t1 else 0,
        "custo_usd": custo_total, "chamadas_sem_custo": sem_custo,
        "categoria": comportamento.resumo_da_sessao(turnos_c)["categoria"],
        "prompts": len(prompts), "chamadas": len(chamadas), "tools": len(tools),
        "erros_tool": sum(t["erro"] == 1 for t in tools), "compactacoes": len(compactacoes),
        "cache_taxa": sum(c["cache_read"] for c in chamadas if not c["erro_api"]) / total_in if total_in else None,
        "tools_com_hash_sem_nome": sum(1 for t in tools if t["alvo_hash"] and not t.get("alvo_nome")),
        "turnos": [turnos[i] for i in sorted(turnos)],
        "problemas": problemas,
        "pausa_limite_s": lim["pausa_s"],
    }


def _item_tool(t: dict, lim: dict) -> dict:
    return {
        "tipo": "tool", "id": t["id"], "tool": t["tool"], "ts": t["ts"], "erro": t["erro"],
        "duracao_ms": t["duracao_ms"], "custo_usd": t.get("custo"), "subtipo": t["subtipo"],
        "alvo_nome": t.get("alvo_nome"), "alvo_hash": (t["alvo_hash"] or "")[:8] or None,
        "leitura_ini": t.get("leitura_ini"), "leitura_qtd": t.get("leitura_qtd"), "trecho": _texto_do_trecho(t),
        "espera": t["tool"] in set(lim["tools_que_esperam"]), "marcas": [], "filhos": [],
    }


def _faixa(t: dict) -> tuple[int, float] | None:
    """(primeira linha, última linha) lida por um Read; a última é infinita sem `limit`. None = trecho não gravado."""
    ini = t.get("leitura_ini")
    if ini is None:
        return None
    qtd = t.get("leitura_qtd")
    return ini, (ini + qtd - 1 if qtd is not None else INFINITO)


def _texto_da_faixa(ini: int, fim: float) -> str:
    if ini == 1 and fim >= LINHAS_PADRAO_READ:
        return "arquivo inteiro"
    return f"linhas {ini} em diante" if fim == INFINITO else f"linhas {ini}–{int(fim)}"


def _efetiva(faixa: tuple[int, float]) -> tuple[int, int]:
    """Faixa de um Read sem `limit` vale `LINHAS_PADRAO_READ` linhas a partir do início (é o que o Read realmente lê)."""
    return faixa[0], int(min(faixa[1], faixa[0] + LINHAS_PADRAO_READ - 1))


def _uniao(intervalos: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Junta intervalos de linhas que se tocam ou se sobrepõem, em ordem."""
    saida: list[list[int]] = []
    for ini, fim in sorted(intervalos):
        if saida and ini <= saida[-1][1] + 1:
            saida[-1][1] = max(saida[-1][1], fim)
        else:
            saida.append([ini, fim])
    return [(i, f) for i, f in saida]


def _texto_do_trecho(t: dict) -> str | None:
    """Trecho lido por um Read, para o nó da árvore; None se a tool não lê arquivo ou o trecho não foi gravado."""
    faixa = _faixa(t)
    return _texto_da_faixa(*faixa) if faixa and t["tool"] in LEITURAS_DE_ARQUIVO else None


# ---------- problemas ----------

def _descricao_alvo(t: dict) -> str:
    return t.get("alvo_nome") or (f"#{t['alvo_hash'][:8]}" if t.get("alvo_hash") else "")


def _falhas_repetidas(por_thread_t: dict, itens: dict, lim: dict, problemas: list[dict]) -> None:
    """Mesma tool no mesmo alvo falhando `loop_min` vezes ou mais seguidas (limite e gravidade do diagnóstico)."""
    for lista in por_thread_t.values():
        for corrida in diagnostico.corridas_de_falhas(lista, lim, por_alvo=True):
            chamadas = corrida["chamadas"]
            custo = sum(t.get("custo") or 0.0 for t in chamadas)
            alvo = _descricao_alvo(chamadas[0])
            n = corrida["tamanho"]
            for k, t in enumerate(chamadas, 1):
                itens[t["id"]]["marcas"].append(f"falha repetida {k}/{n}")
            problemas.append({
                "tipo": "falha_repetida", "severidade": "alta" if n >= lim["loop_alta"] else "media",
                "motivo": f"{corrida['tool']}{' em ' + alvo if alvo else ''} falhou {n} vezes seguidas",
                "custo_usd": custo, "ts": chamadas[0]["ts"], "dados": {"tool": corrida["tool"], "vezes": n},
            })


def _releituras(por_thread_t: dict, tools: list[dict], compactacoes: list[str], itens: dict, lim: dict,
                problemas: list[dict]) -> None:
    """Trecho de um arquivo lido de novo (mesmo hash, mesma thread) sem edição nele nem compactação entre as leituras.

    Uma leitura só repete outras se as faixas de linhas (offset/limit) se sobrepõem: ler outro trecho do arquivo não é
    releitura. Conta a união de todas as leituras anteriores ainda válidas, e só vira releitura se a parte repetida for
    `releitura_min_sobreposicao` (25%) ou mais do trecho lido agora (uma sobreposição de 3 linhas num trecho de 80 é
    ruído). Sem `limit`, o Read lê até `LINHAS_PADRAO_READ` linhas. Se alguma leitura anterior não tem o trecho gravado
    (sessão ingerida antes dessa coluna), a releitura é só **possível**. Releitura depois de um Edit/Write com sucesso no arquivo ou
    depois de uma compactação é normal e não conta. Limite: alterações feitas por comandos de shell não são vistas,
    por isso as extensões de `releitura_ignora_extensoes` (imagens) ficam de fora; sem o nome gravado, o arquivo é checado.
    """
    edicoes: dict[str, list[str]] = defaultdict(list)
    for t in tools:
        if t["tool"] in EDICAO and t["alvo_hash"] and t["erro"] != 1:
            edicoes[t["alvo_hash"]].append(t["ts"])
    ignoradas = tuple(e.lower() for e in lim["releitura_ignora_extensoes"])
    achados: dict[tuple[str, bool], list[dict]] = defaultdict(list)     # (hash, certa?) -> leituras repetidas
    for lista in por_thread_t.values():
        lidas: dict[str, list[dict]] = defaultdict(list)
        for t in lista:
            if t["tool"] not in LEITURAS_DE_ARQUIVO or not t["alvo_hash"] or t["erro"] == 1:
                continue
            if ignoradas and (t.get("alvo_nome") or "").lower().endswith(ignoradas):
                continue        # imagem: um script de shell pode ter mudado o arquivo, e o `tc` não vê isso
            h = t["alvo_hash"]
            vigentes = [p for p in lidas[h]
                        if not any(p["ts"] <= e <= t["ts"] for e in edicoes.get(h, ()))
                        and not any(p["ts"] <= k <= t["ts"] for k in compactacoes)]
            certa, possivel, sobrepostos, recente = None, None, [], None
            ft = _faixa(t)
            for p in reversed(vigentes):                                   # a leitura mais recente primeiro
                fp = _faixa(p)
                if fp is None or ft is None:
                    possivel = possivel or p
                    continue
                (pi, pf), (ti, tf) = _efetiva(fp), _efetiva(ft)
                ini, fim = max(pi, ti), min(pf, tf)
                if ini <= fim:
                    sobrepostos.append((ini, fim))
                    recente = recente or p
            if sobrepostos:
                uniao = _uniao(sobrepostos)
                ti, tf = _efetiva(ft)
                fracao = min(1.0, sum(f - i + 1 for i, f in uniao) / (tf - ti + 1))
                if fracao >= lim["releitura_min_sobreposicao"]:
                    certa = (recente, " e ".join(_texto_da_faixa(i, f) for i, f in uniao), fracao)
            if certa:
                p, onde, fracao = certa
                achados[(h, True)].append(t)
                itens[t["id"]]["marcas"].append(
                    f"releitura de {onde} ({fracao:.0%} do trecho lido; já lido às {hora(p['ts'])}, sem edição nem compactação)")
            elif possivel:
                achados[(h, False)].append(t)
                itens[t["id"]]["marcas"].append(
                    f"possível releitura (o trecho lido não foi gravado; já lido às {hora(possivel['ts'])})")
            lidas[h].append(t)
    for (_, certa), chamadas in achados.items():
        alvo = _descricao_alvo(chamadas[0]) or "arquivo"
        n = len(chamadas)
        custo = sum(t.get("custo") or 0.0 for t in chamadas)
        if certa:
            motivo = (f"{alvo}: {n} leitura(s) repetindo linhas já lidas ({lim['releitura_min_sobreposicao']:.0%} ou mais "
                      "do trecho), sem edição nem compactação entre elas")
            severidade = "media" if n + 1 >= lim["leitura_media"] else "baixa"
        else:
            motivo = (f"{alvo}: {n} possível(is) releitura(s); o trecho lido não foi gravado "
                      "(`tc ingest --tudo` preenche, enquanto o log existir)")
            severidade = "baixa"
        problemas.append({"tipo": "releitura", "severidade": severidade, "custo_usd": custo, "ts": chamadas[0]["ts"],
                          "motivo": motivo, "dados": {"releituras": n, "certa": certa}})


def _pausas_e_cache(s: dict, lim: dict, compactacoes: list[str], horarios_prompt: list[str], turnos: dict,
                    problemas: list[dict]) -> None:
    """Pausas longas, com o cache que a chamada seguinte teve de regravar; re-criações fora de pausa; cache baixo."""
    chamadas = s["chamadas"]
    principais = [c for c in chamadas if not c["subagente"] and not c["erro_api"]]
    horarios = [c["ts"] for c in principais]
    consumidas: set[str] = set()
    for inicio, fim, segundos in diagnostico.pausas_longas(s["eventos"], lim):
        k = bisect.bisect_left(horarios, fim)
        proxima = principais[k] if k < len(principais) else None
        gravado = proxima["cache_write_5m"] + proxima["cache_write_1h"] if proxima else None
        impacto = diagnostico.impacto_recriacao(proxima) if proxima else None
        if proxima is not None:
            consumidas.add(proxima["id"])
        regravou = gravado is not None and gravado > lim["recriacao_cache_min_tokens"]
        entre_prompts = fim in horarios_prompt
        info = {"segundos": segundos, "tokens_regravados": gravado, "regravou": regravou, "entre_prompts": entre_prompts}
        # só é problema (⚠) se a chamada seguinte teve de regravar o cache; sem isso a pausa não custou nada
        problema = None
        if regravou:
            problema = {
                "tipo": "pausa_longa", "severidade": "media", "custo_usd": impacto, "ts": fim, "dados": info,
                "motivo": f"pausa de {diagnostico._dur(segundos)} (limite {diagnostico._dur(lim['pausa_s'])}): "
                          f"cache expirado, {gravado:,} tokens regravados",
            }
            problemas.append(problema)
        i = bisect.bisect_right(horarios_prompt, fim) - 1
        if i in turnos:
            if entre_prompts:
                turnos[i]["pausa_info"], turnos[i]["pausa_problema"] = info, problema
            else:
                turnos[i]["itens"].append({"tipo": "pausa", "ts": fim, "segundos": segundos, "info": info,
                                           "problema": problema})
    for r in diagnostico.recriacoes_de_cache(chamadas, lim, compactacoes):
        c = r["chamada"]
        if c["id"] in consumidas or r["apos_compactacao"]:
            continue    # já descrita na pausa, ou esperada (o contexto foi compactado)
        if not (r["apos_pausa"] or r["prefixo_perdido"]):
            continue    # só entrou conteúdo novo grande (uma leitura pesada, por exemplo): o cache anterior foi aproveitado
        problema = {
            "tipo": "recriacao_cache", "severidade": "media", "custo_usd": r["impacto_usd"], "ts": c["ts"],
            "motivo": f"{r['gravado']:,} tokens de cache regravados no meio da sessão"
                      + (f", {diagnostico._dur(r['pausa_s'])} depois da chamada anterior" if r["pausa_s"] is not None else ""),
            "dados": {"tokens_regravados": r["gravado"]},
        }
        problemas.append(problema)
        i = bisect.bisect_right(horarios_prompt, c["ts"]) - 1
        if i in turnos:
            turnos[i]["itens"].append({"tipo": "recriacao", "ts": c["ts"], "problema": problema})
    for rec in diagnostico.sinal_cache_baixo(s, lim):
        custo, tokens = _custo_gravacao_cache(chamadas)
        problemas.append({
            "tipo": "cache_baixo", "severidade": rec["severidade"], "custo_usd": custo, "ts": None,
            "motivo": rec["evidencia"] + f"; {fmt_tokens(tokens)} tokens gravados em cache",
            "dados": rec["dados"],
        })


def sugestoes(dados: dict) -> list[str]:
    """Sugestões curtas, uma por tipo de problema encontrado (na ordem da gravidade)."""
    vistos, saida = set(), []
    for p in dados["problemas"]:
        if p["tipo"] not in vistos:
            vistos.add(p["tipo"])
            saida.append(SUGESTOES[p["tipo"]])
    return saida


# ---------- comandos de shell (só na tela, nunca gravados) ----------

def comandos_da_sessao(con: sqlite3.Connection, sessao_id: str) -> dict[str, str]:
    """{id da tool: comando} lidos agora dos logs originais, em memória. Logs que sumiram ficam de fora."""
    por_arquivo: dict[str, dict[str, str]] = defaultdict(dict)
    for r in con.execute("SELECT id, arquivo FROM tool_calls WHERE sessao_id = ? AND subtipo IS NOT NULL", (sessao_id,)):
        por_arquivo[r["arquivo"]][r["id"].split(":", 1)[-1]] = r["id"]
    saida: dict[str, str] = {}
    for arquivo, ids in por_arquivo.items():
        for tool_use_id, comando in claude_code.comandos_do_log(Path(arquivo), set(ids)).items():
            saida[ids[tool_use_id]] = comando
    return saida


def _uma_linha(comando: str) -> str:
    texto = " ".join(comando.split())
    return texto if len(texto) <= COMANDO_MAX else texto[:COMANDO_MAX - 1] + "…"


# ---------- árvore ----------

def _rotulo_tool(it: dict, comandos: dict[str, str] | None) -> list:
    partes = [(it["tool"], "tool")]
    if comandos is not None and it["id"] in comandos:
        partes.append(("  $ " + _uma_linha(comandos[it["id"]]), "alvo"))
    elif comandos is not None and it["subtipo"] is not None:
        partes.append(("  (comando indisponível: log apagado)", "dim"))
    elif it["subtipo"]:
        partes.append((f"  [{it['subtipo']}]", "dim"))
    elif it["alvo_nome"]:
        partes.append(("  " + it["alvo_nome"], "alvo"))
    elif it["alvo_hash"]:
        partes.append((f"  #{it['alvo_hash']}", "dim"))     # ingerida antes do nome: só o hash curto
    if it["trecho"]:
        partes.append((f" · {it['trecho']}", "dim"))
    partes.append(("  " + ("✓" if it["erro"] == 0 else "✗" if it["erro"] == 1 else "?"),
                   "ok" if it["erro"] == 0 else "erro" if it["erro"] == 1 else "dim"))
    partes.append((f"  {usd(it['custo_usd'])}", "custo"))
    tempo = dur_ms(it["duracao_ms"])
    partes.append((f"  {tempo}" + (" (espera)" if it["espera"] and it["duracao_ms"] is not None else ""), "dim"))
    for marca in it["marcas"]:
        partes.append((f"  ⚠ {marca}", "aviso"))
    return partes


def _filho_tool(pai: No, it: dict, comandos: dict[str, str] | None) -> None:
    no = pai.add(*_rotulo_tool(it, comandos))
    for g in it["filhos"]:
        _filho_subagente(no, g, comandos)


def _filho_subagente(pai: No, g: dict, comandos: dict[str, str] | None) -> None:
    segundos = (diagnostico._t(g["fim"]) - diagnostico._t(g["inicio"])).total_seconds()
    no = pai.add(("subagente", "titulo"), (f"  {len(g['filhos'])} tools · {g['chamadas']} chamadas · ", "dim"),
                 (usd(g["custo_usd"]) + ("*" if g["sem_custo"] else ""), "custo"), (f" · {diagnostico._dur(segundos)}", "dim"))
    for it in g["filhos"]:
        _filho_tool(no, it, comandos)


def montar(dados: dict, comandos: dict[str, str] | None = None) -> Visao:
    """Árvore, cabeçalho e rodapé de uma sessão analisada. `comandos` (opcional) vem de `comandos_da_sessao`."""
    v = Visao()
    taxa = dados["cache_taxa"]
    v.cabecalho = [
        [(f"Sessão {dados['sessao'][:8]}", "titulo"), (f" · {dados['projeto'] or '?'}", "tool")],
        [("duração ", "dim"), (diagnostico._dur(dados["duracao_s"]), "tool"), ("   custo ", "dim"),
         (usd(dados["custo_usd"]) + ("*" if dados["chamadas_sem_custo"] else ""), "custo"), ("   categoria ", "dim"),
         (comportamento.ROTULOS.get(dados["categoria"], "-"), "tool")],
        [(f"{dados['prompts']} prompts · {dados['chamadas']} chamadas · {dados['tools']} tools "
          f"({dados['erros_tool']} com erro) · cache {fmt_pct(taxa)}"
          + (f" · {dados['compactacoes']} compactação(ões)" if dados["compactacoes"] else ""), "dim")],
    ]
    v.raiz = No([("Linha do tempo", "titulo")])
    if not dados["tools"]:
        v.cabecalho.append([("Esta sessão não tem tool calls (só conversa): não há tools para mostrar na árvore.", "aviso")])
    for t in dados["turnos"]:
        if t["indice"] >= 0 and t["pausa_antes_s"] is not None and t["pausa_antes_s"] >= 1:
            _no_pausa(v.raiz, t["pausa_antes_s"], t.get("pausa_info"), t.get("pausa_problema"))
        if t["indice"] >= 0:
            rotulo = [(f"Prompt {t['indice'] + 1}", "prompt"), (f" · {hora(t['inicio'])}", "dim")]
        else:
            rotulo = [("Antes do 1º prompt" if dados["prompts"] else "Sem prompt registrado", "prompt")]
        rotulo += [(f" · {comportamento.ROTULOS.get(t['categoria'], '-')}", "tool") if t["categoria"] else ("", "dim"),
                   (f" · {t['tools']} tools · ", "dim"), (usd(t["custo_usd"]) + ("*" if t["sem_custo"] else ""), "custo")]
        ramo = v.raiz.add(*rotulo)
        for it in t["itens"]:
            _item_do_turno(ramo, it, comandos)
        if t["sem_tool_chamadas"]:
            ramo.add((f"respostas sem tool: {t['sem_tool_chamadas']} chamada(s) · ", "dim"),
                     (usd(t["sem_tool_custo_usd"]), "custo"))
    v.rodape = _rodape(dados)
    return v


def _no_pausa(pai: No, segundos: float, info: dict | None, problema: dict | None) -> None:
    """Nó de pausa. Com ⚠ só se o cache foi regravado; senão a pausa aparece apagada, só com a nota do cache."""
    tempo = diagnostico._dur(segundos)
    if problema:
        pai.add(("⏸ ", "aviso"), (f"pausa de {tempo}", "aviso"),
                (f" ⚠ cache expirado, {info['tokens_regravados']:,} tokens regravados", "aviso"),
                *((f" · {usd(problema['custo_usd'])} a mais que ler do cache", "custo"),) if problema["custo_usd"] else ())
        return
    nota = ""
    if info and info["segundos"] > 0 and info["tokens_regravados"] is not None:
        nota = f" · cache não regravado ({info['tokens_regravados']:,} tokens gravados)"
    pai.add((f"⏸ pausa de {tempo}{nota}", "dim"))


def _item_do_turno(ramo: No, it: dict, comandos: dict[str, str] | None) -> None:
    tipo = it["tipo"]
    if tipo == "tool":
        _filho_tool(ramo, it, comandos)
    elif tipo == "subagente":
        _filho_subagente(ramo, it, comandos)
    elif tipo == "compactacao":
        ramo.add((f"↯ contexto compactado · {hora(it['ts'])}", "dim"))
    elif tipo == "pausa":
        _no_pausa(ramo, it["segundos"], it["info"], it["problema"])
    elif tipo == "recriacao":
        p = it["problema"]
        ramo.add(("⚠ ", "aviso"), (p["motivo"], "aviso"),
                 *((f" · {usd(p['custo_usd'])} a mais que ler do cache", "custo"),) if p["custo_usd"] else ())


def _rodape(dados: dict) -> list:
    linhas: list = []
    problemas = dados["problemas"]
    simbolo = {"alta": "▲", "media": "●", "baixa": "○"}
    if problemas:
        linhas.append([(f"Problemas ({len(problemas)})", "titulo")])
        for p in problemas:
            custo = f" · custo envolvido {usd(p['custo_usd'])}" if p["custo_usd"] is not None else " · sem custo medido"
            linhas.append([(f"  ⚠ {simbolo[p['severidade']]} ", "aviso"), (TITULOS[p["tipo"]], "titulo"),
                           (f": {p['motivo']}", "dim"), (custo, "custo")])
    else:
        linhas.append([("Nenhum problema encontrado nos limites do `tc diagnostico`.", "ok")])
    linhas.append([("Para custar menos", "titulo")])
    dicas = sugestoes(dados)
    for d in dicas or ["Nada a mudar: nenhum problema acima dos limites."]:
        linhas.append([("  → ", "ok"), (d, "tool")])
    notas = ["custo de uma tool = o da chamada do modelo que a pediu (dividido entre as tools pedidas juntas); "
             "o que a tool devolveu entra no custo da chamada seguinte. * = há chamada sem preço.",
             "releitura: conta só linhas repetidas (25% ou mais do trecho lido) e não vê mudanças feitas por comandos de shell; "
             "outros sinais (tool lenta, sessão cara) em `tc diagnostico --sessao " + dados["sessao"][:8] + "`."]
    if dados["tools_com_hash_sem_nome"]:
        notas.append(f"{dados['tools_com_hash_sem_nome']} tool(s) mostram o hash curto (#...) no lugar do nome: foram "
                     "ingeridas antes do nome do arquivo ser gravado; `tc ingest --tudo` preenche enquanto o log existir.")
    linhas += [[(n, "dim")] for n in notas]
    return linhas


# ---------- texto simples (sem rich) ----------

def _desenhar(no: No, prefixo: str = "") -> list[str]:
    linhas = []
    for i, f in enumerate(no.filhos):
        ultimo = i == len(no.filhos) - 1
        linhas.append(prefixo + ("└── " if ultimo else "├── ") + f.texto())
        linhas += _desenhar(f, prefixo + ("    " if ultimo else "│   "))
    return linhas


def em_texto(v: Visao) -> str:
    """Mesma árvore em texto puro, para quando o rich não está instalado."""
    linhas = ["".join(t for t, _ in l) for l in v.cabecalho]
    linhas += ["", v.raiz.texto(), *_desenhar(v.raiz), ""]
    linhas += ["".join(t for t, _ in l) for l in v.rodape]
    return "\n".join(linhas)
