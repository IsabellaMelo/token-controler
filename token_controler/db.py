"""Banco SQLite local. Guarda só números e metadados, nunca o texto das conversas."""

from __future__ import annotations

import json
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
    origem_custo   TEXT,                  -- registrado, calculado, sem_preco; NULL = erro de API
    turno          INTEGER,               -- T22: 1 turno = 1 prompt + tudo até a resposta; 0 = antes do 1º prompt; NULL = não gravado
    seq            INTEGER,               -- T22: ordem na sessão (compartilhada com tool_calls); NULL = não gravado
    subagente_id   TEXT                   -- T22: agentId do subagente que fez a chamada; NULL = thread principal
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
    subtipo    TEXT,                      -- só para shell: teste, build, git, leitura, outro (nunca o comando)
    alvo_nome  TEXT,                      -- só o NOME do arquivo, sem pasta (T19, em teste; ver `tc limpar-nomes`); NULL = n/a
    leitura_ini INTEGER,                  -- Read: linha inicial lida (1 sem offset); NULL = não é Read ou intervalo não gravado
    leitura_qtd INTEGER,                  -- Read: quantas linhas (limit); NULL com leitura_ini preenchido = até o fim
    turno      INTEGER,                   -- T22: turno da tool (ver chamadas.turno)
    seq        INTEGER,                   -- T22: ordem na sessão (compartilhada com chamadas)
    chamada_id TEXT,                      -- T22: chamadas.id da chamada ao modelo que pediu a tool (por tool_use_id)
    subagente_id TEXT,                    -- T22: na chamada Agent, o subagente que ela abriu; nas tools do subagente, o próprio
    servidor_mcp TEXT,                    -- T22: mcp__google-drive__x -> google-drive; NULL = não é MCP
    exit_code  INTEGER,                   -- T23: código de saída de shell com erro (só o número); NULL = n/a
    erro_categoria TEXT                   -- T23: rótulo da lista fixa de regras_erros.py (nunca a mensagem); NULL = sem erro ou não classificado
);

-- Linha do tempo mínima (prompts e atividade) para contar prompts e tempo ativo
CREATE TABLE IF NOT EXISTS eventos (
    ferramenta TEXT NOT NULL,
    sessao_id  TEXT NOT NULL,
    projeto    TEXT,
    ts         TEXT NOT NULL,
    tipo       TEXT NOT NULL,             -- prompt, atividade, compactacao (só o horário)
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
    """Acrescenta colunas novas em bancos antigos (custo, T08; linhas ilegíveis, T11; tools, T13 e T19)."""
    if "linhas_ruins" not in {r["name"] for r in con.execute("PRAGMA table_info(arquivos)")}:
        # arquivos lidos antes da T11 ficam NULL (não contado), nunca 0: `tc ingest --tudo` conta
        con.execute("ALTER TABLE arquivos ADD COLUMN linhas_ruins INTEGER")
    if "rotulo" not in {r["name"] for r in con.execute("PRAGMA table_info(eventos)")}:
        # NULL até `tc ingest --tudo`; prompts de logs já apagados não têm como ser rotulados depois
        con.execute("ALTER TABLE eventos ADD COLUMN rotulo TEXT")
    colunas_tools = {r["name"] for r in con.execute("PRAGMA table_info(tool_calls)")}
    # T13, T14 e T19 (alvo_nome); NULL até `tc ingest --tudo`
    for coluna, tipo in (("duracao_ms", "INTEGER"), ("alvo_hash", "TEXT"), ("subtipo", "TEXT"),
                         ("alvo_nome", "TEXT"), ("leitura_ini", "INTEGER"), ("leitura_qtd", "INTEGER")):
        if coluna not in colunas_tools:
            con.execute(f"ALTER TABLE tool_calls ADD COLUMN {coluna} {tipo}")
    # T22 e T23: NULL até `tc ingest --tudo`
    for coluna, tipo in (("turno", "INTEGER"), ("seq", "INTEGER"), ("chamada_id", "TEXT"), ("subagente_id", "TEXT"),
                         ("servidor_mcp", "TEXT"), ("exit_code", "INTEGER"), ("erro_categoria", "TEXT")):
        if coluna not in colunas_tools:
            con.execute(f"ALTER TABLE tool_calls ADD COLUMN {coluna} {tipo}")
    colunas = {r["name"] for r in con.execute("PRAGMA table_info(chamadas)")}
    for coluna, tipo in (("turno", "INTEGER"), ("seq", "INTEGER"), ("subagente_id", "TEXT")):
        if coluna not in colunas:
            con.execute(f"ALTER TABLE chamadas ADD COLUMN {coluna} {tipo}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_chamadas_sessao_seq ON chamadas(sessao_id, seq)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_tools_sessao_seq ON tool_calls(sessao_id, seq)")
    colunas = {r["name"] for r in con.execute("PRAGMA table_info(chamadas)")}
    if "custo_registrado" not in colunas:
        con.execute("ALTER TABLE chamadas ADD COLUMN custo_registrado REAL")
    if "origem_custo" not in colunas:
        con.execute("ALTER TABLE chamadas ADD COLUMN origem_custo TEXT")
        # linhas antigas não guardaram o costUSD: ficam com o custo calculado, como antes.
        # `tc ingest --tudo` relê os logs que ainda existirem e captura o registrado.
        recalcular(con, ler_modo(con))
        con.commit()


# Erro "de verdade" de uma tool (T23): falhou e não foi só o usuário negando a permissão. A recusa fica fora da taxa de
# erro e dos loops. Uso em SQL: `SUM(CASE WHEN {ERRO_REAL} THEN 1 ELSE 0 END)`. Linhas ainda sem categoria contam como erro.
ERRO_REAL = "(COALESCE(erro, 0) = 1 AND COALESCE(erro_categoria, '') <> 'recusado_usuario')"


def eh_erro_real(erro, categoria) -> bool:
    """Mesma regra de `ERRO_REAL`, para linhas já lidas: `erro` 1 e categoria diferente de `recusado_usuario`."""
    return erro == 1 and categoria != "recusado_usuario"


def ler_modo(con: sqlite3.Connection) -> str:
    """Modo de custo gravado no banco (auto, calculate ou display)."""
    linha = con.execute("SELECT valor FROM config WHERE chave = 'modo_custo'").fetchone()
    return linha["valor"] if linha and linha["valor"] in MODOS else MODO_PADRAO


def gravar_modo(con: sqlite3.Connection, modo: str) -> None:
    if modo not in MODOS:
        raise ValueError(f"modo de custo inválido: {modo}")
    con.execute("INSERT OR REPLACE INTO config (chave, valor) VALUES ('modo_custo', ?)", (modo,))


def arquivo_do_banco(con: sqlite3.Connection) -> Path | None:
    """Arquivo do banco aberto em `con`; None para banco em memória."""
    for r in con.execute("PRAGMA database_list"):
        if r["name"] == "main" and r["file"]:
            return Path(r["file"])
    return None


def _chave_ligada(con: sqlite3.Connection, chave: str) -> bool:
    """Chave booleana do config.json ao lado do banco. Padrão: true; ausente, ilegível ou não booleana vale true."""
    arquivo = arquivo_do_banco(con)
    if arquivo is None:
        return True
    try:
        valor = json.loads((arquivo.parent / "config.json").read_text(encoding="utf-8-sig")).get(chave, True)
    except (OSError, ValueError, AttributeError):
        return True
    return valor if isinstance(valor, bool) else True


def gravar_nome_arquivo(con: sqlite3.Connection) -> bool:
    """Chave `gravar_nome_arquivo` (T19, nome do arquivo sem pasta)."""
    return _chave_ligada(con, "gravar_nome_arquivo")


def gravar_trecho_leitura(con: sqlite3.Connection) -> bool:
    """Chave `gravar_trecho_leitura` (T19, linha inicial e quantidade de linhas de um Read)."""
    return _chave_ligada(con, "gravar_trecho_leitura")


def limpar_nomes(con: sqlite3.Connection) -> int:
    """Apaga os nomes de arquivo gravados (T19). Devolve quantas linhas tinham nome."""
    n = con.execute("UPDATE tool_calls SET alvo_nome = NULL WHERE alvo_nome IS NOT NULL").rowcount
    con.commit()
    return n


def limpar_trechos(con: sqlite3.Connection) -> int:
    """Apaga os trechos de leitura gravados (T19). Devolve quantas linhas tinham trecho."""
    n = con.execute("""UPDATE tool_calls SET leitura_ini = NULL, leitura_qtd = NULL
                       WHERE leitura_ini IS NOT NULL OR leitura_qtd IS NOT NULL""").rowcount
    con.commit()
    return n


def apagar_arquivo(con: sqlite3.Connection, caminho: str) -> None:
    """Remove tudo que veio de um arquivo, para reprocessá-lo do zero."""
    for tabela in ("chamadas", "tool_calls", "eventos"):
        con.execute(f"DELETE FROM {tabela} WHERE arquivo = ?", (caminho,))
