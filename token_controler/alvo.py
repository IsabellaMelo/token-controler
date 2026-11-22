"""Identifica o arquivo alvo de uma tool sem guardar o caminho (regra 1 do AGENTS.md).

O banco recebe só um hash do caminho normalizado. Serve para contar leituras repetidas e
retrabalho no mesmo arquivo. O hash leva um salt aleatório gravado em `config`: isso evita
tabelas de hashes pré-calculadas e a ligação entre bancos diferentes, mas NÃO torna o valor
irreversível para quem tem o banco e adivinha caminhos.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

# Campos do `input` da tool que trazem o caminho de um arquivo lido ou editado.
# `path` (Grep, Glob) é uma pasta de busca e `command` (Bash) é texto livre: ficam de fora.
CAMPOS_ARQUIVO = ("file_path", "notebook_path")


def sal_do_banco(con: sqlite3.Connection) -> bytes:
    """Salt do banco; criado na primeira vez e depois reaproveitado."""
    linha = con.execute("SELECT valor FROM config WHERE chave = 'sal_alvo'").fetchone()
    if linha:
        return linha["valor"].encode("ascii")
    sal = secrets.token_hex(16)
    con.execute("INSERT INTO config (chave, valor) VALUES ('sal_alvo', ?)", (sal,))
    return sal.encode("ascii")


def normalizar(caminho: str) -> str:
    """Mesmo arquivo, mesma grafia: barras iguais, sem barra final, sem diferença de caixa."""
    return caminho.strip().replace("\\", "/").rstrip("/").casefold()


def hash_do_caminho(sal: bytes, caminho: str) -> str:
    """16 caracteres hexadecimais (64 bits) do HMAC-SHA256 do caminho normalizado."""
    return hmac.new(sal, normalizar(caminho).encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def alvo_da_tool(sal: bytes, entrada) -> str | None:
    """Hash do arquivo alvo no `input` de um tool_use; None se a tool não opera em arquivo."""
    if not isinstance(entrada, dict):
        return None
    for campo in CAMPOS_ARQUIVO:
        valor = entrada.get(campo)
        if isinstance(valor, str) and valor.strip():
            return hash_do_caminho(sal, valor)
    return None
