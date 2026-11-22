"""Diagnóstico: sinais de custo e lentidão por sessão, cada um com a evidência em números.

Implementa os sinais do design §6.3 a partir do que já está no banco, sem estimar nada pelo texto
e sem chamar modelo. Inspirado nos limites e nas ideias do AgentTrace (loops, pausas, tools lentas),
mas com regras e limites próprios. TODOS os limites são propostas iniciais, a calibrar na T17, e podem
ser trocados em `~/.token-controler/config.json`, na chave "diagnostico".
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import db
from .precos import achar_preco

# ---------- limites ----------

LIMITES_PADRAO: dict = {
    "cache_taxa_min": 0.80,         # cache baixo: taxa de acerto abaixo disso (design §6.3)
    "cache_taxa_alta": 0.50,        #   ...e severidade alta abaixo disso
    "cache_min_chamadas": 10,       #   ...só em sessões com mais chamadas que isso (design §6.3)
    "recriacao_cache_min_tokens": 10_000,   # re-criação: gravação de cache acima disso numa chamada (design §6.3)
    "recriacoes_media": 3,          #   ...severidade média a partir de tantas re-criações
    "recriacao_perda_pct": 0.50,    #   ...fora de pausa só conta se a leitura de cache caiu abaixo disso do contexto anterior (T19)
    "pausa_s": 300,                 # pausa longa: intervalo entre eventos (cache de 5 min expira) (design §6.3)
    "pausas_media": 3,              #   ...severidade média a partir de tantas pausas
    "loop_min": 3,                  # loop de retry: a mesma tool falha tantas vezes seguidas (design §6.3; AgentTrace: 3)
    "loop_alta": 5,                 #   ...severidade alta a partir de tantas (AgentTrace: 5)
    "leitura_min": 3,               # leitura repetida: mesmo arquivo lido tantas vezes (design §6.3)
    "leitura_media": 5,             #   ...severidade média a partir de tantas
    "tool_lenta_piso_s": 30,        # tool lenta: além do p95 da tool, precisa passar disso (AgentTrace: p95 > 30 s)
    "tool_lenta_amostra_min": 20,   #   ...e a tool precisa ter tantas medições para ter p95
    "tool_lenta_total_media_s": 600,    #   ...severidade média a partir de tanto tempo somado em chamadas lentas
    "tools_que_esperam": ["AskUserQuestion", "ExitPlanMode", "Agent", "Task", "ScheduleWakeup"],  # duração não é da tool
    "sessao_cara_pct": 0.90,        # sessão cara: custo acima desse percentil das sessões do projeto (design §6.3)
    "sessao_cara_min_sessoes": 10,  #   ...exige tantas sessões no projeto (com menos, o p90 é igual ao máximo e nunca é superado)
    "custo_alto_usd": 10.0,         #   ...e severidade alta a partir desse custo absoluto (AgentTrace: US$ 10)
    "contexto_pct": 0.80,           # pressão de contexto: input total da chamada acima dessa fração da janela
    "contexto_pct_alta": 0.95,      #   ...e severidade alta acima dessa
    "releitura_ignora_extensoes": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif", ".ico"],
                                    # `tc sessao`: imagens não entram na releitura (costumam ser regeneradas por script, que o `tc` não vê)
    "live_sessao_ativa_min": 15,    # `tc live`: sessão ativa = com atividade nos últimos tantos minutos
    "live_alerta_janela_min": 60,   #   ...e só alerta de evento (pausa, falha...) dos últimos tantos minutos
    "releitura_min_sobreposicao": 0.25,  # `tc sessao`: releitura só conta se repete esta fração (ou mais) do trecho lido agora
    "janelas_contexto": {},         # {"prefixo-do-modelo": tokens}; vazio = sinal desligado (nenhuma tabela é presumida)
}

SEVERIDADES = {"alta": 0, "media": 1, "baixa": 2}

ACOES = {
    "cache_baixo": "o contexto está sendo regravado em vez de lido do cache: evite mudar o começo do contexto "
                   "no meio da sessão e abra uma sessão nova ao trocar de assunto",
    "recriacao_cache": "evite pausas longas no meio do trabalho ou abra uma sessão nova depois delas",
    "pausa_longa": "pausas acima do TTL do cache fazem o contexto ser regravado na próxima chamada: "
                   "retome em uma sessão nova se a pausa for longa",
    "loop_retry": "a mesma tool falhou várias vezes seguidas: interrompa e mude a abordagem em vez de repetir",
    "leitura_repetida": "o mesmo arquivo foi lido várias vezes: peça para reaproveitar o que já foi lido "
                        "ou passe só o trecho relevante",
    "tool_lenta": "veja se há comandos pesados que podem rodar em segundo plano ou ser restringidos",
    "sessao_cara": "considere dividir o trabalho em sessões menores ou usar um modelo mais barato nas partes simples",
    "pressao_contexto": "o contexto está perto do limite da janela: resuma o que importa e abra uma sessão nova",
}


ROTULOS = {
    "cache_baixo": "cache baixo", "recriacao_cache": "re-criação de cache", "pausa_longa": "pausa longa",
    "loop_retry": "loop de retry", "leitura_repetida": "leitura repetida", "tool_lenta": "tool lenta",
    "sessao_cara": "sessão cara", "pressao_contexto": "pressão de contexto",
}


def caminho_config(caminho_db: Path | None) -> Path:
    """config.json fica ao lado do banco em uso."""
    return (caminho_db or db.caminho_padrao()).parent / "config.json"


def carregar_limites(arquivo: Path | None) -> tuple[dict, list[str]]:
    """Limites padrão sobrepostos pelo config.json. Devolve (limites, avisos); valor inválido é ignorado."""
    limites = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
               for k, v in LIMITES_PADRAO.items()}
    avisos: list[str] = []
    if arquivo is None or not arquivo.exists():
        return limites, avisos
    try:
        brutos = json.loads(arquivo.read_text(encoding="utf-8-sig")).get("diagnostico", {})
        if not isinstance(brutos, dict):
            raise ValueError('"diagnostico" deve ser um objeto')
    except (OSError, ValueError, AttributeError) as erro:
        return limites, [f"config.json ilegível ({erro}); usando os limites padrão"]
    for chave, valor in brutos.items():
        padrao = LIMITES_PADRAO.get(chave)
        if padrao is None:
            avisos.append(f"config.json: limite desconhecido '{chave}' ignorado")
            continue
        if isinstance(padrao, dict):
            ok = isinstance(valor, dict) and all(isinstance(k, str) and _numero(v) and v > 0 for k, v in valor.items())
        elif isinstance(padrao, list):
            ok = isinstance(valor, list) and all(isinstance(v, str) for v in valor)
        else:
            ok = _numero(valor) and valor >= 0
        if ok:
            limites[chave] = valor
        else:
            avisos.append(f"config.json: valor inválido para '{chave}' ignorado")
    return limites, avisos


def _numero(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ---------- utilitários ----------

def _t(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def percentil(valores: list[float], p: float) -> float | None:
    """Percentil pelo método do AgentTrace: ordena e pega o índice ceil(n*p)-1."""
    if not valores:
        return None
    ordenados = sorted(valores)
    return ordenados[max(0, math.ceil(len(ordenados) * p) - 1)]


def _dur(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    h, resto = divmod(s, 3600)
    m, seg = divmod(resto, 60)
    return f"{h}h{m:02d}" if h else f"{m}min{seg:02d}s"


def _rec(sessao: str, projeto, sinal: str, severidade: str, evidencia: str, dados: dict,
         impacto_usd: float | None = None) -> dict:
    return {"sessao": sessao, "projeto": projeto, "sinal": sinal, "severidade": severidade,
            "evidencia": evidencia, "dados": dados, "impacto_usd": impacto_usd, "acao": ACOES[sinal]}


# ---------- sinais (cada um recebe os dados de UMA sessão e devolve 0 ou mais recomendações) ----------

def sinal_cache_baixo(s: dict, lim: dict) -> list[dict]:
    chamadas = [c for c in s["chamadas"] if not c["erro_api"]]
    total = sum(c["input_tokens"] + c["cache_write_5m"] + c["cache_write_1h"] + c["cache_read"] for c in chamadas)
    if len(chamadas) <= lim["cache_min_chamadas"] or not total:
        return []
    taxa = sum(c["cache_read"] for c in chamadas) / total
    if taxa >= lim["cache_taxa_min"]:
        return []
    sev = "alta" if taxa < lim["cache_taxa_alta"] else "media"
    return [_rec(s["id"], s["projeto"], "cache_baixo", sev,
                 f"cache {taxa:.0%} em {len(chamadas)} chamadas (mínimo {lim['cache_taxa_min']:.0%})",
                 {"taxa": taxa, "chamadas": len(chamadas)})]


def impacto_recriacao(c: dict) -> float | None:
    """US$ a mais que uma chamada pagou por regravar o cache em vez de lê-lo (limite superior); None sem preço."""
    gravado = c["cache_write_5m"] + c["cache_write_1h"]
    preco = achar_preco(c["modelo"], c["input_tokens"] + gravado + c["cache_read"])
    if preco is None:
        return None
    # se o cache não tivesse expirado, esses tokens seriam lidos, não gravados
    return (c["cache_write_5m"] * (preco[1] - preco[3]) + c["cache_write_1h"] * (preco[2] - preco[3])) / 1e6


def recriacoes_de_cache(chamadas: list[dict], lim: dict, compactacoes: list[str] = ()) -> list[dict]:
    """Chamadas da thread principal que gravam mais de `recriacao_cache_min_tokens` de cache no meio da sessão.

    Cada item: chamada, tokens gravados, `pausa_s` desde a chamada anterior, `apos_pausa` (pausa maior que o TTL),
    `apos_compactacao` (houve compactação entre as duas chamadas, caso em que regravar é esperado), `prefixo_perdido`
    (a leitura de cache caiu abaixo de `recriacao_perda_pct` do contexto da chamada anterior: o cache não foi
    aproveitado, em vez de só ter entrado conteúdo novo grande) e o impacto em US$.
    A primeira chamada da sessão nunca entra: gravar o cache pela primeira vez é normal.
    """
    principais = [c for c in chamadas if not c["subagente"] and not c["erro_api"]]
    saida = []
    for anterior, c in zip(principais, principais[1:]):
        gravado = c["cache_write_5m"] + c["cache_write_1h"]
        if gravado <= lim["recriacao_cache_min_tokens"]:
            continue
        t0, t1 = _t(anterior["ts"]), _t(c["ts"])
        pausa = (t1 - t0).total_seconds() if t0 and t1 else None
        contexto_anterior = (anterior["input_tokens"] + anterior["cache_write_5m"] + anterior["cache_write_1h"]
                             + anterior["cache_read"])
        saida.append({
            "chamada": c, "gravado": gravado, "pausa_s": pausa,
            "prefixo_perdido": bool(contexto_anterior) and c["cache_read"] < lim["recriacao_perda_pct"] * contexto_anterior,
            "apos_pausa": pausa is not None and pausa > lim["pausa_s"],
            "apos_compactacao": any(anterior["ts"] <= k <= c["ts"] for k in compactacoes),
            "impacto_usd": impacto_recriacao(c),
        })
    return saida


def sinal_recriacao_cache(s: dict, lim: dict) -> list[dict]:
    """Chamada da thread principal que grava muito cache logo depois de uma pausa maior que o TTL."""
    itens = [r for r in recriacoes_de_cache(s["chamadas"], lim) if r["apos_pausa"]]
    n = len(itens)
    tokens = sum(r["gravado"] for r in itens)
    sem_preco = sum(r["impacto_usd"] is None for r in itens)
    impacto = sum(r["impacto_usd"] or 0.0 for r in itens)
    if not n:
        return []
    sev = "media" if n >= lim["recriacoes_media"] else "baixa"
    texto = f"{n} re-criação(ões) de cache após pausa > {_dur(lim['pausa_s'])}, {tokens:,} tokens regravados"
    if sem_preco:
        texto += f" ({sem_preco} sem preço, fora do impacto)"
    return [_rec(s["id"], s["projeto"], "recriacao_cache", sev, texto,
                 {"recriacoes": n, "tokens_regravados": tokens, "sem_preco": sem_preco},
                 impacto if (n - sem_preco) else None)]


def pausas_longas(eventos: list[str], lim: dict) -> list[tuple[str, str, float]]:
    """Intervalos sem atividade maiores que `pausa_s`, como (início, fim, segundos). `eventos` são horários ISO em ordem."""
    saida = []
    for a, b in zip(eventos, eventos[1:]):
        t0, t1 = _t(a), _t(b)
        if t0 and t1 and (t1 - t0).total_seconds() > lim["pausa_s"]:
            saida.append((a, b, (t1 - t0).total_seconds()))
    return saida


def sinal_pausa_longa(s: dict, lim: dict) -> list[dict]:
    pausas = [segundos for _, _, segundos in pausas_longas(s["eventos"], lim)]
    if not pausas:
        return []
    sev = "media" if len(pausas) >= lim["pausas_media"] else "baixa"
    return [_rec(s["id"], s["projeto"], "pausa_longa", sev,
                 f"{len(pausas)} pausa(s) > {_dur(lim['pausa_s'])} (maior: {_dur(max(pausas))}, total: {_dur(sum(pausas))})",
                 {"pausas": len(pausas), "maior_s": max(pausas), "total_s": sum(pausas)})]


def corridas_de_falhas(tools: list[dict], lim: dict, por_alvo: bool = False) -> list[dict]:
    """Corridas de `loop_min` ou mais falhas seguidas da mesma tool, na sequência dada (uma thread só).

    Com `por_alvo`, a corrida também exige o mesmo `alvo_hash` (a tool falhando no mesmo arquivo; sem alvo conta como
    o mesmo). Qualquer chamada que não seja erro corta a corrida. Cada item: tool, alvo, tamanho e as chamadas.
    """
    corridas: list[dict] = []
    atual: list[dict] = []

    def fechar() -> None:
        if len(atual) >= lim["loop_min"]:
            corridas.append({"tool": atual[0]["tool"], "alvo": atual[0].get("alvo_hash"), "tamanho": len(atual),
                             "chamadas": list(atual)})

    for t in tools:
        if t["erro"] != 1:
            fechar()
            atual = []
        elif atual and t["tool"] == atual[0]["tool"] and (not por_alvo or t.get("alvo_hash") == atual[0].get("alvo_hash")):
            atual.append(t)
        else:
            fechar()
            atual = [t]
    fechar()
    return corridas


def sinal_loop_retry(s: dict, lim: dict) -> list[dict]:
    """Corrida de `loop_min` ou mais falhas seguidas da mesma tool, na sequência da thread principal."""
    corridas = corridas_de_falhas([t for t in s["tools"] if not t["subagente"]], lim)
    if not corridas:
        return []
    pior = max(corridas, key=lambda c: c["tamanho"])
    tool, pior = pior["tool"], pior["tamanho"]
    sev = "alta" if pior >= lim["loop_alta"] else "media"
    return [_rec(s["id"], s["projeto"], "loop_retry", sev,
                 f"{tool} falhou {pior} vezes seguidas ({len(corridas)} loop(s) na sessão)",
                 {"tool": tool, "maior_sequencia": pior, "loops": len(corridas)})]


def sinal_leitura_repetida(s: dict, lim: dict) -> list[dict]:
    contagem = Counter(t["alvo_hash"] for t in s["tools"]
                       if t["tool"] == "Read" and t["alvo_hash"] and not t["subagente"])
    repetidos = {alvo: n for alvo, n in contagem.items() if n >= lim["leitura_min"]}
    if not repetidos:
        return []
    maximo = max(repetidos.values())
    sev = "media" if maximo >= lim["leitura_media"] else "baixa"
    return [_rec(s["id"], s["projeto"], "leitura_repetida", sev,
                 f"{len(repetidos)} arquivo(s) lido(s) {lim['leitura_min']}+ vezes (máximo: {maximo}x)",
                 {"arquivos": len(repetidos), "maximo_leituras": maximo,
                  "leituras_extras": sum(n - 1 for n in repetidos.values())})]


def sinal_tool_lenta(s: dict, lim: dict, p95_das_tools: dict[str, float]) -> list[dict]:
    """Chamadas acima do p95 histórico da tool E do piso absoluto. Tools que esperam a pessoa ficam de fora."""
    espera = set(lim["tools_que_esperam"])
    lentas: dict[str, list[float]] = {}
    for t in s["tools"]:
        p95 = p95_das_tools.get(t["tool"])
        if t["tool"] in espera or p95 is None or t["duracao_ms"] is None:
            continue
        seg = t["duracao_ms"] / 1000
        if seg > p95 and seg > lim["tool_lenta_piso_s"]:
            lentas.setdefault(t["tool"], []).append(seg)
    if not lentas:
        return []
    total = sum(sum(v) for v in lentas.values())
    pior = max(lentas, key=lambda k: sum(lentas[k]))
    n = sum(len(v) for v in lentas.values())
    sev = "media" if total >= lim["tool_lenta_total_media_s"] else "baixa"
    return [_rec(s["id"], s["projeto"], "tool_lenta", sev,
                 f"{n} chamada(s) acima do p95 e de {_dur(lim['tool_lenta_piso_s'])}, somando {_dur(total)}; "
                 f"pior: {pior} ({_dur(sum(lentas[pior]))}, p95 da tool: {_dur(p95_das_tools[pior])})",
                 {"chamadas": n, "total_s": total,
                  "por_tool": {k: {"chamadas": len(v), "total_s": sum(v), "p95_s": p95_das_tools[k]}
                               for k, v in lentas.items()}})]


def sinal_sessao_cara(s: dict, lim: dict, p90_do_projeto: dict[str, tuple[float, int]]) -> list[dict]:
    custo = sum(c["custo_usd"] or 0 for c in s["chamadas"])
    p90, n = p90_do_projeto.get(s["projeto"], (None, 0))
    acima_p90 = p90 is not None and custo > p90
    absoluto = custo >= lim["custo_alto_usd"]
    if not (acima_p90 or absoluto):
        return []
    partes = []
    if acima_p90:
        partes.append(f"acima do p{int(lim['sessao_cara_pct'] * 100)} do projeto (US$ {p90:,.2f}, {n} sessões)")
    if absoluto:
        partes.append(f"acima de US$ {lim['custo_alto_usd']:,.2f} por sessão")
    return [_rec(s["id"], s["projeto"], "sessao_cara", "alta" if absoluto else "media",
                 f"custo US$ {custo:,.2f}, " + " e ".join(partes),
                 {"custo_usd": custo, "p90_usd": p90, "sessoes_no_projeto": n},
                 custo - p90 if acima_p90 else None)]


def sinal_pressao_contexto(s: dict, lim: dict) -> list[dict]:
    janelas = lim["janelas_contexto"]
    if not janelas:
        return []
    maior, acima, janela_do_maior = 0.0, 0, 0
    for c in s["chamadas"]:
        modelo = (c["modelo"] or "").lower()
        prefixos = [p for p in janelas if modelo.startswith(p.lower())]
        if not prefixos or c["erro_api"]:
            continue
        janela = janelas[max(prefixos, key=len)]
        usado = (c["input_tokens"] + c["cache_write_5m"] + c["cache_write_1h"] + c["cache_read"]) / janela
        if usado >= lim["contexto_pct"]:
            acima += 1
        if usado > maior:
            maior, janela_do_maior = usado, janela
    if not acima:
        return []
    sev = "alta" if maior >= lim["contexto_pct_alta"] else "media"
    return [_rec(s["id"], s["projeto"], "pressao_contexto", sev,
                 f"{acima} chamada(s) com contexto acima de {lim['contexto_pct']:.0%} da janela "
                 f"(máximo: {maior:.0%} de {janela_do_maior:,} tokens)",
                 {"chamadas": acima, "maximo": maior, "janela": janela_do_maior})]


# ---------- coleta e orquestração ----------

def _em_lotes(itens: list[str], tamanho: int = 500):
    for i in range(0, len(itens), tamanho):
        yield itens[i:i + tamanho]


def carregar_sessoes(con: sqlite3.Connection, ids: list[str]) -> list[dict]:
    sessoes: dict[str, dict] = {i: {"id": i, "projeto": None, "chamadas": [], "tools": [], "eventos": []} for i in ids}
    for lote in _em_lotes(ids):
        marcas = ",".join("?" * len(lote))
        for r in con.execute(f"""SELECT * FROM chamadas WHERE sessao_id IN ({marcas}) ORDER BY ts""", lote):
            s = sessoes[r["sessao_id"]]
            s["projeto"] = s["projeto"] or r["projeto"]
            s["chamadas"].append(dict(r))
        for r in con.execute(f"""SELECT * FROM tool_calls WHERE sessao_id IN ({marcas}) ORDER BY ts, rowid""", lote):
            s = sessoes[r["sessao_id"]]
            s["projeto"] = s["projeto"] or r["projeto"]
            s["tools"].append(dict(r))
        for r in con.execute(f"""SELECT sessao_id, ts FROM eventos
                                 WHERE tipo = 'atividade' AND sessao_id IN ({marcas}) ORDER BY ts""", lote):
            sessoes[r["sessao_id"]]["eventos"].append(r["ts"])
    return list(sessoes.values())


def _p95_das_tools(con: sqlite3.Connection, lim: dict) -> dict[str, float]:
    duracoes: dict[str, list[float]] = {}
    for r in con.execute("SELECT tool, duracao_ms FROM tool_calls WHERE duracao_ms IS NOT NULL"):
        duracoes.setdefault(r["tool"], []).append(r["duracao_ms"] / 1000)
    return {t: percentil(v, 0.95) for t, v in duracoes.items() if len(v) >= lim["tool_lenta_amostra_min"]}


def _p90_dos_projetos(con: sqlite3.Connection, lim: dict) -> dict[str, tuple[float, int]]:
    custos: dict[str, list[float]] = {}
    for r in con.execute("SELECT projeto, SUM(custo_usd) AS custo FROM chamadas GROUP BY projeto, sessao_id"):
        if r["custo"] is not None:
            custos.setdefault(r["projeto"], []).append(r["custo"])
    return {p: (percentil(v, lim["sessao_cara_pct"]), len(v)) for p, v in custos.items()
            if len(v) >= lim["sessao_cara_min_sessoes"]}


def diagnosticar(con: sqlite3.Connection, limites: dict, sessoes: list[str]) -> list[dict]:
    """Recomendações das sessões dadas, da mais para a menos importante.

    Ordem: severidade (alta, média, baixa); depois o impacto em US$ quando há conta real; depois o custo da sessão.
    """
    p95 = _p95_das_tools(con, limites)
    p90 = _p90_dos_projetos(con, limites)
    saida = []
    for s in carregar_sessoes(con, sessoes):
        recs = (sinal_cache_baixo(s, limites) + sinal_recriacao_cache(s, limites) + sinal_pausa_longa(s, limites)
                + sinal_loop_retry(s, limites) + sinal_leitura_repetida(s, limites)
                + sinal_tool_lenta(s, limites, p95) + sinal_sessao_cara(s, limites, p90)
                + sinal_pressao_contexto(s, limites))
        custo = sum(c["custo_usd"] or 0 for c in s["chamadas"])
        for r in recs:
            r["custo_sessao_usd"] = custo
        saida += recs
    saida.sort(key=lambda r: (SEVERIDADES[r["severidade"]], -(r["impacto_usd"] or 0), -r["custo_sessao_usd"],
                              r["sessao"], r["sinal"]))
    return saida


def resolver_sessao(con: sqlite3.Connection, prefixo: str) -> list[str]:
    """IDs de sessão que começam com o prefixo (o `tc sessoes` mostra os 8 primeiros caracteres)."""
    return [r[0] for r in con.execute("SELECT DISTINCT sessao_id FROM chamadas WHERE sessao_id LIKE ? "
                                      "UNION SELECT DISTINCT sessao_id FROM eventos WHERE sessao_id LIKE ?",
                                      (prefixo + "%", prefixo + "%"))]
