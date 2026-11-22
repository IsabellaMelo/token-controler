"""Adaptador do Claude Code: lê ~/.claude/projects/**/*.jsonl.

Extrai apenas números e metadados (modelo, tokens, nomes de tools, flags de erro,
timestamps). O texto de prompts, respostas e resultados de tools é descartado.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..alvo import alvo_da_tool, nome_da_tool, sal_do_banco, trecho_da_leitura
from ..comandos import TOOLS_SHELL, classificar_comando
from ..custo import custo_registrado_valido, decidir
from ..db import apagar_arquivo, gravar_nome_arquivo, gravar_trecho_leitura, ler_modo
from ..regras_erros import classificar, codigo_de_saida, servidor_mcp_da_tool
from ..rotulos import rotular_prompt

FERRAMENTA = "claude_code"


def pasta_padrao() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    raiz = Path(base).expanduser() if base else Path.home() / ".claude"
    return raiz / "projects"


def _nome_projeto(cwd: str | None, arquivo: Path, apelidos: dict[str, str]) -> str:
    if cwd:
        cwd_norm = cwd.replace("\\", "/").rstrip("/")
        # apelidos: prefixo de caminho -> nome do projeto (o prefixo mais longo vence)
        for prefixo in sorted(apelidos, key=len, reverse=True):
            if cwd_norm.lower().startswith(prefixo.replace("\\", "/").rstrip("/").lower()):
                return apelidos[prefixo]
        return cwd_norm.rsplit("/", 1)[-1] or cwd_norm
    # sem cwd: usa o nome da pasta do projeto dentro de ~/.claude/projects
    return arquivo.parent.name


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _eh_prompt(linha: dict) -> bool:
    """Mensagem digitada pela pessoa (não resultado de tool, não meta, não subagente, não resumo de compactação)."""
    if linha.get("type") != "user" or linha.get("isMeta") or linha.get("isSidechain") or linha.get("isCompactSummary"):
        return False
    conteudo = (linha.get("message") or {}).get("content")
    if isinstance(conteudo, str):
        return not conteudo.startswith("<local-command-stdout>")
    if isinstance(conteudo, list):
        tipos = {b.get("type") for b in conteudo if isinstance(b, dict)}
        return "tool_result" not in tipos and bool(tipos & {"text", "image"})
    return False


DURACAO_MAX_S = 3600  # intervalo maior que 1 h entre o tool_use e o resultado não é uma duração confiável


def _duracao_ms(inicio: str | None, fim: str | None) -> int | None:
    """Milissegundos entre dois timestamps ISO; None se faltar um, for ilegível ou sair de [0, 1 h)."""
    try:
        t0 = datetime.fromisoformat(str(inicio).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(fim).replace("Z", "+00:00"))
        segundos = (t1 - t0).total_seconds()
    except (ValueError, TypeError):
        return None
    return round(segundos * 1000) if 0 <= segundos < DURACAO_MAX_S else None


def _texto_do_prompt(linha: dict):
    """Texto do prompt, só para ser entregue ao rotulador. Não é guardado, impresso nem logado."""
    conteudo = (linha.get("message") or {}).get("content")
    if isinstance(conteudo, list):
        return " ".join(b["text"] for b in conteudo
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return conteudo


TOOLS_SUBAGENTE = frozenset({"Agent", "Task"})
LIMITE_ERRO = 20000  # caracteres da mensagem de erro mantidos em memória até a classificação


def _texto_do_erro(bloco: dict) -> str:
    """Mensagem de erro de um tool_result, só para `regras_erros.classificar`. Nunca é gravada, impressa nem logada."""
    c = bloco.get("content")
    if isinstance(c, str):
        return c[:LIMITE_ERRO]
    if isinstance(c, list):
        return " ".join(b["text"] for b in c
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))[:LIMITE_ERRO]
    return ""


def _negacao(linha: dict) -> str | None:
    """`toolDenialKind` da linha do resultado (lista fixa: user-rejected, permission-rule...); outro formato vira None."""
    v = linha.get("toolDenialKind")
    return v if isinstance(v, str) and 0 < len(v) <= 40 and v.replace("-", "").isalnum() else None


def _agente_do_resultado(linha: dict, n_resultados: int) -> str | None:
    """`agentId` do resultado de uma chamada `Agent` (só um ID, não texto). Só vale se a linha tem um resultado."""
    tur = linha.get("toolUseResult")
    ag = tur.get("agentId") if isinstance(tur, dict) and n_resultados == 1 else None
    return ag if isinstance(ag, str) and 0 < len(ag) <= 64 and ag.replace("-", "").replace("_", "").isalnum() else None


def _agente_do_arquivo(arquivo: Path) -> str | None:
    """`agent-<id>.jsonl` -> `<id>`; qualquer outro arquivo -> None."""
    nome = arquivo.stem
    return nome[len("agent-"):] if "subagents" in arquivo.parts and nome.startswith("agent-") else None


def _subtipo(bloco: dict) -> str | None:
    """Classe do comando se a tool roda shell; None nas demais. O texto do comando não sai daqui."""
    if bloco.get("name") not in TOOLS_SHELL:
        return None
    entrada = bloco.get("input")
    return classificar_comando(entrada.get("command") if isinstance(entrada, dict) else None)


def comandos_do_log(arquivo: Path, ids: set[str]) -> dict[str, str]:
    """Comandos de shell dos `tool_use_id` dados, lidos do log original SÓ EM MEMÓRIA (T19, `tc sessao --comandos`).

    O banco nunca recebe esse texto: quem chama só o mostra na tela. Log que não existe mais ou ilegível devolve {}.
    """
    achados: dict[str, str] = {}
    try:
        with arquivo.open("r", encoding="utf-8", errors="replace") as f:
            for bruta in f:
                if "tool_use" not in bruta or not any(i in bruta for i in ids):
                    continue
                try:
                    linha = json.loads(bruta)
                except json.JSONDecodeError:
                    continue
                msg = linha.get("message") if isinstance(linha, dict) else None
                conteudo = msg.get("content") if isinstance(msg, dict) else None
                for b in conteudo if isinstance(conteudo, list) else []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in ids
                            and b.get("name") in TOOLS_SHELL):
                        continue
                    entrada = b.get("input")
                    comando = entrada.get("command") if isinstance(entrada, dict) else None
                    if isinstance(comando, str):
                        achados[b["id"]] = comando
    except OSError:
        return {}
    return achados


BRANCH_MAX = 40            # tamanho máximo do branch mostrado
BYTES_FINAIS = 256 * 1024  # `branch_do_log` só olha o fim do log


def branch_do_log(arquivo: Path) -> str | None:
    """Branch git da sessão (`gitBranch` da última linha do log que o traz), lido SÓ EM MEMÓRIA (T20, `tc live`).

    É mostrado na tela e nunca gravado no banco. Log que não existe, ilegível ou sem o campo devolve None.
    """
    try:
        with arquivo.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - BYTES_FINAIS))
            final = f.read()
    except OSError:
        return None
    for linha in reversed(final.decode("utf-8", errors="replace").splitlines()):
        if '"gitBranch"' not in linha:
            continue
        try:
            dados = json.loads(linha)
        except json.JSONDecodeError:         # a 1ª linha do trecho pode vir cortada
            continue
        branch = dados.get("gitBranch") if isinstance(dados, dict) else None
        if isinstance(branch, str):
            branch = "".join(c for c in branch if c.isprintable()).strip()
            if branch:
                return branch if len(branch) <= BRANCH_MAX else branch[:BRANCH_MAX - 1] + "…"
    return None


def processar_arquivo(con: sqlite3.Connection, arquivo: Path, apelidos: dict[str, str],
                      gravar_nomes: bool | None = None, gravar_trechos: bool | None = None) -> dict:
    """Lê um log e grava no banco. `gravar_nomes` e `gravar_trechos` (T19): None = chaves do config.json."""
    caminho = str(arquivo)
    if gravar_nomes is None:
        gravar_nomes = gravar_nome_arquivo(con)
    if gravar_trechos is None:
        gravar_trechos = gravar_trecho_leitura(con)
    eh_subagente_arquivo = "subagents" in arquivo.parts
    sal = sal_do_banco(con)

    chamadas: dict[tuple, dict] = {}
    tools: dict[str, dict] = {}
    # tool_use_id -> (is_error, timestamp do resultado, mensagem de erro SÓ EM MEMÓRIA, toolDenialKind, agentId do Agent)
    resultados: dict[str, tuple] = {}
    eventos: list[tuple] = []
    linhas_ruins = 0
    turno = 0                 # T22: sobe a cada prompt da thread principal; subagente recebe o do pai depois (`ligar_sessoes`)
    seq = 0                   # T22: ordem de aparição no arquivo; `ligar_sessoes` refaz a ordem da sessão inteira
    agente_arquivo = _agente_do_arquivo(arquivo)

    with arquivo.open("r", encoding="utf-8", errors="replace") as f:
        for bruta in f:
            bruta = bruta.strip()
            if not bruta:
                continue
            try:
                linha = json.loads(bruta)
            except json.JSONDecodeError:
                linhas_ruins += 1
                continue
            if not isinstance(linha, dict):
                continue

            ts = linha.get("timestamp")
            sessao = linha.get("sessionId")
            if not ts or not sessao:
                continue
            projeto = _nome_projeto(linha.get("cwd"), arquivo, apelidos)
            subagente = int(bool(linha.get("isSidechain")) or eh_subagente_arquivo)
            msg = linha.get("message") or {}
            tipo = linha.get("type")

            if tipo in ("user", "assistant"):
                eventos.append((FERRAMENTA, sessao, projeto, ts, "atividade", caminho, None))
            if tipo == "system" and linha.get("subtype") == "compact_boundary":
                # só o horário: marca onde o contexto foi compactado (o texto do resumo nunca é lido)
                eventos.append((FERRAMENTA, sessao, projeto, ts, "compactacao", caminho, None))
            if _eh_prompt(linha) and not eh_subagente_arquivo:
                turno += 1
                # só o rótulo derivado vai para o banco; o texto do prompt não é guardado em lugar nenhum
                eventos.append((FERRAMENTA, sessao, projeto, ts, "prompt", caminho,
                                rotular_prompt(_texto_do_prompt(linha))))

            if tipo == "user" and isinstance(msg.get("content"), list):
                blocos = [b for b in msg["content"]
                          if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")]
                for b in blocos:
                    falhou = bool(b.get("is_error"))
                    resultados[b["tool_use_id"]] = (
                        falhou, ts, _texto_do_erro(b) if falhou else None, _negacao(linha) if falhou else None,
                        _agente_do_resultado(linha, len(blocos)))

            if tipo != "assistant":
                continue

            erro_api = bool(linha.get("isApiErrorMessage"))
            usage = msg.get("usage") or {}
            cc = usage.get("cache_creation") or {}
            w5 = _int(cc.get("ephemeral_5m_input_tokens"))
            w1 = _int(cc.get("ephemeral_1h_input_tokens"))
            if not cc:  # formato antigo: só o total, assume TTL de 5 min
                w5 = _int(usage.get("cache_creation_input_tokens"))

            msg_id = msg.get("id") or linha.get("uuid")
            chave = (msg_id, linha.get("requestId"))
            registro = {
                "id": f"{FERRAMENTA}:{msg_id}:{linha.get('requestId') or ''}",
                "sessao": sessao,
                "projeto": projeto,
                "ts": ts,
                "modelo": msg.get("model"),
                "input": _int(usage.get("input_tokens")),
                "output": _int(usage.get("output_tokens")),
                "w5": w5,
                "w1": w1,
                "read": _int(usage.get("cache_read_input_tokens")),
                "subagente": subagente,
                "erro_api": int(erro_api),
                "registrado": custo_registrado_valido(linha.get("costUSD")),
                "turno": None if eh_subagente_arquivo else turno,
                "seq": seq,
                "subagente_id": agente_arquivo,
            }
            # O Claude Code grava uma linha por bloco de conteúdo da mesma resposta.
            # Fica a linha com o maior output (a mais completa), mas o primeiro timestamp.
            anterior = chamadas.get(chave)
            if anterior is None:
                seq += 1
                registro["seq"] = seq
            if anterior is None or registro["output"] >= anterior["output"]:
                if anterior is not None:
                    registro["ts"] = min(anterior["ts"], registro["ts"])
                    registro["turno"], registro["seq"] = anterior["turno"], anterior["seq"]  # a posição é a da 1ª linha
                    # empate de output: prefere a cópia que gravou o costUSD (como o ccusage)
                    if registro["output"] == anterior["output"] and registro["registrado"] is None:
                        registro["registrado"] = anterior["registrado"]
                chamadas[chave] = registro

            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    if b["id"] not in tools:
                        seq += 1
                    tools.setdefault(b["id"], {
                        "turno": None if eh_subagente_arquivo else turno, "seq": seq,
                        "chamada_id": registro["id"],          # a chamada ao modelo que pediu a tool
                        "servidor_mcp": servidor_mcp_da_tool(b.get("name")),
                        "sessao": sessao, "projeto": projeto, "ts": ts,
                        "tool": b.get("name") or "?", "subagente": subagente,
                        # só o hash do caminho: o caminho em texto e o resto do input são descartados
                        "alvo": alvo_da_tool(sal, b.get("input")),
                        # T19: só o nome do arquivo, sem pasta; nunca o caminho. Desligável em config.json
                        "nome": nome_da_tool(b.get("input")) if gravar_nomes else None,
                        # T19: Read grava só offset e limit (dois inteiros); nenhum outro argumento. Desligável em config.json
                        "trecho": trecho_da_leitura(b.get("name"), b.get("input")) if gravar_trechos else (None, None),
                        # shell: só a classe do comando (teste, build, git, leitura, outro), nunca o texto
                        "subtipo": _subtipo(b),
                    })

    apagar_arquivo(con, caminho)
    modo = ler_modo(con)
    for r in chamadas.values():
        custo, origem = decidir(modo, r["modelo"], r["input"], r["output"], r["w5"], r["w1"],
                                r["read"], r["registrado"], bool(r["erro_api"]))
        con.execute(
            """INSERT OR IGNORE INTO chamadas
               (id, ferramenta, sessao_id, projeto, ts, modelo, input_tokens, output_tokens,
                cache_write_5m, cache_write_1h, cache_read, custo_usd, subagente, erro_api, arquivo,
                custo_registrado, origem_custo, turno, seq, subagente_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["id"], FERRAMENTA, r["sessao"], r["projeto"], r["ts"], r["modelo"], r["input"],
             r["output"], r["w5"], r["w1"], r["read"], custo, r["subagente"], r["erro_api"], caminho,
             r["registrado"], origem, r["turno"], r["seq"], r["subagente_id"]),
        )
    for tool_id, t in tools.items():
        erro, ts_resultado, mensagem, negacao, agente = resultados.get(tool_id, (None, None, None, None, None))
        # T23: a mensagem só passa por estas duas funções puras; o que sai é um rótulo e um inteiro
        categoria = classificar(mensagem, t["tool"], t["servidor_mcp"], negacao) if erro else None
        saida = codigo_de_saida(mensagem, t["tool"]) if erro else None
        mensagem = None
        con.execute(
            """INSERT OR IGNORE INTO tool_calls
               (id, ferramenta, sessao_id, projeto, ts, tool, erro, subagente, arquivo, duracao_ms, alvo_hash,
                subtipo, alvo_nome, leitura_ini, leitura_qtd, turno, seq, chamada_id, subagente_id, servidor_mcp,
                exit_code, erro_categoria)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{FERRAMENTA}:{tool_id}", FERRAMENTA, t["sessao"], t["projeto"], t["ts"], t["tool"],
             None if erro is None else int(erro), t["subagente"], caminho,
             _duracao_ms(t["ts"], ts_resultado), t["alvo"], t["subtipo"], t["nome"],
             t["trecho"][0], t["trecho"][1], t["turno"], t["seq"], t["chamada_id"],
             agente if t["tool"] in TOOLS_SUBAGENTE and agente else agente_arquivo,
             t["servidor_mcp"], saida, categoria),
        )
    con.executemany(
        "INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo, rotulo) VALUES (?,?,?,?,?,?,?)",
        eventos,
    )

    sessoes = {r["sessao"] for r in chamadas.values()} | {t["sessao"] for t in tools.values()}
    return {"chamadas": len(chamadas), "tools": len(tools), "linhas_ruins": linhas_ruins, "sessoes": sessoes}


def _id_do_tool_use_do_subagente(arquivo: str) -> str | None:
    """`toolUseId` do `agent-<id>.meta.json` ao lado do log do subagente (só um ID); None se não houver."""
    meta = Path(arquivo).with_suffix(".meta.json")
    try:
        v = json.loads(meta.read_text(encoding="utf-8", errors="replace")).get("toolUseId")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and 0 < len(v) <= 80 else None


def ligar_sessoes(con: sqlite3.Connection, sessoes: set[str]) -> None:
    """Liga o que está em arquivos diferentes da mesma sessão (T22). Roda depois de processar os arquivos.

    1. Reserva do `subagente_id` da chamada `Agent`: o `toolUseId` do `.meta.json` do subagente.
    2. O turno das chamadas e tools de um subagente é o da chamada `Agent` que o abriu.
    3. `seq`: refaz a ordem da sessão inteira por horário (os subagentes entram no meio da thread principal).
    """
    for sessao in sessoes:
        arquivos = [r[0] for r in con.execute(
            "SELECT DISTINCT arquivo FROM chamadas WHERE sessao_id = ? AND subagente_id IS NOT NULL", (sessao,))]
        for arq in arquivos:
            agente = Path(arq).stem[len("agent-"):]
            uso = _id_do_tool_use_do_subagente(arq)
            if uso:
                con.execute("UPDATE tool_calls SET subagente_id = ? WHERE id = ? AND subagente_id IS NULL",
                            (agente, f"{FERRAMENTA}:{uso}"))
        for tabela in ("chamadas", "tool_calls"):
            con.execute(f"""UPDATE {tabela} SET turno = (
                                SELECT t.turno FROM tool_calls t
                                WHERE t.sessao_id = {tabela}.sessao_id AND t.subagente_id = {tabela}.subagente_id
                                  AND t.subagente = 0 LIMIT 1)
                            WHERE sessao_id = ? AND subagente = 1 AND subagente_id IS NOT NULL""", (sessao,))
        passos = [(r["ts"], 0, r["seq"] or 0, "chamadas", r["id"]) for r in con.execute(
            "SELECT id, ts, seq FROM chamadas WHERE sessao_id = ?", (sessao,))]
        passos += [(r["ts"], 1, r["seq"] or 0, "tool_calls", r["id"]) for r in con.execute(
            "SELECT id, ts, seq FROM tool_calls WHERE sessao_id = ?", (sessao,))]
        passos.sort(key=lambda x: x[:3])
        for tabela in ("chamadas", "tool_calls"):
            con.executemany(f"UPDATE {tabela} SET seq = ? WHERE id = ?",
                            [(n, p[4]) for n, p in enumerate(passos, 1) if p[3] == tabela])


def ingerir(con: sqlite3.Connection, pasta: Path | None = None, tudo: bool = False,
            apelidos: dict[str, str] | None = None) -> dict:
    """Processa arquivos novos ou alterados. Com tudo=True, reprocessa todos."""
    pasta = pasta or pasta_padrao()
    apelidos = apelidos or {}
    resumo = {"arquivos_lidos": 0, "arquivos_pulados": 0, "chamadas": 0, "tools": 0, "linhas_ruins": 0}
    if not pasta.exists():
        resumo["aviso"] = f"pasta não encontrada: {pasta}"
        return resumo

    conhecidos = {
        r["caminho"]: (r["tamanho"], r["mtime"])
        for r in con.execute("SELECT caminho, tamanho, mtime FROM arquivos WHERE ferramenta = ?", (FERRAMENTA,))
    }
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    gravar_nomes, gravar_trechos = gravar_nome_arquivo(con), gravar_trecho_leitura(con)

    tocadas: set[str] = set()
    for arquivo in sorted(pasta.rglob("*.jsonl")):
        st = arquivo.stat()
        if not tudo and conhecidos.get(str(arquivo)) == (st.st_size, st.st_mtime):
            resumo["arquivos_pulados"] += 1
            continue
        r = processar_arquivo(con, arquivo, apelidos, gravar_nomes, gravar_trechos)
        con.execute(
            """INSERT OR REPLACE INTO arquivos
               (caminho, ferramenta, tamanho, mtime, processado_em, linhas_ruins)
               VALUES (?,?,?,?,?,?)""",
            (str(arquivo), FERRAMENTA, st.st_size, st.st_mtime, agora, r["linhas_ruins"]),
        )
        resumo["arquivos_lidos"] += 1
        for k in ("chamadas", "tools", "linhas_ruins"):
            resumo[k] += r[k]
        tocadas |= r["sessoes"]
    ligar_sessoes(con, tocadas)
    con.commit()
    return resumo
