"""Banco SQLite local. Guarda só números e metadados, nunca o texto das conversas."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .custo import MODO_PADRAO, MODOS, recalcular

SCHEMA = """
CREATE TABLE IF NOT EXISTS arquivos (
    caminho       TEXT PRIMARY KEY,
    ferramenta    TEXT NOT NULL,
    tamanho       INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    processado_em TEXT NOT NULL,
    linhas_ruins  INTEGER                 -- linhas ilegíveis no arquivo; NULL = ainda não contado
);

-- Uma linha por requisição ao modelo
CREATE TABLE IF NOT EXISTS chamadas (
    id             TEXT PRIMARY KEY,      -- ferramenta:message_id:request_id
    ferramenta     TEXT NOT NULL,         -- claude_code, copilot, api...
    sessao_id      TEXT NOT NULL,
    projeto        TEXT,
    ts             TEXT NOT NULL,         -- ISO 8601 UTC
    modelo         TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,
    cache_read     INTEGER NOT NULL DEFAULT 0,
    custo_usd      REAL,                  -- custo efetivo no modo ativo; NULL = sem custo (ver origem_custo)
    subagente      INTEGER NOT NULL DEFAULT 0,
    erro_api       INTEGER NOT NULL DEFAULT 0,
    arquivo        TEXT NOT NULL,
    custo_registrado REAL,                -- custo que o log trouxe (costUSD); NULL = o log não trouxe
    origem_custo   TEXT                   -- registrado, calculado, sem_preco; NULL = erro de API
);

-- Configurações do próprio tc (por exemplo, o modo de custo)
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

-- Uma linha por uso de tool
CREATE TABLE IF NOT EXISTS tool_calls (
    id         TEXT PRIMARY KEY,          -- ferramenta:tool_use_id
    ferramenta TEXT NOT NULL,
    sessao_id  TEXT NOT NULL,
    projeto    TEXT,
    ts         TEXT NOT NULL,
    tool       TEXT NOT NULL,
    erro       INTEGER,                   -- 1 erro, 0 ok, NULL sem resultado
    subagente  INTEGER NOT NULL DEFAULT 0,
    arquivo    TEXT NOT NULL,
    duracao_ms INTEGER,                   -- do tool_use ao tool_result; NULL = sem resultado ou intervalo inválido
    alvo_hash  TEXT,                      -- hash do caminho do arquivo lido/editado (nunca o caminho); NULL = n/a
    subtipo    TEXT                       -- só para shell: teste, build, git, leitura, outro (nunca o comando)
);

-- Linha do tempo mínima (prompts e atividade) para contar prompts e tempo ativo
CREATE TABLE IF NOT EXISTS eventos (
    ferramenta TEXT NOT NULL,
    sessao_id  TEXT NOT NULL,
    projeto    TEXT,
    ts         TEXT NOT NULL,
    tipo       TEXT NOT NULL,             -- prompt, atividade
    arquivo    TEXT NOT NULL,
    rotulo     TEXT                       -- só em prompts: 'debug' ou NULL (derivado do texto; o texto nunca é gravado)
);

CREATE INDEX IF NOT EXISTS ix_chamadas_ts      ON chamadas(ts);
CREATE INDEX IF NOT EXISTS ix_chamadas_sessao  ON chamadas(sessao_id);
CREATE INDEX IF NOT EXISTS ix_chamadas_arquivo ON chamadas(arquivo);
CREATE INDEX IF NOT EXISTS ix_tools_arquivo    ON tool_calls(arquivo);
CREATE INDEX IF NOT EXISTS ix_tools_ts         ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS ix_eventos_sessao   ON eventos(sessao_id, ts);
CREATE INDEX IF NOT EXISTS ix_eventos_arquivo  ON eventos(arquivo);
"""


def caminho_padrao() -> Path:
    env = os.environ.get("TC_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".token-controler" / "tracker.db"


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    caminho = caminho or caminho_padrao()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrar(con)
    return con


def _migrar(con: sqlite3.Connection) -> None:
    """Acrescenta colunas novas em bancos antigos (custo, T08; linhas ilegíveis, T11; tools, T13)."""
    if "linhas_ruins" not in {r["name"] for r in con.execute("PRAGMA table_info(arquivos)")}:
        # arquivos lidos antes da T11 ficam NULL (não contado), nunca 0: `tc ingest --tudo` conta
        con.execute("ALTER TABLE arquivos ADD COLUMN linhas_ruins INTEGER")
    if "rotulo" not in {r["name"] for r in con.execute("PRAGMA table_info(eventos)")}:
        # NULL até `tc ingest --tudo`; prompts de logs já apagados não têm como ser rotulados depois
        con.execute("ALTER TABLE eventos ADD COLUMN rotulo TEXT")
    colunas_tools = {r["name"] for r in con.execute("PRAGMA table_info(tool_calls)")}
    # T13 e T14; NULL até `tc ingest --tudo`
    for coluna, tipo in (("duracao_ms", "INTEGER"), ("alvo_hash", "TEXT"), ("subtipo", "TEXT")):
        if coluna not in colunas_tools:
            con.execute(f"ALTER TABLE tool_calls ADD COLUMN {coluna} {tipo}")
    colunas = {r["name"] for r in con.execute("PRAGMA table_info(chamadas)")}
    if "custo_registrado" not in colunas:
        con.execute("ALTER TABLE chamadas ADD COLUMN custo_registrado REAL")
    if "origem_custo" not in colunas:
        con.execute("ALTER TABLE chamadas ADD COLUMN origem_custo TEXT")
        # linhas antigas não guardaram o costUSD: ficam com o custo calculado, como antes.
        # `tc ingest --tudo` relê os logs que ainda existirem e captura o registrado.
        recalcular(con, ler_modo(con))
        con.commit()


def ler_modo(con: sqlite3.Connection) -> str:
    """Modo de custo gravado no banco (auto, calculate ou display)."""
    linha = con.execute("SELECT valor FROM config WHERE chave = 'modo_custo'").fetchone()
    return linha["valor"] if linha and linha["valor"] in MODOS else MODO_PADRAO


def gravar_modo(con: sqlite3.Connection, modo: str) -> None:
    if modo not in MODOS:
        raise ValueError(f"modo de custo inválido: {modo}")
    con.execute("INSERT OR REPLACE INTO config (chave, valor) VALUES ('modo_custo', ?)", (modo,))


def apagar_arquivo(con: sqlite3.Connection, caminho: str) -> None:
    """Remove tudo que veio de um arquivo, para reprocessá-lo do zero."""
    for tabela in ("chamadas", "tool_calls", "eventos"):
        con.execute(f"DELETE FROM {tabela} WHERE arquivo = ?", (caminho,))
