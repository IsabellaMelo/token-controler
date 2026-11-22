"""Verificador independente dos totais de tokens por sessão.

Lê os .jsonl do Claude Code direto (somente leitura), deduplica por message.id + requestId
e soma os tokens por sessão. Não importa nada de token_controler, para servir de segunda
opinião. Imprime só números e IDs de sessão, nunca texto de conversa.

Uso:
    python scripts/verificar_totais.py [--pasta PASTA] [--db tracker.db] [--ccusage saida.json]

--db       compara com o banco do tc (lido em modo somente leitura)
--ccusage  compara com a saída de `ccusage session --json`
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

CAMPOS = ("input", "output", "cache_write", "cache_read")


def pasta_padrao() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    raiz = Path(base).expanduser() if base else Path.home() / ".claude"
    return raiz / "projects"


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def ler_linhas(arquivo: Path):
    """Gera os registros JSON de um arquivo, pulando linhas ilegíveis."""
    with arquivo.open("r", encoding="utf-8", errors="replace") as f:
        for bruta in f:
            try:
                linha = json.loads(bruta)
            except json.JSONDecodeError:
                continue
            if isinstance(linha, dict):
                yield linha


def tokens_da_linha(usage: dict) -> tuple:
    """(input, output, cache_write, cache_read) de um bloco usage."""
    cw = _int(usage.get("cache_creation_input_tokens"))
    if not cw:
        cc = usage.get("cache_creation") or {}
        cw = _int(cc.get("ephemeral_5m_input_tokens")) + _int(cc.get("ephemeral_1h_input_tokens"))
    return (_int(usage.get("input_tokens")), _int(usage.get("output_tokens")),
            cw, _int(usage.get("cache_read_input_tokens")))


def somar_por_sessao(pasta: Path) -> dict:
    """Deduplica por (message.id, requestId), ficando com a linha de maior output, e soma por sessão."""
    unicas: dict = {}
    for arquivo in sorted(pasta.rglob("*.jsonl")):
        for linha in ler_linhas(arquivo):
            if linha.get("type") != "assistant" or not linha.get("sessionId") or not linha.get("timestamp"):
                continue
            msg = linha.get("message") or {}
            chave = (msg.get("id") or linha.get("uuid"), linha.get("requestId"))
            tok = tokens_da_linha(msg.get("usage") or {})
            anterior = unicas.get(chave)
            if anterior is None or tok[1] >= anterior[1][1]:
                unicas[chave] = (linha["sessionId"], tok)
    totais: dict = {}
    for sessao, tok in unicas.values():
        t = totais.setdefault(sessao, {"chamadas": 0, **{c: 0 for c in CAMPOS}})
        t["chamadas"] += 1
        for nome, valor in zip(CAMPOS, tok):
            t[nome] += valor
    return totais


def ler_db(caminho: Path) -> dict:
    """Totais por sessão do banco do tc, aberto somente para leitura."""
    con = sqlite3.connect(f"file:{caminho.as_posix()}?mode=ro", uri=True)
    try:
        sql = """SELECT sessao_id, COUNT(*), SUM(input_tokens), SUM(output_tokens),
                        SUM(cache_write_5m + cache_write_1h), SUM(cache_read)
                 FROM chamadas GROUP BY sessao_id"""
        return {
            r[0]: {"chamadas": r[1], **dict(zip(CAMPOS, (r[2] or 0, r[3] or 0, r[4] or 0, r[5] or 0)))}
            for r in con.execute(sql)
        }
    finally:
        con.close()


def ler_ccusage(caminho: Path) -> dict:
    """Totais por sessão do JSON do `ccusage session --json`."""
    dados = json.loads(caminho.read_text(encoding="utf-8-sig"))
    # `ccusage claude session` usa "sessions"/"sessionId"; o relatório unificado usa "session"/"period"
    linhas = dados.get("sessions") or dados.get("session") or []
    return {
        (r.get("sessionId") or r.get("period")): {
            "input": _int(r.get("inputTokens")), "output": _int(r.get("outputTokens")),
            "cache_write": _int(r.get("cacheCreationTokens")), "cache_read": _int(r.get("cacheReadTokens")),
        }
        for r in linhas
    }


def comparar(nome: str, base: dict, outro: dict) -> int:
    """Imprime as diferenças (outro - base) por sessão. Devolve quantas sessões divergem."""
    print(f"\n== {nome} vs verificador ==")
    divergentes = 0
    for sessao in sorted(set(base) | set(outro)):
        b, o = base.get(sessao), outro.get(sessao)
        if b is None or o is None:
            lado = "só no verificador" if o is None else f"só em {nome}"
            print(f"{sessao[:8]}  {lado}")
            divergentes += 1
            continue
        dif = {c: o[c] - b[c] for c in CAMPOS}
        if any(dif.values()):
            divergentes += 1
            print(f"{sessao[:8]}  " + "  ".join(f"{c}={d:+,}" for c, d in dif.items()))
    if not divergentes:
        print("todas as sessões batem")
    return divergentes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Soma tokens por sessão direto dos logs, sem usar o tc.")
    ap.add_argument("--pasta", type=Path, default=pasta_padrao())
    ap.add_argument("--db", type=Path)
    ap.add_argument("--ccusage", type=Path)
    args = ap.parse_args(argv)

    if not args.pasta.exists():
        print(f"pasta não encontrada: {args.pasta}", file=sys.stderr)
        return 2
    base = somar_por_sessao(args.pasta)

    print(f"{'sessão':<10}{'chamadas':>9}{'input':>10}{'output':>12}{'cache w':>12}{'cache r':>14}")
    for sessao in sorted(base):
        t = base[sessao]
        print(f"{sessao[:8]:<10}{t['chamadas']:>9,}{t['input']:>10,}{t['output']:>12,}"
              f"{t['cache_write']:>12,}{t['cache_read']:>14,}")

    divergentes = 0
    if args.db:
        divergentes += comparar("tc", base, ler_db(args.db))
    if args.ccusage:
        divergentes += comparar("ccusage", base, ler_ccusage(args.ccusage))
    return 1 if divergentes else 0


if __name__ == "__main__":
    sys.exit(main())
