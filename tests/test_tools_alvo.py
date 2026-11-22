"""T13: duração das tools e hash do arquivo alvo (fixture sintética em tests/fixtures/claude_code_tools)."""

import re
import sqlite3
from pathlib import Path

import pytest

from token_controler import alvo, db
from token_controler.fontes import claude_code

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_tools" / "projects"


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "t.db")
    claude_code.ingerir(c, pasta=FIXTURES)
    yield c
    c.close()


def tool(con, tool_id):
    return con.execute("SELECT * FROM tool_calls WHERE id = ?", (f"claude_code:{tool_id}",)).fetchone()


def test_duracao_e_o_intervalo_entre_o_uso_e_o_resultado(con):
    assert tool(con, "tA1")["duracao_ms"] == 2500
    assert tool(con, "tA2")["duracao_ms"] == 250
    assert tool(con, "tE")["duracao_ms"] == 1000 and tool(con, "tE")["erro"] == 1
    assert tool(con, "tB")["duracao_ms"] == 1000


def test_duracao_invalida_fica_null_e_nunca_zero(con):
    assert tool(con, "tZ")["duracao_ms"] is None          # sem resultado
    assert tool(con, "tZ")["erro"] is None
    assert tool(con, "tN")["duracao_ms"] is None          # resultado antes do uso (relógio inconsistente)
    assert tool(con, "tL")["duracao_ms"] is None          # 5400 s: acima do limite de 1 h


def test_alvo_so_para_tools_que_operam_em_arquivo(con):
    for ok in ("tA1", "tA2", "tE", "tL", "tM"):
        assert re.fullmatch(r"[0-9a-f]{16}", tool(con, ok)["alvo_hash"]), ok
    assert tool(con, "tB")["alvo_hash"] is None           # Bash: comando é texto livre
    assert tool(con, "tZ")["alvo_hash"] is None           # Grep: `path` é pasta de busca
    assert tool(con, "tX")["alvo_hash"] is None           # file_path que não é texto


def test_mesmo_arquivo_com_grafia_diferente_tem_o_mesmo_hash(con):
    assert tool(con, "tA1")["alvo_hash"] == tool(con, "tA2")["alvo_hash"]
    assert tool(con, "tA1")["alvo_hash"] != tool(con, "tE")["alvo_hash"]
    assert len({tool(con, t)["alvo_hash"] for t in ("tA1", "tE", "tL", "tM")}) == 4


def test_nao_grava_caminho_nem_conteudo_das_tools(con, tmp_path):
    con.commit()
    conteudo = (tmp_path / "t.db").read_bytes()
    for texto in (b"SEGREDO", b"segredo", b"proj\\tools", b"proj/tools", b"echo "):
        assert texto not in conteudo


def test_hash_estavel_ao_reprocessar_e_diferente_entre_bancos(con, tmp_path):
    antes = tool(con, "tA1")["alvo_hash"]
    claude_code.ingerir(con, pasta=FIXTURES, tudo=True)
    assert tool(con, "tA1")["alvo_hash"] == antes          # o salt fica gravado no banco

    outro = db.conectar(tmp_path / "outro.db")
    claude_code.ingerir(outro, pasta=FIXTURES)
    assert tool(outro, "tA1")["alvo_hash"] != antes        # salt diferente: hashes não se ligam entre bancos
    outro.close()


def test_normalizacao_do_caminho():
    sal = b"x"
    h = alvo.hash_do_caminho
    assert h(sal, "C:\\A\\b.py") == h(sal, "c:/a/B.PY") == h(sal, " c:/a/b.py/ ")
    assert h(sal, "C:\\A\\b.py") != h(b"y", "C:\\A\\b.py")
    assert alvo.alvo_da_tool(sal, {"command": "ls"}) is None
    assert alvo.alvo_da_tool(sal, "texto") is None
    assert alvo.alvo_da_tool(sal, {"file_path": "   "}) is None


def test_banco_antigo_ganha_as_colunas_com_null(tmp_path):
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.executescript("""
        CREATE TABLE tool_calls (id TEXT PRIMARY KEY, ferramenta TEXT NOT NULL, sessao_id TEXT NOT NULL,
            projeto TEXT, ts TEXT NOT NULL, tool TEXT NOT NULL, erro INTEGER,
            subagente INTEGER NOT NULL DEFAULT 0, arquivo TEXT NOT NULL);
        INSERT INTO tool_calls VALUES ('claude_code:x', 'claude_code', 's', 'p', '2026-11-01T00:00:00Z', 'Read', 0, 0, 'f');
    """)
    antigo.commit()
    antigo.close()
    c = db.conectar(caminho)
    linha = c.execute("SELECT duracao_ms, alvo_hash FROM tool_calls").fetchone()
    assert tuple(linha) == (None, None)
    c.close()
