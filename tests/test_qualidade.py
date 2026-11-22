"""Testes do `tc qualidade` (confiança dos dados), com logs sintéticos."""

import json
import sqlite3
from pathlib import Path

import pytest

from token_controler import custo, db, qualidade
from token_controler.cli import main
from token_controler.fontes import claude_code

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code" / "projects"


def linha(msg, modelo="claude-opus-5-5", usage=None, req=None, **extra):
    """Linha de resposta do assistente, escrita à mão (sem nenhum texto de conversa)."""
    d = {"type": "assistant", "sessionId": "sess-q", "cwd": "C:\\Users\\isa\\proj\\q",
         "timestamp": "2026-11-21T10:00:00Z", "requestId": req or f"r-{msg}",
         "message": {"id": msg, "model": modelo, "usage": usage if usage is not None else
                     {"input_tokens": 100, "output_tokens": 10}, "content": []}}
    d.update(extra)
    return json.dumps(d)


def pasta_com(tmp_path, *linhas_log):
    pasta = tmp_path / "logs" / "projects" / "proj"
    pasta.mkdir(parents=True)
    (pasta / "s.jsonl").write_text("\n".join(linhas_log) + "\n", encoding="utf-8")
    return tmp_path / "logs" / "projects"


def ingerido(tmp_path, *linhas_log):
    con = db.conectar(tmp_path / "q.db")
    claude_code.ingerir(con, pasta=pasta_com(tmp_path, *linhas_log))
    return con


def unico(con):
    (q,) = qualidade.coletar(con)
    return q


def test_fixture_do_claude_code(tmp_path):
    con = db.conectar(tmp_path / "f.db")
    claude_code.ingerir(con, pasta=FIXTURES)
    q = unico(con)
    assert q["ferramenta"] == "claude_code"
    assert (q["arquivos"], q["arquivos_sem_contagem"], q["linhas_ilegiveis"]) == (3, 0, 1)
    assert (q["chamadas"], q["erros_api"], q["chamadas_sem_tokens"]) == (5, 1, 0)
    assert q["modelos_sem_preco"] == [{"modelo": "gpt-9-desconhecido", "chamadas": 1}]
    assert q["chamadas_sem_preco"] == 1
    assert (q["origem_registrado"], q["origem_calculado"], q["origem_sem_preco"]) == (0, 3, 1)
    assert q["nivel_detalhe"] is None            # depende da T09: nunca inventado
    assert q["confianca"] == "baixa"
    assert any("gpt-9-desconhecido" in m for m in q["motivos"])
    assert any("1 linha(s) ilegível" in m for m in q["motivos"])


def test_dados_limpos_tem_confianca_alta(tmp_path):
    q = unico(ingerido(tmp_path, linha("m1"), linha("m2")))
    assert (q["linhas_ilegiveis"], q["chamadas_sem_tokens"], q["chamadas_sem_preco"]) == (0, 0, 0)
    assert q["confianca"] == "alta" and q["motivos"] == []


def test_chamada_sem_tokens_e_contada_e_nao_estimada(tmp_path):
    # linha de resposta sem bloco usage: o tc não inventa tokens, só aponta a lacuna
    sem_usage = json.dumps({"type": "assistant", "sessionId": "sess-q", "timestamp": "2026-11-21T10:00:00Z",
                            "requestId": "r-x", "message": {"id": "mx", "model": "claude-opus-5-5"}})
    q = unico(ingerido(tmp_path, linha("m1"), sem_usage))
    assert q["chamadas_sem_tokens"] == 1
    assert q["confianca"] == "baixa"
    assert any("sem contagem de tokens" in m for m in q["motivos"])


def test_erro_de_api_nao_conta_como_sem_tokens_nem_sem_preco(tmp_path):
    erro = linha("e1", modelo="<synthetic>", usage={"input_tokens": 0, "output_tokens": 0},
                 isApiErrorMessage=True)
    q = unico(ingerido(tmp_path, linha("m1"), erro))
    assert (q["erros_api"], q["chamadas_sem_tokens"], q["chamadas_sem_preco"]) == (1, 0, 0)
    assert q["confianca"] == "alta"


def test_linhas_ilegiveis_nao_dobram_ao_reprocessar(tmp_path):
    pasta = pasta_com(tmp_path, linha("m1"), "{quebrado", "{outra quebrada")
    con = db.conectar(tmp_path / "q.db")
    for tudo in (False, True, True):
        claude_code.ingerir(con, pasta=pasta, tudo=tudo)
        assert unico(con)["linhas_ilegiveis"] == 2


def test_arquivo_lido_antes_da_t11_fica_sem_contagem_e_a_confianca_e_media(tmp_path):
    con = ingerido(tmp_path, linha("m1"))
    con.execute("UPDATE arquivos SET linhas_ruins = NULL")
    q = unico(con)
    assert q["arquivos_sem_contagem"] == 1
    assert q["linhas_ilegiveis"] is None          # desconhecido, não zero
    assert q["confianca"] == "media"
    assert "tc ingest --tudo" in q["motivos"][0]


def test_banco_antigo_recebe_a_coluna_com_null(tmp_path):
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.executescript("""
        CREATE TABLE arquivos (caminho TEXT PRIMARY KEY, ferramenta TEXT NOT NULL, tamanho INTEGER NOT NULL,
                               mtime REAL NOT NULL, processado_em TEXT NOT NULL);
        INSERT INTO arquivos VALUES ('a.jsonl', 'claude_code', 10, 1.0, '2026-11-01T00:00:00+00:00');
    """)
    antigo.commit()
    antigo.close()
    con = db.conectar(caminho)
    assert con.execute("SELECT linhas_ruins FROM arquivos").fetchone()[0] is None
    assert unico(con)["arquivos_sem_contagem"] == 1


def test_modelo_sem_preco_independe_do_modo_de_custo(tmp_path):
    con = ingerido(tmp_path, linha("m1"), linha("m2", modelo="modelo-novo-9"))
    antes = unico(con)["modelos_sem_preco"]
    custo.recalcular(con, "display")  # no display, todas ficam sem custo registrado
    q = unico(con)
    assert q["modelos_sem_preco"] == antes == [{"modelo": "modelo-novo-9", "chamadas": 1}]
    assert q["origem_sem_preco"] == 2


def test_preco_novo_na_tabela_vale_sem_reingerir(tmp_path, monkeypatch):
    con = ingerido(tmp_path, linha("m1", modelo="modelo-novo-9"))
    assert unico(con)["confianca"] == "baixa"
    monkeypatch.setattr(qualidade, "achar_preco", lambda modelo: (1, 1, 1, 1, 1))
    assert unico(con)["modelos_sem_preco"] == []


def test_filtro_por_ferramenta_e_banco_vazio(tmp_path):
    con = ingerido(tmp_path, linha("m1"))
    assert qualidade.coletar(con, "copilot") == []
    assert [q["ferramenta"] for q in qualidade.coletar(con, "claude_code")] == ["claude_code"]
    assert qualidade.coletar(db.conectar(tmp_path / "vazio.db")) == []


def test_cli_tabela_e_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    banco = str(tmp_path / "cli.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()

    main(["--db", banco, "qualidade"])
    saida = capsys.readouterr().out
    assert "Qualidade dos dados · modo de custo: auto" in saida
    assert "claude_code" in saida and "baixa" in saida
    assert "gpt-9-desconhecido" in saida and "T09" in saida
    assert "SEGREDO" not in saida

    main(["--db", banco, "qualidade", "--json"])
    dados = json.loads(capsys.readouterr().out)
    assert isinstance(dados, list) and dados[0]["ferramenta"] == "claude_code"
    assert dados[0]["modelos_sem_preco"] == [{"modelo": "gpt-9-desconhecido", "chamadas": 1}]
    assert dados[0]["nivel_detalhe"] is None

    main(["--db", banco, "qualidade", "--ferramenta", "copilot", "--json"])
    assert json.loads(capsys.readouterr().out) == []
    main(["--db", banco, "qualidade", "--ferramenta", "copilot"])
    assert "sem dados" in capsys.readouterr().out
