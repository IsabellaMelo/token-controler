"""Testes da saída --json de resumo, sessoes e tools (fixtures sintéticas em tests/fixtures)."""

import json
from pathlib import Path

import pytest

from token_controler.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code" / "projects"


@pytest.fixture
def banco(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    arquivo = str(tmp_path / "json.db")
    main(["--db", arquivo, "ingest"])
    capsys.readouterr()  # descarta a saída do ingest
    return arquivo


def saida_json(banco, capsys, *args):
    """Roda o comando e devolve o JSON impresso. Falha se a saída tiver qualquer coisa além do JSON."""
    main(["--db", banco, *args, "--json"])
    return json.loads(capsys.readouterr().out)


def por(lista, chave, valor):
    return next(l for l in lista if l[chave] == valor)


def test_resumo_json_e_lista_de_objetos_com_numeros_crus(banco, capsys):
    linhas = saida_json(banco, capsys, "resumo")
    assert isinstance(linhas, list) and {l["projeto"] for l in linhas} == {"proj-um", "proj-dois"}
    for linha in linhas:
        for campo, valor in linha.items():
            if campo != "projeto":  # número ou null, nunca texto formatado ("1.5k", "$0.12", "98%")
                assert valor is None or (isinstance(valor, (int, float)) and not isinstance(valor, bool)), campo


def test_resumo_json_valores_do_projeto(banco, capsys):
    proj_um = por(saida_json(banco, capsys, "resumo"), "projeto", "proj-um")
    assert proj_um["sessoes"] == 1
    assert proj_um["prompts"] == 2
    assert proj_um["chamadas"] == 4
    assert proj_um["tools"] == 3 and proj_um["erros_tool"] == 1
    assert proj_um["erros_api"] == 1
    assert (proj_um["input"], proj_um["output"]) == (160, 550)
    assert (proj_um["cache_write"], proj_um["cache_read"]) == (21000, 20000)
    assert proj_um["cache_taxa"] == pytest.approx(20000 / (160 + 21000 + 20000))
    assert proj_um["custo_usd"] == pytest.approx((106040 + 13200 + 35) / 1e6)
    assert proj_um["chamadas_sem_preco"] == 0  # a chamada de erro de API não conta como sem preço


def test_resumo_json_modelo_sem_preco_nao_recebe_valor_chutado(banco, capsys):
    proj_dois = por(saida_json(banco, capsys, "resumo"), "projeto", "proj-dois")
    assert proj_dois["custo_usd"] is None
    assert proj_dois["chamadas_sem_preco"] == 1
    assert proj_dois["tools"] is None and proj_dois["erros_tool"] is None  # a tabela mostra "-"


def test_resumo_json_por_modelo_marca_nao_se_aplica_com_null(banco, capsys):
    linhas = saida_json(banco, capsys, "resumo", "--por", "modelo")
    desconhecido = por(linhas, "modelo", "gpt-9-desconhecido")
    assert desconhecido["chamadas_sem_preco"] == 1
    assert all(l["prompts"] is None and l["tools"] is None and l["erros_tool"] is None for l in linhas)


def test_resumo_json_por_sessao_traz_o_id_completo(banco, capsys, monkeypatch):
    linhas = saida_json(banco, capsys, "resumo", "--por", "sessao")
    assert {l["sessao"] for l in linhas} == {"sess-aaaa-1111", "sess-bbbb-2222"}
    # a tabela continua truncando o ID
    main(["--db", banco, "resumo", "--por", "sessao"])
    assert "sess-aaaa-11…" in capsys.readouterr().out


def test_resumo_json_respeita_filtros_e_devolve_lista_vazia(banco, capsys):
    assert saida_json(banco, capsys, "resumo", "--projeto", "nao-existe") == []
    so_proj_um = saida_json(banco, capsys, "resumo", "--projeto", "proj-um")
    assert [l["projeto"] for l in so_proj_um] == ["proj-um"]


def test_sessoes_json(banco, capsys):
    sessoes = saida_json(banco, capsys, "sessoes")
    assert [s["sessao"] for s in sessoes] == ["sess-aaaa-1111", "sess-bbbb-2222"]  # mais recente primeiro
    s = sessoes[0]
    assert s["projeto"] == "proj-um"
    assert s["inicio"] == "2026-11-21T12:00:00Z"
    assert s["duracao_s"] == 1230          # 12:00:00 até 12:20:30
    assert s["ativo_s"] == 30              # a pausa de 20 min não conta como tempo ativo
    assert (s["prompts"], s["chamadas"], s["tools"], s["erros_tool"]) == (2, 4, 3, 1)
    assert s["custo_usd"] == pytest.approx((106040 + 13200 + 35) / 1e6)
    assert all(isinstance(s[c], int) for c in ("duracao_s", "ativo_s"))
    assert sessoes[1]["custo_usd"] is None


def test_sessoes_json_respeita_o_limite(banco, capsys):
    assert len(saida_json(banco, capsys, "sessoes", "--limite", "1")) == 1


def test_tools_json(banco, capsys):
    tools = saida_json(banco, capsys, "tools")
    assert sum(t["chamadas"] for t in tools) == 3
    assert sum(t["erros"] for t in tools) == 1
    for t in tools:
        assert t["taxa_erro"] == pytest.approx(t["erros"] / t["chamadas"])
    assert "Bash" in {t["tool"] for t in tools}


def test_json_nao_vaza_texto_das_conversas(banco, capsys):
    saida = ""
    for cmd in (["resumo"], ["resumo", "--por", "sessao"], ["sessoes"], ["tools"]):
        main(["--db", banco, *cmd, "--json"])
        saida += capsys.readouterr().out
    for texto in ("SEGREDO", "analise os dados", "gera relatorio", "e agora?"):
        assert texto not in saida


def test_tabelas_continuam_iguais_sem_json(banco, capsys):
    main(["--db", banco, "resumo"])
    saida = capsys.readouterr().out
    assert "proj-um" in saida and "$0.12" in saida and "$0.12*" not in saida
    assert "proj-dois" in saida and "-*" in saida  # sem preço: custo "-" com asterisco
    assert not saida.lstrip().startswith(("[", "{"))
