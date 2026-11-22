"""Testes da origem do custo e dos modos auto/calculate/display (fixture sintética com costUSD)."""

import sqlite3
from pathlib import Path

import pytest

from token_controler import custo, db
from token_controler.cli import main
from token_controler.fontes import claude_code

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_custo" / "projects"
CHEIO = (1000 * 4 + 1000 * 20) / 1e6  # opus 5.5: US$ 4 de input e US$ 20 de output por milhão = 0,024


def ingerir_em(tmp_path, modo=None):
    con = db.conectar(tmp_path / "c.db")
    if modo:
        db.gravar_modo(con, modo)
    claude_code.ingerir(con, pasta=FIXTURES)
    return con


def resultado(con):
    """{id curto: (custo_usd, origem_custo)}"""
    return {
        r["id"].split(":")[1]: (r["custo_usd"], r["origem_custo"])
        for r in con.execute("SELECT id, custo_usd, origem_custo FROM chamadas")
    }


def test_auto_usa_o_registrado_e_calcula_o_resto(tmp_path):
    r = resultado(ingerir_em(tmp_path))
    assert r["msgC1"] == (0.5, "registrado")
    assert r["msgC2"] == (0.0, "registrado")            # zero registrado vale, não é "ausente"
    assert r["msgC3"] == (pytest.approx(CHEIO), "calculado")
    assert r["msgC4"] == (None, "sem_preco")            # modelo fora da tabela: nunca chuta valor
    assert r["msgC5"] == (0.3, "registrado")            # empate de output: fica a cópia com costUSD


def test_maior_output_sem_costusd_nao_herda_o_da_linha_menor(tmp_path):
    # msgC6: a linha de output 10 tem costUSD, a de output 20 não; vale a mais completa, calculada
    custo_, origem = resultado(ingerir_em(tmp_path))["msgC6"]
    assert origem == "calculado"
    assert custo_ == pytest.approx((1000 * 4 + 20 * 20) / 1e6)


@pytest.mark.parametrize("msg", ["msgC7", "msgC8", "msgC9", "msgC10"])
def test_costusd_invalido_e_tratado_como_ausente(tmp_path, msg):
    # null, negativo, texto e booleano não são custo registrado
    assert resultado(ingerir_em(tmp_path))[msg] == (pytest.approx(CHEIO), "calculado")


def test_erro_de_api_nao_tem_custo_nem_origem(tmp_path):
    assert resultado(ingerir_em(tmp_path))["msgErro"] == (None, None)


def test_modo_calculate_ignora_o_registrado_mas_o_guarda(tmp_path):
    con = ingerir_em(tmp_path, "calculate")
    r = resultado(con)
    assert r["msgC1"] == (pytest.approx(CHEIO), "calculado")
    assert r["msgC2"] == (pytest.approx(CHEIO), "calculado")
    assert r["msgC5"] == (pytest.approx((1000 * 4 + 50 * 20) / 1e6), "calculado")
    assert r["msgC4"] == (None, "sem_preco")
    assert con.execute("SELECT custo_registrado FROM chamadas WHERE id LIKE '%msgC1%'").fetchone()[0] == 0.5


def test_modo_display_so_usa_o_registrado(tmp_path):
    r = resultado(ingerir_em(tmp_path, "display"))
    assert r["msgC1"] == (0.5, "registrado")
    assert r["msgC2"] == (0.0, "registrado")
    assert r["msgC5"] == (0.3, "registrado")
    for msg in ("msgC3", "msgC4", "msgC6", "msgC7"):
        assert r[msg] == (None, "sem_preco")             # sem custo no log: NULL, não zero


def test_trocar_de_modo_so_com_o_banco_e_voltar_da_o_mesmo_resultado(tmp_path):
    con = ingerir_em(tmp_path)
    original = resultado(con)
    for modo in ("display", "calculate", "auto"):
        assert custo.recalcular(con, modo) == 11
    assert resultado(con) == original  # nenhum log foi relido


def test_cli_modo_fica_gravado_e_recalcula_o_que_ja_estava_no_banco(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    banco = str(tmp_path / "cli.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()

    main(["--db", banco, "ingest", "--modo", "display"])
    saida = capsys.readouterr().out
    assert "Modo de custo: display (11 chamada(s) recalculada(s))" in saida
    assert "0 arquivo(s) lido(s), 1 sem mudança" in saida  # nenhum log foi relido

    con = db.conectar(Path(banco))
    assert db.ler_modo(con) == "display"
    assert resultado(con)["msgC3"] == (None, "sem_preco")
    con.close()

    main(["--db", banco, "ingest"])          # sem --modo: mantém o modo gravado
    assert "Modo de custo" not in capsys.readouterr().out
    con = db.conectar(Path(banco))
    assert db.ler_modo(con) == "display"
    con.close()


def test_cli_modo_invalido_e_recusado(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "x.db"), "ingest", "--modo", "chute"])
    assert "invalid choice" in capsys.readouterr().err


def test_resumo_mostra_modo_e_origem_dos_custos(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    banco = str(tmp_path / "r.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()
    main(["--db", banco, "resumo"])
    saida = capsys.readouterr().out
    # 11 chamadas: 3 registradas, 6 calculadas, 1 sem preço e 1 erro de API (sem origem)
    assert "Modo de custo: auto · chamadas: 3 registrada(s), 6 calculada(s), 1 sem preço" in saida
    assert "modelo fora da tabela de preços" in saida

    main(["--db", banco, "ingest", "--modo", "display"])
    capsys.readouterr()
    main(["--db", banco, "resumo"])
    saida = capsys.readouterr().out
    assert "Modo de custo: display" in saida and "sem custo registrado no log" in saida


def test_banco_antigo_ganha_as_colunas_e_a_origem(tmp_path):
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.executescript("""
        CREATE TABLE chamadas (
            id TEXT PRIMARY KEY, ferramenta TEXT NOT NULL, sessao_id TEXT NOT NULL, projeto TEXT,
            ts TEXT NOT NULL, modelo TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, cache_write_5m INTEGER NOT NULL DEFAULT 0,
            cache_write_1h INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
            custo_usd REAL, subagente INTEGER NOT NULL DEFAULT 0, erro_api INTEGER NOT NULL DEFAULT 0,
            arquivo TEXT NOT NULL);
        INSERT INTO chamadas (id, ferramenta, sessao_id, ts, modelo, input_tokens, output_tokens, custo_usd, arquivo)
            VALUES ('claude_code:a:r', 'claude_code', 's', '2026-11-01T00:00:00Z', 'claude-opus-5-5', 1000, 1000, 0.024, 'f'),
                   ('claude_code:b:r', 'claude_code', 's', '2026-11-01T00:00:01Z', 'modelo-novo', 10, 10, NULL, 'f');
    """)
    antigo.commit()
    antigo.close()

    con = db.conectar(caminho)
    colunas = {r["name"] for r in con.execute("PRAGMA table_info(chamadas)")}
    assert {"custo_registrado", "origem_custo"} <= colunas
    r = resultado(con)
    assert r["a"] == (pytest.approx(CHEIO), "calculado")
    assert r["b"] == (None, "sem_preco")  # sem preço continua sem valor, não vira zero
    assert db.ler_modo(con) == "auto"
    con.close()
    db.conectar(caminho).close()  # abrir de novo não migra nem falha


def test_custo_registrado_e_so_numero(tmp_path):
    con = ingerir_em(tmp_path)
    con.commit()
    assert b"SEGREDO" not in (tmp_path / "c.db").read_bytes()
    tipos = {r[0] for r in con.execute("SELECT DISTINCT typeof(custo_registrado) FROM chamadas")}
    assert tipos <= {"real", "null"}
