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


def _caminho_da_entrada(entrada) -> str | None:
    """Caminho em texto do arquivo alvo no `input` de um tool_use; None se a tool não opera em arquivo."""
    if not isinstance(entrada, dict):
        return None
    for campo in CAMPOS_ARQUIVO:
        valor = entrada.get(campo)
        if isinstance(valor, str) and valor.strip():
            return valor
    return None


def alvo_da_tool(sal: bytes, entrada) -> str | None:
    """Hash do arquivo alvo no `input` de um tool_use; None se a tool não opera em arquivo."""
    caminho = _caminho_da_entrada(entrada)
    return hash_do_caminho(sal, caminho) if caminho is not None else None


# T19 (terceira exceção da regra 1, aprovada em 22/11/2026): o trecho lido por um `Read`, só como dois números.
LEITURAS = frozenset({"Read", "FileReadTool"})   # tools que leem um arquivo e aceitam offset/limit
LINHA_MAX = 10**9


def _linha(v, minimo: int) -> int | None:
    """Inteiro (não booleano) entre `minimo` e `LINHA_MAX`; qualquer outra coisa vira None."""
    return v if isinstance(v, int) and not isinstance(v, bool) and minimo <= v <= LINHA_MAX else None


def trecho_da_leitura(nome_tool, entrada) -> tuple[int | None, int | None]:
    """(linha inicial, quantidade de linhas) de um `Read`; a linha inicial é 1 quando não há `offset`.

    Sem `limit`, a quantidade é None (lê até o fim). Devolve (None, None), "trecho desconhecido", se a tool não lê
    arquivo, se `offset`/`limit` não são inteiros válidos ou se há outro modo de recorte (`pages`, de PDF), que não
    cabe em linhas. Só esses dois números saem daqui: nenhum outro argumento é lido.
    """
    if nome_tool not in LEITURAS or _caminho_da_entrada(entrada) is None or "pages" in entrada:
        return None, None
    offset, limit = entrada.get("offset"), entrada.get("limit")
    inicio = 1 if offset is None else _linha(offset, 0)
    quantidade = None if limit is None else _linha(limit, 1)
    if inicio is None or (limit is not None and quantidade is None):
        return None, None
    return max(inicio, 1), quantidade


# T19 (exceção da regra 1, aprovada em 22/11/2026): só o NOME do arquivo, nunca a pasta.
NOME_MAX = 80


def nome_do_arquivo(caminho) -> str | None:
    """Só o nome do arquivo, sem nenhuma pasta (sem `/`, `\\` nem unidade `C:`), com tamanho limitado.

    Devolve None se não sobrar nome (caminho vazio, só separadores, valor que não é texto). Quando o nome passa de
    `NOME_MAX`, corta o começo e mantém a extensão. O resultado nunca contém separador de pasta.
    """
    if not isinstance(caminho, str):
        return None
    nome = caminho.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if len(nome) >= 2 and nome[0].isalpha() and nome[1] == ":":      # `C:arquivo.py`, unidade sem barra
        nome = nome[2:]
    nome = "".join(c for c in nome if c.isprintable()).strip()
    if not nome:
        return None
    if len(nome) > NOME_MAX:
        ponto = nome.rfind(".")
        extensao = nome[ponto:] if 0 < ponto and len(nome) - ponto <= 10 else ""
        nome = nome[:NOME_MAX - len(extensao)] + extensao
    return nome


def nome_da_tool(entrada) -> str | None:
    """Nome do arquivo alvo (sem pasta) no `input` de um tool_use; None se a tool não opera em arquivo."""
    return nome_do_arquivo(_caminho_da_entrada(entrada))
