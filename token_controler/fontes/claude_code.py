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
    resultados: dict[str, tuple] = {}  # tool_use_id -> (is_error, timestamp do resultado)
    eventos: list[tuple] = []
    linhas_ruins = 0

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
                # só o rótulo derivado vai para o banco; o texto do prompt não é guardado em lugar nenhum
                eventos.append((FERRAMENTA, sessao, projeto, ts, "prompt", caminho,
                                rotular_prompt(_texto_do_prompt(linha))))

            if tipo == "user" and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                        resultados[b["tool_use_id"]] = (bool(b.get("is_error")), ts)

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
            }
            # O Claude Code grava uma linha por bloco de conteúdo da mesma resposta.
            # Fica a linha com o maior output (a mais completa), mas o primeiro timestamp.
            anterior = chamadas.get(chave)
            if anterior is None or registro["output"] >= anterior["output"]:
                if anterior is not None:
                    registro["ts"] = min(anterior["ts"], registro["ts"])
                    # empate de output: prefere a cópia que gravou o costUSD (como o ccusage)
                    if registro["output"] == anterior["output"] and registro["registrado"] is None:
                        registro["registrado"] = anterior["registrado"]
                chamadas[chave] = registro

            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    tools.setdefault(b["id"], {
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
                custo_registrado, origem_custo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["id"], FERRAMENTA, r["sessao"], r["projeto"], r["ts"], r["modelo"], r["input"],
             r["output"], r["w5"], r["w1"], r["read"], custo, r["subagente"], r["erro_api"], caminho,
             r["registrado"], origem),
        )
    for tool_id, t in tools.items():
        erro, ts_resultado = resultados.get(tool_id, (None, None))
        con.execute(
            """INSERT OR IGNORE INTO tool_calls
               (id, ferramenta, sessao_id, projeto, ts, tool, erro, subagente, arquivo, duracao_ms, alvo_hash,
                subtipo, alvo_nome, leitura_ini, leitura_qtd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{FERRAMENTA}:{tool_id}", FERRAMENTA, t["sessao"], t["projeto"], t["ts"], t["tool"],
             None if erro is None else int(erro), t["subagente"], caminho,
             _duracao_ms(t["ts"], ts_resultado), t["alvo"], t["subtipo"], t["nome"],
             t["trecho"][0], t["trecho"][1]),
        )
    con.executemany(
        "INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo, rotulo) VALUES (?,?,?,?,?,?,?)",
        eventos,
    )

    return {"chamadas": len(chamadas), "tools": len(tools), "linhas_ruins": linhas_ruins}


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
    con.commit()
    return resumo
