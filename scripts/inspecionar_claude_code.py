"""Mostra a ESTRUTURA dos logs do Claude Code, sem nenhum texto de conversa (T21).

Uso:
    python scripts/inspecionar_claude_code.py [pasta]          # padrão: ~/.claude/projects
    python scripts/inspecionar_claude_code.py > estrutura_claude_code.txt

A saída tem só nomes de campos, tipos, contagens, tamanhos e nomes de tools. Nunca imprime texto de prompt,
resposta, resultado de tool, comando ou caminho. Mensagens de erro são lidas em memória só para medir o
tamanho e contar sinais; o texto não é impresso nem guardado. Nomes de chaves que não parecem identificadores
saem como "<variável>". Os arquivos são abertos só para leitura.

O que o script responde (premissas das decisões B e C de docs/design.md):
  1. `tool_use_id` liga `tool_use` a `tool_result`?
  2. Como o `parentUuid` encadeia as linhas?
  3. Como as linhas de `subagents/` se ligam à chamada `Task`?
  4. Os `tool_result` com `is_error` trazem texto, de que tamanho?
  5. A recusa do usuário se distingue de um erro comum?
  6. Dá para obter duração e exit code?
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\-]{0,49}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def seguro(nome) -> str:
    """Só devolve nomes que parecem identificadores; qualquer outra coisa vira um marcador."""
    return nome if isinstance(nome, str) and _IDENT.match(nome) else "<variável>"


def tipo(v) -> str:
    return "null" if v is None else type(v).__name__


def pct(a: int, b: int) -> str:
    return f"{a}/{b}" + (f" ({100 * a / b:.0f}%)" if b else "")


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def resumo_num(valores: list[float]) -> str:
    if not valores:
        return "sem dados"
    v = sorted(valores)
    return f"n={len(v)} min={v[0]:.0f} mediana={statistics.median(v):.0f} máx={v[-1]:.0f}"


def titulo(t: str) -> None:
    print(f"\n=== {t}")


# Sinais PRELIMINARES de categoria de erro (item C do design). Só contam; a regra de verdade é a T23.
SINAIS = [
    ("recusado_usuario", re.compile(r"doesn't want to proceed|user rejected|rejeitou|tool use was rejected|"
                                    r"interrupted by user", re.I)),
    ("mcp_auth", re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|not authenticated|authenticate|reauth|"
                            r"token (expired|invalid)|needs? authentication", re.I)),
    ("modulo_ausente", re.compile(r"ModuleNotFoundError|No module named|Cannot find module|"
                                  r"ImportError|MODULE_NOT_FOUND", re.I)),
    ("comando_ausente", re.compile(r"command not found|is not recognized as|not recognized as an internal|"
                                   r"não é reconhecido|nao e reconhecido", re.I)),
    ("sintaxe_shell", re.compile(r"syntax error|unexpected token|unexpected EOF|unterminated|"
                                 r"ParserError|erro de sintaxe|unmatched", re.I)),
    ("arquivo_ausente", re.compile(r"No such file|ENOENT|does not exist|cannot find the (file|path)|"
                                   r"File not found|não foi possível localizar|FileNotFoundError", re.I)),
    ("permissao", re.compile(r"permission denied|EACCES|EPERM|access is denied|acesso negado|"
                             r"not permitted|operation not permitted", re.I)),
    ("timeout", re.compile(r"timed? ?out|timeout|ETIMEDOUT|deadline exceeded", re.I)),
    ("teste_falhou", re.compile(r"\bFAILED\b|\d+ failed|AssertionError|tests? failed|Tests:\s+\d+ failed", re.I)),
    ("rede", re.compile(r"ECONNREFUSED|ECONNRESET|ENOTFOUND|getaddrinfo|network is unreachable|"
                        r"connection (refused|reset|error)|could not resolve host|SSL", re.I)),
]
EXIT_RE = re.compile(r"^\s*Exit code[:\s]+(-?\d+)", re.I)


def texto_do_resultado(bloco: dict):
    """Texto do `content` de um tool_result, só em memória. Devolve (texto, forma_do_content)."""
    c = bloco.get("content")
    if isinstance(c, str):
        return c, "str"
    if isinstance(c, list):
        partes = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        tipos = "+".join(sorted({seguro(b.get("type")) for b in c if isinstance(b, dict)})) or "vazio"
        return "\n".join(p for p in partes if isinstance(p, str)), f"list[{tipos}]"
    return "", tipo(c)


def classificar_preliminar(texto: str) -> str:
    for nome, rx in SINAIS:
        if rx.search(texto):
            return nome
    return "outro"


def ler_linhas(arq: Path):
    with open(arq, encoding="utf-8", errors="replace") as f:
        for n, bruta in enumerate(f, 1):
            bruta = bruta.strip()
            if not bruta:
                continue
            try:
                obj = json.loads(bruta)
            except json.JSONDecodeError:
                yield n, None
                continue
            yield n, obj if isinstance(obj, dict) else None


def main() -> None:
    if len(sys.argv) > 1:
        raiz = Path(sys.argv[1])
    else:
        raiz = Path.home() / ".claude" / "projects"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not raiz.is_dir():
        print(f"Pasta não encontrada: {raiz.name}")
        sys.exit(1)
    arquivos = sorted(raiz.rglob("*.jsonl"))
    principais = [a for a in arquivos if "subagents" not in a.parts]
    subs = [a for a in arquivos if "subagents" in a.parts]
    metas = sorted(raiz.rglob("subagents/*.meta.json"))

    titulo("0. Inventário")
    print(f"arquivos .jsonl: {len(arquivos)} (sessões: {len(principais)}, subagentes: {len(subs)}); meta.json: {len(metas)}")

    # ---- varredura única: guarda só estrutura ----
    tipos_linha = Counter()
    chaves_topo = defaultdict(Counter)           # type -> chave -> n
    blocos = defaultdict(Counter)                # (role) -> tipo de bloco -> n
    chaves_bloco = defaultdict(Counter)          # tipo de bloco -> chave -> n
    chaves_input = defaultdict(Counter)          # tool -> chave do input -> n
    linhas_ruins = 0
    uso: dict[str, dict] = {}                    # tool_use_id -> info
    resultados: dict[str, list[dict]] = defaultdict(list)
    por_uuid: dict[str, dict] = {}               # uuid -> info da linha (por arquivo, prefixado)
    linhas_info: list[dict] = []
    tur_chaves = defaultdict(Counter)            # tool -> chave de toolUseResult -> "tipo"
    tur_tipo = Counter()                         # tool -> forma do toolUseResult
    msgs_assist_split = defaultdict(set)         # message.id -> {uuid}
    sub_info: dict[str, dict] = {}               # caminho do arquivo de subagente -> info
    task_resultados = []                         # info de tool_result de Task
    blobs = Counter()                            # blocos dentro de api-request-blob (ignorados)

    for arq in arquivos:
        eh_sub = "subagents" in arq.parts
        sess_dir = arq.parent.parent.name if eh_sub else arq.stem
        agentes_no_arq = set()
        n_linhas = 0
        primeira = None
        for n, obj in ler_linhas(arq):
            if obj is None:
                linhas_ruins += 1
                continue
            n_linhas += 1
            t = seguro(obj.get("type"))
            tipos_linha[t] += 1
            for k in obj:
                chaves_topo[t][seguro(k)] += 1
            uid = obj.get("uuid")
            info = {"arq": str(arq), "n": n, "type": t, "uuid": uid, "parent": obj.get("parentUuid"),
                    "ts": ts(obj.get("timestamp")), "side": bool(obj.get("isSidechain")), "sub": eh_sub,
                    "sess": obj.get("sessionId"), "agent": obj.get("agentId"), "tem_tool_use": False,
                    "tem_tool_result": False, "meta": bool(obj.get("isMeta")),
                    "prompt_id": obj.get("promptId"), "sub_tipo": obj.get("subtype")}
            if primeira is None:
                primeira = info
            if obj.get("agentId"):
                agentes_no_arq.add(obj.get("agentId"))
            if isinstance(uid, str):
                por_uuid[str(arq) + "|" + uid] = info
            linhas_info.append(info)

            msg = obj.get("message")
            if isinstance(msg, dict) and t == "api-request-blob":
                # cópia de mensagens (sem uuid nem timestamp): NÃO conta como tool_use/tool_result
                for b in (msg.get("content") if isinstance(msg.get("content"), list) else []):
                    if isinstance(b, dict):
                        blobs[seguro(b.get("type"))] += 1
            elif isinstance(msg, dict):
                if t == "assistant" and isinstance(msg.get("id"), str) and isinstance(uid, str):
                    msgs_assist_split[str(arq) + "|" + msg["id"]].add(uid)
                cont = msg.get("content")
                role = seguro(msg.get("role"))
                if isinstance(cont, str):
                    blocos[role]["<texto simples>"] += 1
                elif isinstance(cont, list):
                    for b in cont:
                        if not isinstance(b, dict):
                            continue
                        bt = seguro(b.get("type"))
                        blocos[role][bt] += 1
                        for k in b:
                            chaves_bloco[bt][seguro(k)] += 1
                        if bt == "tool_use" and isinstance(b.get("id"), str):
                            info["tem_tool_use"] = True
                            nome = b.get("name") if isinstance(b.get("name"), str) else "?"
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            for k in inp:
                                chaves_input[nome][seguro(k)] += 1
                            uso[b["id"]] = {"nome": nome, "ts": info["ts"], "arq": str(arq), "uuid": uid,
                                            "n": n, "side": info["side"] or eh_sub, "msg": msg.get("id")}
                        if bt == "tool_result" and isinstance(b.get("tool_use_id"), str):
                            info["tem_tool_result"] = True
                            texto, forma = texto_do_resultado(b)
                            r = {"id": b["tool_use_id"], "erro": b.get("is_error"), "len": len(texto),
                                 "forma": forma, "ts": info["ts"], "arq": str(arq), "uuid": uid, "n": n,
                                 "texto": texto, "tur": obj.get("toolUseResult"), "parent": obj.get("parentUuid")}
                            resultados[b["tool_use_id"]].append(r)

            tur = obj.get("toolUseResult")
            if tur is not None and info["tem_tool_result"]:
                # a que tool pertence? descobre depois, pelo tool_use_id; aqui só guardo a forma
                pass

            # linha de resumo de compactação / sistema
            if t == "system":
                chaves_topo["system:" + seguro(obj.get("subtype"))]["(linhas)"] += 1
        if eh_sub:
            sub_info[str(arq)] = {"agentes": agentes_no_arq, "sess_dir": sess_dir, "primeira": primeira,
                                  "n_linhas": n_linhas, "nome": arq.stem}

    # ---- 1. chaves ----
    titulo("1. Estrutura das linhas")
    print(f"linhas com JSON inválido: {linhas_ruins}")
    for t, n in tipos_linha.most_common():
        print(f"\n  type={t}  ({n} linhas)")
        ks = chaves_topo[t]
        print("    chaves: " + ", ".join(f"{k}({c})" for k, c in sorted(ks.items(), key=lambda x: -x[1])))
    sub_t = [k for k in chaves_topo if k.startswith("system:")]
    if sub_t:
        print("\n  subtypes de system: " + ", ".join(f"{k.split(':',1)[1]}({chaves_topo[k]['(linhas)']})" for k in sub_t))
    print("\n  blocos em message.content, por papel:")
    for role, c in blocos.items():
        print(f"    {role}: " + ", ".join(f"{k}({v})" for k, v in c.most_common()))
    print(f"\n  blocos dentro de `api-request-blob` (cópias, IGNORADAS nas contagens de tool_use/tool_result): {dict(blobs)}")
    print("\n  chaves por tipo de bloco:")
    for bt, c in chaves_bloco.items():
        print(f"    {bt}: " + ", ".join(f"{k}({v})" for k, v in sorted(c.items(), key=lambda x: -x[1])))
    print("\n  chaves do `input` por tool (só nomes de argumentos):")
    for nome, c in sorted(chaves_input.items()):
        print(f"    {seguro(nome) if not nome.startswith('mcp__') else nome[:60]}: " + ", ".join(sorted(c)))

    # ---- 2. tool_use_id ----
    titulo("2. `tool_use_id` liga tool_use a tool_result?")
    ids_uso = set(uso)
    ids_res = set(resultados)
    casados = ids_uso & ids_res
    print(f"tool_use: {len(ids_uso)} | tool_result: {len(ids_res)} | casados: {len(casados)}")
    print(f"tool_use sem resultado: {len(ids_uso - ids_res)} | resultado sem tool_use: {len(ids_res - ids_uso)}")
    dup = sum(1 for v in resultados.values() if len(v) > 1)
    print(f"tool_use_id com mais de um resultado: {dup}")
    mesmo_arq = sum(1 for i in casados if uso[i]["arq"] == resultados[i][0]["arq"])
    print(f"par no MESMO arquivo: {pct(mesmo_arq, len(casados))}")
    lat = [(resultados[i][0]["ts"] - uso[i]["ts"]) * 1000 for i in casados
           if resultados[i][0]["ts"] is not None and uso[i]["ts"] is not None]
    print(f"resultado - tool_use em ms (diferença de timestamps das duas linhas): {resumo_num(lat)}")
    dist = Counter()
    for i in casados:
        dist[resultados[i][0]["n"] - uso[i]["n"]] += 1
    print("distância em linhas (resultado - tool_use), mais comuns: "
          + ", ".join(f"{d}:{c}" for d, c in dist.most_common(6)))
    multi = Counter(len([1 for u in uso.values() if u["msg"] == m and u["arq"] == a])
                    for (a, m) in {(u["arq"], u["msg"]) for u in uso.values() if u["msg"]})
    print("tool_use por message.id (chamadas pedidas juntas): "
          + ", ".join(f"{k} tool(s):{v} msg(s)" for k, v in sorted(multi.items())[:6]))
    por_tool = Counter(u["nome"] if not u["nome"].startswith("mcp__") else "mcp__*" for u in uso.values())
    print("tools pedidas: " + ", ".join(f"{seguro(k) if k != 'mcp__*' else k}({v})" for k, v in por_tool.most_common()))
    mcp = Counter()
    mal = 0
    for u in uso.values():
        if u["nome"].startswith("mcp__"):
            partes = u["nome"].split("__")
            if len(partes) >= 3:
                mcp[seguro(partes[1])] += 1
            else:
                mal += 1
    if mcp or mal:
        print(f"nomes mcp__<servidor>__<tool>: servidores {dict(mcp)}; fora do padrão: {mal}")

    # ---- 3. parentUuid ----
    titulo("3. Como o `parentUuid` encadeia as linhas")
    com_uuid = [i for i in linhas_info if isinstance(i["uuid"], str)]
    nulos = sum(1 for i in com_uuid if i["parent"] is None)
    existe = sum(1 for i in com_uuid if i["parent"] and (i["arq"] + "|" + i["parent"]) in por_uuid)
    fora = sum(1 for i in com_uuid if i["parent"] and (i["arq"] + "|" + i["parent"]) not in por_uuid)
    print(f"linhas com uuid: {len(com_uuid)} | parentUuid nulo: {nulos} | pai no mesmo arquivo: {existe} | pai ausente: {fora}")
    pares = Counter()
    for i in com_uuid:
        p = por_uuid.get(i["arq"] + "|" + i["parent"]) if i["parent"] else None
        if p:
            pares[f"{i['type']}{'(tool_result)' if i['tem_tool_result'] else ''} <- {p['type']}{'(tool_use)' if p['tem_tool_use'] else ''}"] += 1
    print("filho <- pai (mais comuns):")
    for k, v in pares.most_common(10):
        print(f"   {v:5d}  {k}")
    ok = tot = 0
    for i, rs in resultados.items():
        if i in uso:
            r = rs[0]
            tot += 1
            # o pai do tool_result é a linha do tool_use (ou outra linha da mesma message.id)?
            pai = por_uuid.get(r["arq"] + "|" + (r["parent"] or ""))
            if pai and (pai["uuid"] == uso[i]["uuid"] or (pai["type"] == "assistant" and pai["tem_tool_use"])):
                ok += 1
    print(f"tool_result cujo pai é a linha do tool_use (ou outra linha assistant com tool_use): {pct(ok, tot)}")
    div = [(m, u) for m, u in msgs_assist_split.items() if len(u) > 1]
    print(f"mensagens do assistente repartidas em várias linhas (mesmo message.id): {len(div)} de {len(msgs_assist_split)}")
    cadeia_prompt = sum(1 for i in com_uuid if i["type"] == "user" and not i["tem_tool_result"] and not i["meta"] and not i["side"]
                        and i["parent"] and (i["arq"] + "|" + i["parent"]) in por_uuid)
    print(f"linhas `user` sem tool_result (prompts) com pai existente (prompt encadeia no turno anterior): {cadeia_prompt}")
    com_pid = sum(1 for i in linhas_info if i["prompt_id"])
    print(f"linhas com `promptId`: {com_pid} de {len(linhas_info)}")
    if com_pid:
        grupos = defaultdict(set)
        for i in linhas_info:
            if i["prompt_id"]:
                grupos[i["prompt_id"]].add(i["type"])
        print(f"  promptIds distintos: {len(grupos)} (candidato a identificador de turno)")

    # ---- 4. subagentes ----
    titulo("4. Como `subagents/` se liga à chamada `Task`")
    print(f"arquivos de subagente: {len(subs)}")
    if metas:
        mk = Counter()
        for m in metas:
            try:
                d = json.loads(m.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            for k, v in d.items():
                mk[f"{seguro(k)}:{tipo(v)}"] += 1
        print("chaves do .meta.json: " + ", ".join(f"{k}({v})" for k, v in mk.items()))
    sess_ids = {a.stem for a in principais}
    for n_sub, (caminho, s) in enumerate(sub_info.items(), 1):
        p = s["primeira"] or {}
        print(f"\n  subagente #{n_sub}: {s['n_linhas']} linhas | agentId(s) nas linhas: {len(s['agentes'])} "
              f"| agentId == nome do arquivo: {all(('agent-' + a) == s['nome'] for a in s['agentes']) if s['agentes'] else 'sem agentId'}"
              f" | pasta da sessão existe como sessão: {s['sess_dir'] in sess_ids}"
              f" | sessionId das linhas == pasta: {p.get('sess') == s['sess_dir']}")
        print(f"     1ª linha: type={p.get('type')} parentUuid={'nulo' if p.get('parent') is None else 'preenchido'} "
              f"isSidechain={p.get('side')}")
    tasks = {i: u for i, u in uso.items() if u["nome"] in ("Task", "Agent")}
    print(f"\nchamadas de subagente (tool_use `Task`/`Agent`): {len(tasks)} | nomes: "
          f"{dict(Counter(u['nome'] for u in tasks.values()))}")
    ligados = 0
    for i, u in tasks.items():
        for r in resultados.get(i, []):
            tur = r["tur"]
            ks = sorted(seguro(k) for k in tur) if isinstance(tur, dict) else [tipo(tur)]
            task_resultados.append(ks)
            ag = tur.get("agentId") if isinstance(tur, dict) else None
            if isinstance(ag, str) and any(("agent-" + ag) == s["nome"] for s in sub_info.values()):
                ligados += 1
    print(f"Task cujo resultado traz `agentId` que bate com um arquivo agent-*.jsonl: {pct(ligados, len(tasks))}")
    for ks in Counter(tuple(k) for k in task_resultados).most_common(3):
        print("   chaves do toolUseResult de Task: " + ", ".join(ks[0]) + f"  (x{ks[1]})")
    # tool_use dentro dos subagentes: pai da cadeia
    dentro = sum(1 for u in uso.values() if u["side"])
    print(f"tool_use feitos DENTRO de subagentes: {dentro} de {len(uso)}")
    sc_sem = sum(1 for i in linhas_info if i["side"] and not i["sub"])
    print(f"linhas isSidechain=true em arquivos de sessão (fora de subagents/): {sc_sem}")
    sc_em_sub = sum(1 for i in linhas_info if i["sub"])
    print(f"linhas em arquivos subagents/: {sc_em_sub} (isSidechain=true nelas: {sum(1 for i in linhas_info if i['sub'] and i['side'])})")
    for n_sub, (caminho, s) in enumerate(list(sub_info.items())[:6], 1):
        # a 1ª linha do subagente aponta para alguma linha da sessão principal?
        p = s["primeira"] or {}
        if p.get("parent"):
            achou = any(k.endswith("|" + p["parent"]) for k in por_uuid)
            print(f"   subagente #{n_sub}: 1ª linha tem parentUuid e ele existe em algum arquivo: {achou}")

    # ---- 5. erros ----
    titulo("5. `tool_result` com erro")
    todos = [(i, r) for i, rs in resultados.items() for r in rs]
    com_flag = [(i, r) for i, r in todos if r["erro"] is True]
    print(f"tool_result: {len(todos)} | is_error=true: {len(com_flag)} | is_error ausente: "
          f"{sum(1 for _, r in todos if r['erro'] is None)} | is_error=false: {sum(1 for _, r in todos if r['erro'] is False)}")
    por_tool_err = Counter(
        (uso[i]["nome"] if i in uso and not uso[i]["nome"].startswith("mcp__") else ("mcp__*" if i in uso else "?"))
        for i, _ in com_flag)
    por_tool_tot = Counter(
        (uso[i]["nome"] if i in uso and not uso[i]["nome"].startswith("mcp__") else ("mcp__*" if i in uso else "?"))
        for i, _ in todos)
    for k, v in por_tool_err.most_common():
        print(f"   {seguro(k) if k != 'mcp__*' else k}: {v} erro(s) em {por_tool_tot[k]} chamada(s)")
    print("forma do `content` nos erros: " + str(dict(Counter(r["forma"] for _, r in com_flag))))
    tams = [r["len"] for _, r in com_flag]
    print(f"tamanho do texto dos erros (caracteres): {resumo_num(tams)}")
    print(f"erros com texto vazio: {sum(1 for t in tams if t == 0)}")
    tam_ok = [r["len"] for _, r in todos if r["erro"] is not True]
    print(f"(para comparar) tamanho dos resultados sem erro: {resumo_num(tam_ok)}")
    print("`toolUseResult` (campo da linha) nos erros: "
          + str(dict(Counter(tipo(r["tur"]) for _, r in com_flag))))
    str_tur = [len(r["tur"]) for _, r in com_flag if isinstance(r["tur"], str)]
    print(f"   quando é texto, tamanho: {resumo_num(str_tur)}")

    # ---- 6. recusa do usuário ----
    titulo("6. A recusa do usuário se distingue?")
    rec_content = Counter()
    rec_tur = Counter()
    for i, r in com_flag:
        c = bool(SINAIS[0][1].search(r["texto"]))
        t_ = bool(isinstance(r["tur"], str) and SINAIS[0][1].search(r["tur"]))
        rec_content[c] += 1
        rec_tur[t_] += 1
    print(f"erros cujo `content` bate com o padrão de recusa: {rec_content.get(True, 0)} de {len(com_flag)}")
    print(f"erros cujo `toolUseResult` (texto) bate com o padrão de recusa: {rec_tur.get(True, 0)} de {len(com_flag)}")
    interrup = 0
    # linhas user de texto curto que sinalizam interrupção (só contagem)
    for arq in principais:
        for _, obj in ler_linhas(arq):
            if not obj or obj.get("type") != "user":
                continue
            m = obj.get("message")
            c = m.get("content") if isinstance(m, dict) else None
            txt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in (c or []) if isinstance(b, dict)
                                                         and isinstance(b.get("text"), str))
            if re.search(r"\[Request interrupted by user", txt):
                interrup += 1
    print(f"linhas de usuário com marcador de interrupção ('[Request interrupted…'): {interrup}")
    flags_extra = Counter()
    for i, r in todos:
        tur = r["tur"]
        if isinstance(tur, dict):
            for k in ("interrupted", "userModified", "rejected", "isImage"):
                if k in tur:
                    flags_extra[f"{k}={tur[k]!r}"] += 1
    print(f"flags no toolUseResult (dict): {dict(flags_extra)}")

    # ---- 6b. negações de permissão ----
    titulo("6b. Campos que distinguem negação/recusa de falha")
    cruz = Counter()
    pd_chaves = Counter()
    for arq in arquivos:
        for _, obj in ler_linhas(arq):
            if not obj or obj.get("type") != "user":
                continue
            m = obj.get("message")
            cont = m.get("content") if isinstance(m, dict) and isinstance(m.get("content"), list) else []
            for b in cont:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                pd = obj.get("permissionDecision")
                if isinstance(pd, dict):
                    pd_chaves[(("erro" if b.get("is_error") else "ok"), ",".join(sorted(seguro(k) for k in pd)))] += 1
                    dec = pd.get("decision")
                    rt = pd.get("reasonType")
                    pd_chaves[("valores", "decision=" + seguro(dec), "reasonType=" + seguro(rt))] += 1
                if b.get("is_error"):
                    texto, _ = texto_do_resultado(b)
                    cruz[("toolDenialKind=" + (seguro(obj.get("toolDenialKind")) if obj.get("toolDenialKind") else "ausente"),
                          "texto bate padrão de recusa" if SINAIS[0][1].search(texto) else "texto não bate")] += 1
    print("campo `toolDenialKind` (na linha user) x padrão de texto, só entre os erros:")
    for k, v in cruz.most_common():
        print(f"   {v:4d}  {k[0]} | {k[1]}")
    print("campo `permissionDecision` (dict): chaves por erro/ok e valores de decision/reasonType:")
    for k, v in sorted(pd_chaves.items(), key=lambda x: -x[1])[:14]:
        print(f"   {v:4d}  {k}")

    # ---- 6c. turno ----
    titulo("6c. Turno: `promptId` e campos vizinhos")
    por_arq = defaultdict(list)
    for arq in principais:
        for _, obj in ler_linhas(arq):
            if obj and obj.get("type") in ("user", "assistant"):
                por_arq[arq].append(obj)
    tot = Counter()
    herda = Counter()
    vals = Counter()
    for arq, ls in por_arq.items():
        ult = None
        prompt_atual = None
        for o in ls:
            if o.get("type") != "user":
                continue
            m = o.get("message")
            cont = m.get("content") if isinstance(m, dict) and isinstance(m.get("content"), list) else []
            tr = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in cont)
            tot["linhas user"] += 1
            if o.get("promptId") != ult:
                tot["trocas de promptId"] += 1
                tot["  troca numa linha tool_result"] += int(tr)
                tot["  troca numa linha isMeta"] += int(bool(o.get("isMeta")))
                tot["  troca numa linha de resumo de compactação"] += int(bool(o.get("isCompactSummary")))
                ult = o.get("promptId")
            if not tr and not o.get("isMeta") and not o.get("isCompactSummary"):
                tot["prompts (user, sem tool_result, sem meta)"] += 1
                prompt_atual = o.get("promptId")
            elif tr and prompt_atual is not None:
                herda["tool_result com o promptId do último prompt" if o.get("promptId") == prompt_atual
                      else "tool_result com promptId diferente"] += 1
            for k in ("turnOrigin", "promptSource", "permissionMode"):
                if k in o:
                    vals[(k, seguro(o[k]) if isinstance(o[k], str) else tipo(o[k]))] += 1
            if isinstance(o.get("turnPosition"), dict):
                vals[("turnPosition(chaves)", ",".join(sorted(seguro(k) for k in o["turnPosition"])))] += 1
    print(dict(tot))
    print(dict(herda))
    print("valores:", dict(vals))
    print("`promptId` em linhas assistant:", sum(1 for ls in por_arq.values() for o in ls
                                                   if o.get("type") == "assistant" and "promptId" in o))
    ok = n_src = 0
    for arq, ls in por_arq.items():
        idx = {o.get("uuid"): o for o in ls}
        for o in ls:
            src = o.get("sourceToolAssistantUUID")
            if not src:
                continue
            n_src += 1
            p = idx.get(src)
            m = p.get("message") if p else None
            ids = {b.get("id") for b in (m.get("content") if isinstance(m, dict) and isinstance(m.get("content"), list) else [])
                   if isinstance(b, dict) and b.get("type") == "tool_use"}
            mm = o.get("message")
            res = {b.get("tool_use_id") for b in (mm.get("content") if isinstance(mm, dict) and isinstance(mm.get("content"), list) else [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"}
            ok += int(bool(ids & res))
    print(f"`sourceToolAssistantUUID` aponta para a linha assistant que tem o tool_use do resultado: {pct(ok, n_src)}")

    # ---- 7. duração e exit code ----
    titulo("7. Duração e exit code")
    por_tool_keys = defaultdict(Counter)
    for i, r in todos:
        nome = uso[i]["nome"] if i in uso else "?"
        nome = "mcp__*" if nome.startswith("mcp__") else seguro(nome)
        tur = r["tur"]
        tur_tipo[(nome, tipo(tur))] += 1
        if isinstance(tur, dict):
            for k, v in tur.items():
                por_tool_keys[nome][f"{seguro(k)}:{tipo(v)}"] += 1
    print("forma do `toolUseResult` por tool:")
    for (n, t_), c in sorted(tur_tipo.items()):
        print(f"   {n}: {t_} x{c}")
    print("\nchaves do `toolUseResult` (dict) por tool:")
    for n, c in sorted(por_tool_keys.items()):
        print(f"   {n}: " + ", ".join(f"{k}({v})" for k, v in sorted(c.items())))
    cand_dur = Counter()
    cand_exit = Counter()
    for n, c in por_tool_keys.items():
        for kt in c:
            k = kt.split(":")[0].lower()
            if "dur" in k or "elapsed" in k or k.endswith("ms") or "time" in k:
                cand_dur[f"{n}.{kt}"] += c[kt]
            if "exit" in k or k in ("code", "returncode", "status", "statuscode"):
                cand_exit[f"{n}.{kt}"] += c[kt]
    print(f"\ncampos candidatos a DURAÇÃO: {dict(cand_dur) or 'nenhum'}")
    print(f"campos candidatos a EXIT CODE no toolUseResult: {dict(cand_exit) or 'nenhum'}")
    # exit code no texto do erro
    exit_txt = Counter()
    for i, r in com_flag:
        nome = uso[i]["nome"] if i in uso else "?"
        m = EXIT_RE.match(r["texto"])
        exit_txt[(seguro(nome) if not nome.startswith("mcp__") else "mcp__*", "Exit code N no início do texto" if m else "sem 'Exit code'")] += 1
    print("\nexit code no texto do erro (padrão 'Exit code N' no início):")
    for (n, k), c in sorted(exit_txt.items()):
        print(f"   {n}: {k} x{c}")
    codigos = Counter()
    for i, r in com_flag:
        m = EXIT_RE.match(r["texto"])
        if m:
            codigos[int(m.group(1))] += 1
    print(f"valores de exit code vistos nos erros: {dict(codigos)}")
    sem_flag_exit = sum(1 for i, r in todos if r["erro"] is not True and EXIT_RE.match(r["texto"] or "")
                        and not EXIT_RE.match(r["texto"]).group(1) == "0")
    print(f"resultados SEM is_error mas com 'Exit code' diferente de 0 no texto: {sem_flag_exit}")
    # duração: usa a diferença de timestamps
    por_nome_lat = defaultdict(list)
    for i in casados:
        a, b = uso[i]["ts"], resultados[i][0]["ts"]
        if a is not None and b is not None:
            n = uso[i]["nome"]
            por_nome_lat["mcp__*" if n.startswith("mcp__") else seguro(n)].append((b - a) * 1000)
    print("\nduração aproximada = timestamp(tool_result) - timestamp(tool_use), em ms, por tool:")
    for n, v in sorted(por_nome_lat.items(), key=lambda x: -len(x[1])):
        print(f"   {n}: {resumo_num(v)}")
    # a linha do tool_use é gravada quando? compara com o timestamp da linha anterior do mesmo message.id
    # ---- 8. sinais preliminares ----
    titulo("8. Sinais preliminares de categoria nos erros (só contagem; regra definitiva = T23)")
    cats = Counter(classificar_preliminar(r["texto"]) for _, r in com_flag)
    for k, v in cats.most_common():
        print(f"   {k}: {v}")
    print(f"   (total de erros: {len(com_flag)}; sem sinal nenhum cai em 'outro')")
    por_cat_tool = defaultdict(Counter)
    for i, r in com_flag:
        nome = uso[i]["nome"] if i in uso else "?"
        por_cat_tool[classificar_preliminar(r["texto"])]["mcp__*" if nome.startswith("mcp__") else seguro(nome)] += 1
    print("\n   categoria <- tools:")
    for k, c in por_cat_tool.items():
        print(f"     {k}: " + ", ".join(f"{t}({n})" for t, n in c.most_common()))
    sem_flag_sinal = Counter()
    for i, r in todos:
        if r["erro"] is not True and r["texto"]:
            c = classificar_preliminar(r["texto"][:600])
            if c != "outro":
                sem_flag_sinal[c] += 1
    print("\n   (sanidade) resultados SEM is_error cujo início bate com algum sinal — mostra o quanto o regex "
          f"dá falso positivo se aplicado sem is_error: {dict(sem_flag_sinal)}")
    print("\nFim. Nenhum texto de conversa foi impresso.")


if __name__ == "__main__":
    main()
