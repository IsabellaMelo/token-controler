"""Testes do adaptador Claude Code com logs sintéticos (tests/fixtures/claude_code)."""

from pathlib import Path

import pytest

from token_controler import db
from token_controler.cli import main
from token_controler.fontes import claude_code

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code" / "projects"


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "t.db")
    claude_code.ingerir(c, pasta=FIXTURES)
    yield c
    c.close()


def um(con, sql, *p):
    return con.execute(sql, p).fetchone()[0]


def test_deduplica_linhas_da_mesma_resposta(con):
    # msg1 aparece em duas linhas; fica uma só, com o maior output e o primeiro timestamp
    linhas = con.execute("SELECT output_tokens, ts FROM chamadas WHERE id LIKE '%:msg1:%'").fetchall()
    assert len(linhas) == 1
    assert linhas[0]["output_tokens"] == 300
    assert linhas[0]["ts"] == "2026-11-21T12:00:05Z"


def test_contagens_do_projeto(con):
    assert um(con, "SELECT COUNT(*) FROM chamadas WHERE projeto='rpps'") == 4
    assert um(con, "SELECT COUNT(*) FROM tool_calls WHERE projeto='rpps'") == 3
    assert um(con, "SELECT SUM(erro) FROM tool_calls WHERE projeto='rpps'") == 1
    assert um(con, "SELECT COUNT(*) FROM eventos WHERE tipo='prompt' AND projeto='rpps'") == 2
    assert um(con, "SELECT SUM(erro_api) FROM chamadas") == 1
    assert um(con, "SELECT SUM(subagente) FROM chamadas") == 1


def test_custo_confere_com_calculo_manual(con):
    # msg1: 10*4 + 20000*5 + 300*20 = 106040 / 1e6
    # msg2: 50*4 + 1000*5 + 20000*0.2 + 200*20 = 13200 / 1e6
    # msg3 (haiku 5.5): 100*0.1 + 50*0.5 = 35 / 1e6
    esperado = (106040 + 13200 + 35) / 1e6
    assert um(con, "SELECT SUM(custo_usd) FROM chamadas") == pytest.approx(esperado)


def test_modelo_desconhecido_fica_sem_preco(con):
    assert um(con, "SELECT custo_usd FROM chamadas WHERE modelo='gpt-9-desconhecido'") is None


def test_nao_grava_texto_das_conversas(con, tmp_path):
    con.commit()
    conteudo = (tmp_path / "t.db").read_bytes()
    for texto in (b"SEGREDO", b"analise os fundos", b"gera relatorio"):
        assert texto not in conteudo


def test_ingestao_incremental_pula_arquivos_sem_mudanca(con):
    r = claude_code.ingerir(con, pasta=FIXTURES)
    assert r["arquivos_lidos"] == 0
    assert r["arquivos_pulados"] == 3


def test_reprocessar_tudo_nao_duplica(con):
    antes = um(con, "SELECT COUNT(*) FROM chamadas")
    claude_code.ingerir(con, pasta=FIXTURES, tudo=True)
    assert um(con, "SELECT COUNT(*) FROM chamadas") == antes
    assert um(con, "SELECT COUNT(*) FROM eventos WHERE tipo='prompt'") == 3


def test_conta_linhas_ilegiveis(tmp_path):
    c = db.conectar(tmp_path / "x.db")
    r = claude_code.ingerir(c, pasta=FIXTURES)
    assert r["linhas_ruins"] == 1
    c.close()


def test_cli_roda_os_relatorios(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    banco = str(tmp_path / "cli.db")
    main(["--db", banco, "ingest"])
    for cmd in (["resumo"], ["resumo", "--por", "modelo"], ["sessoes"], ["tools"]):
        main(["--db", banco, *cmd])
    saida = capsys.readouterr().out
    assert "rpps" in saida and "Bash" in saida and "$0.12" in saida
