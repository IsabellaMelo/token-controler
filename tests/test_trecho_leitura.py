"""T19: trecho lido por um `Read` (offset/limit, só dois números) e a releitura por sobreposição de linhas.

Na fixture `claude_code_sessao`, dados.py é lido em quatro `Read` no prompt 3:
  tP1 linhas 1-100, tP2 linhas 101-200 (outro trecho: normal), tP3 linhas 150-249 (repete 150-200, 51% do trecho) e tP4 sem
  offset/limit (arquivo inteiro: repete 1-249 de até 2000 linhas, ~12%, abaixo do limiar de 25%).
"""

import sqlite3
from pathlib import Path

import pytest

from token_controler import alvo, cli, db, detalhe, diagnostico
from token_controler.fontes import claude_code

SESSAO_LOGS = Path(__file__).parent / "fixtures" / "claude_code_sessao" / "projects"
SESSAO = "sess-eeee-5555"
LIM = diagnostico.carregar_limites(None)[0]


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "t.db")
    claude_code.ingerir(c, pasta=SESSAO_LOGS)
    yield c
    c.close()


def trecho(con, tool_id):
    r = con.execute("SELECT leitura_ini, leitura_qtd FROM tool_calls WHERE id = ?", (f"claude_code:{tool_id}",)).fetchone()
    return tuple(r)


def marcas(con, tool_id, **mudancas):
    d = detalhe.analisar(con, SESSAO, {**LIM, **mudancas})
    achados = {}

    def visitar(it):
        if it["tipo"] == "tool":
            achados[it["id"].split(":")[1]] = it
            list(map(visitar, it["filhos"]))
        elif it["tipo"] == "subagente":
            list(map(visitar, it["filhos"]))

    for t in d["turnos"]:
        list(map(visitar, t["itens"]))
    return achados[tool_id]["marcas"], d


# ---------- a função ----------

@pytest.mark.parametrize("nome, entrada, esperado", [
    ("Read", {"file_path": "a.py"}, (1, None)),                              # sem offset/limit: o arquivo inteiro
    ("Read", {"file_path": "a.py", "offset": 186, "limit": 60}, (186, 60)),
    ("Read", {"file_path": "a.py", "offset": 340}, (340, None)),             # até o fim
    ("Read", {"file_path": "a.py", "limit": 100}, (1, 100)),
    ("Read", {"file_path": "a.py", "offset": 0}, (1, None)),                 # 0 vale como começo do arquivo
    ("FileReadTool", {"file_path": "a.py", "offset": 5, "limit": 5}, (5, 5)),
])
def test_trecho_da_leitura(nome, entrada, esperado):
    assert alvo.trecho_da_leitura(nome, entrada) == esperado


@pytest.mark.parametrize("nome, entrada", [
    ("Read", {"file_path": "a.py", "offset": "10"}),                         # texto, não número
    ("Read", {"file_path": "a.py", "offset": True}),                          # booleano não é número
    ("Read", {"file_path": "a.py", "offset": -3}),
    ("Read", {"file_path": "a.py", "offset": 1.5}),
    ("Read", {"file_path": "a.py", "limit": 0}),
    ("Read", {"file_path": "a.py", "limit": "tudo"}),
    ("Read", {"file_path": "a.py", "offset": 10 ** 12}),
    ("Read", {"file_path": "doc.pdf", "pages": "1-5"}),                       # outro modo de recorte: desconhecido
    ("Read", {"offset": 1, "limit": 2}),                                      # sem arquivo
    ("Read", "texto"),
    ("Read", None),
    ("Edit", {"file_path": "a.py", "offset": 1, "limit": 2}),                 # só Read grava trecho
    ("Grep", {"path": "x", "offset": 1, "limit": 2}),
    ("Bash", {"command": "ls", "offset": 1}),
])
def test_trecho_desconhecido_vira_none_none(nome, entrada):
    assert alvo.trecho_da_leitura(nome, entrada) == (None, None)


# ---------- o que vai para o banco ----------

def test_grava_so_o_trecho_dos_read(con):
    assert trecho(con, "tP1") == (1, 100) and trecho(con, "tP2") == (101, 100) and trecho(con, "tP3") == (150, 100)
    assert trecho(con, "tP4") == (1, None) and trecho(con, "tA1") == (1, None)
    for nao_leitura in ("tE1", "tB1", "tAg", "tS2", "tF1"):                     # Edit, Bash, Agent, Grep
        assert trecho(con, nao_leitura) == (None, None)


def test_so_inteiros_e_so_em_read(con):
    linhas = con.execute("SELECT tool, leitura_ini, leitura_qtd FROM tool_calls WHERE leitura_ini IS NOT NULL").fetchall()
    assert linhas and all(r["tool"] == "Read" for r in linhas)
    assert all(isinstance(r["leitura_ini"], int) and r["leitura_qtd"] in (None,) + tuple(range(1, 10 ** 6)) for r in linhas)
    colunas = {r["name"]: r["type"] for r in con.execute("PRAGMA table_info(tool_calls)")}
    assert colunas["leitura_ini"] == colunas["leitura_qtd"] == "INTEGER"


def test_chave_false_nao_grava_o_trecho_mas_grava_o_nome(tmp_path):
    (tmp_path / "config.json").write_text('{"gravar_trecho_leitura": false}', encoding="utf-8")
    c = db.conectar(tmp_path / "t.db")
    claude_code.ingerir(c, pasta=SESSAO_LOGS)
    assert db.gravar_trecho_leitura(c) is False and db.gravar_nome_arquivo(c) is True
    assert c.execute("SELECT COUNT(*) FROM tool_calls WHERE leitura_ini IS NOT NULL OR leitura_qtd IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM tool_calls WHERE alvo_nome IS NOT NULL").fetchone()[0] > 0
    c.close()


def test_banco_antigo_ganha_as_colunas_de_trecho_com_null(tmp_path):
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.executescript("""
        CREATE TABLE tool_calls (id TEXT PRIMARY KEY, ferramenta TEXT NOT NULL, sessao_id TEXT NOT NULL,
            projeto TEXT, ts TEXT NOT NULL, tool TEXT NOT NULL, erro INTEGER,
            subagente INTEGER NOT NULL DEFAULT 0, arquivo TEXT NOT NULL, duracao_ms INTEGER, alvo_hash TEXT, subtipo TEXT,
            alvo_nome TEXT);
        INSERT INTO tool_calls VALUES ('claude_code:x', 'claude_code', 's', 'p', '2026-11-01T00:00:00Z', 'Read', 0, 0, 'f',
                                       10, 'abcd', NULL, 'a.py');
    """)
    antigo.commit()
    antigo.close()
    c = db.conectar(caminho)
    assert tuple(c.execute("SELECT leitura_ini, leitura_qtd FROM tool_calls").fetchone()) == (None, None)
    c.close()


def test_limpar_trechos_apaga_so_os_trechos(tmp_path, capsys):
    caminho = tmp_path / "t.db"
    c = db.conectar(caminho)
    claude_code.ingerir(c, pasta=SESSAO_LOGS)
    nomes = c.execute("SELECT COUNT(*) FROM tool_calls WHERE alvo_nome IS NOT NULL").fetchone()[0]
    c.close()
    cli.main(["--db", str(caminho), "limpar-trechos"])
    saida = capsys.readouterr().out
    assert "trecho(s) de leitura apagado(s)" in saida and "gravar_trecho_leitura" in saida
    c = db.conectar(caminho)
    assert c.execute("SELECT COUNT(*) FROM tool_calls WHERE leitura_ini IS NOT NULL OR leitura_qtd IS NOT NULL").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM tool_calls WHERE alvo_nome IS NOT NULL").fetchone()[0] == nomes
    c.close()


def test_limpar_trechos_com_a_chave_desligada_nao_avisa(tmp_path, capsys):
    (tmp_path / "config.json").write_text('{"gravar_trecho_leitura": false}', encoding="utf-8")
    cli.main(["--db", str(tmp_path / "t.db"), "limpar-trechos"])
    saida = capsys.readouterr().out
    assert "0 trecho(s)" in saida and "Atenção" not in saida


# ---------- releitura por sobreposição de linhas ----------

def test_trechos_diferentes_do_mesmo_arquivo_nao_sao_releitura(con):
    assert marcas(con, "tP1")[0] == [] and marcas(con, "tP2")[0] == []         # 1-100 e 101-200 (vizinhos, sem sobrepor)


def test_trechos_que_se_sobrepoem_mostram_quais_linhas_se_repetiram(con):
    m3 = marcas(con, "tP3")[0]
    assert len(m3) == 1 and m3[0].startswith("releitura de linhas 150–200 (51% do trecho lido; já lido às ")   # 51 de 100 linhas
    assert marcas(con, "tP4")[0] == []                                          # 249 de 2000 linhas: 12%, abaixo de 25%
    m4 = marcas(con, "tP4", releitura_min_sobreposicao=0.10)[0]                 # com o limiar mais baixo, a união conta
    assert len(m4) == 1 and m4[0].startswith("releitura de linhas 1–249 (12% do trecho lido; já lido às ")


def test_arquivo_inteiro_duas_vezes_e_releitura_de_arquivo_inteiro(con):
    m = marcas(con, "tA2")[0]
    assert len(m) == 1 and m[0].startswith("releitura de arquivo inteiro (100% do trecho lido; já lido às ")


def test_o_no_da_arvore_mostra_o_trecho_lido(con):
    _, d = marcas(con, "tP1")
    texto = detalhe.em_texto(detalhe.montar(d))
    assert "dados.py · linhas 1–100" in texto and "dados.py · linhas 101–200" in texto
    assert "dados.py · arquivo inteiro" in texto and "analise.py · arquivo inteiro" in texto


def test_problema_de_releitura_do_arquivo_lido_por_trechos(con):
    _, d = marcas(con, "tP1")
    (p,) = [p for p in d["problemas"] if p["tipo"] == "releitura" and p["motivo"].startswith("dados.py")]
    assert p["dados"] == {"releituras": 1, "certa": True} and p["custo_usd"] > 0


def test_sem_o_trecho_gravado_a_releitura_e_so_possivel(con):
    """Sessão ingerida antes das colunas: o `tc` não sabe o trecho e volta a desconfiar, dizendo isso."""
    con.execute("UPDATE tool_calls SET leitura_ini = NULL, leitura_qtd = NULL")
    m2, d = marcas(con, "tP2")
    assert m2[0].startswith("possível releitura (o trecho lido não foi gravado; já lido às ")
    assert not any(p["dados"].get("certa") for p in d["problemas"] if p["tipo"] == "releitura")
    assert all("possível(is) releitura(s)" in p["motivo"] and "tc ingest --tudo" in p["motivo"]
               for p in d["problemas"] if p["tipo"] == "releitura")
    assert all(p["severidade"] == "baixa" for p in d["problemas"] if p["tipo"] == "releitura")
    texto = detalhe.em_texto(detalhe.montar(d))
    assert "linhas 1–100" not in texto                                          # sem trecho, o nó não inventa um


def test_trecho_conhecido_so_de_um_lado_tambem_e_so_possivel(con):
    con.execute("UPDATE tool_calls SET leitura_ini = NULL, leitura_qtd = NULL WHERE id = 'claude_code:tP2'")
    assert marcas(con, "tP3")[0][0].startswith("possível releitura")           # não dá para comparar com tP2
    assert marcas(con, "tP1")[0] == []


def test_edicao_entre_as_leituras_zera_a_sobreposicao(con):
    con.execute("""UPDATE tool_calls SET tool = 'Edit', erro = 0, leitura_ini = NULL, leitura_qtd = NULL
                   WHERE id = 'claude_code:tP2'""")                              # tP2 passa a ser um Edit em dados.py
    con.execute("UPDATE tool_calls SET alvo_hash = (SELECT alvo_hash FROM tool_calls WHERE id = 'claude_code:tP3') "
                "WHERE id = 'claude_code:tP2'")
    assert marcas(con, "tP3")[0] == [] and marcas(con, "tP4")[0] == []
    assert marcas(con, "tP4", releitura_min_sobreposicao=0.04)[0][0].startswith("releitura de linhas 150–249")   # só tP3 vale


def test_json_traz_o_trecho(con):
    _, d = marcas(con, "tP1")
    item = next(it for t in d["turnos"] for it in t["itens"] if it["tipo"] == "tool" and it["id"].endswith(":tP3"))
    assert (item["leitura_ini"], item["leitura_qtd"], item["trecho"]) == (150, 100, "linhas 150–249")


# ---------- imagens ficam fora da regra de releitura ----------

def renomear(con, nome, ids):
    marcas_ = ",".join("?" * len(ids))
    con.execute(f"UPDATE tool_calls SET alvo_nome = ? WHERE id IN ({marcas_})", (nome, *[f"claude_code:{i}" for i in ids]))


def test_imagem_lida_de_novo_nao_e_releitura(con):
    renomear(con, "sheet1.JPG", ["tA1", "tA2"])                                  # tA2 era a releitura desnecessária
    m, d = marcas(con, "tA2")
    assert m == []
    assert [p for p in d["problemas"] if p["tipo"] == "releitura" and p["motivo"].startswith("sheet1")] == []


def test_so_a_imagem_sai_o_codigo_continua_checado(con):
    renomear(con, "sheet1.png", ["tA1", "tA2", "tA3", "tA4", "tA5"])              # todas as leituras de analise.py viram imagem
    assert marcas(con, "tP3")[0] != []                                            # dados.py (código) segue checado
    assert all(marcas(con, i)[0] == [] for i in ("tA2", "tA4"))                    # a "imagem" não gera releitura


@pytest.mark.parametrize("nome", ["a.jpg", "a.JPEG", "a.png", "a.gif", "a.webp", "a.bmp", "a.tiff", "a.heic", "a.avif", "a.ico"])
def test_extensoes_de_imagem_padrao(con, nome):
    renomear(con, nome, ["tA1", "tA2"])
    assert marcas(con, "tA2")[0] == []


@pytest.mark.parametrize("nome", ["a.py", "a.svg", "a.jpg.py", "foto", "jpg", "a.pdf"])
def test_outras_extensoes_continuam_na_regra(con, nome):
    renomear(con, nome, ["tA1", "tA2"])
    assert marcas(con, "tA2")[0] != []


def test_sem_o_nome_gravado_o_arquivo_ainda_e_checado(con):
    con.execute("UPDATE tool_calls SET alvo_nome = NULL")                        # não dá para saber a extensão
    assert marcas(con, "tA2")[0] != []


def test_lista_de_extensoes_e_ajustavel_no_config(con, tmp_path):
    renomear(con, "a.jpg", ["tA1", "tA2"])
    assert marcas(con, "tA2", releitura_ignora_extensoes=[])[0] != []             # lista vazia: nada é ignorado
    assert marcas(con, "tA2", releitura_ignora_extensoes=[".JPG"])[0] == []       # caixa não importa
    (tmp_path / "config.json").write_text('{"diagnostico": {"releitura_ignora_extensoes": [".py"]}}', encoding="utf-8")
    limites, avisos = diagnostico.carregar_limites(tmp_path / "config.json")
    assert limites["releitura_ignora_extensoes"] == [".py"] and avisos == []
    (tmp_path / "config.json").write_text('{"diagnostico": {"releitura_ignora_extensoes": "jpg"}}', encoding="utf-8")
    limites, avisos = diagnostico.carregar_limites(tmp_path / "config.json")
    assert ".jpg" in limites["releitura_ignora_extensoes"] and avisos                # valor inválido: usa o padrão e avisa


# ---------- sobreposição mínima (25% do trecho lido agora) ----------

def trechos(con, p1, p2, p3, p4=(100000, 10)):
    """Põe os trechos (linha inicial, quantidade) nas quatro leituras de dados.py da fixture."""
    for tid, (ini, qtd) in zip(("tP1", "tP2", "tP3", "tP4"), (p1, p2, p3, p4)):
        con.execute("UPDATE tool_calls SET leitura_ini = ?, leitura_qtd = ? WHERE id = ?", (ini, qtd, f"claude_code:{tid}"))


def test_tres_linhas_de_oitenta_nao_sao_releitura(con):
    """O caso real que motivou o limite: ler as linhas 278-357 (80 linhas) repetiu só 3 delas (355-357), já lidas antes."""
    trechos(con, (1, 100), (355, 5), (278, 80))                                   # 355-357 já lidas: 3 de 80 = 3,75%
    assert marcas(con, "tP3")[0] == []


@pytest.mark.parametrize("ini, repetidas, flagrado", [(177, 24, False), (176, 25, True), (101, 100, True), (200, 1, False)])
def test_limiar_de_25_por_cento_do_trecho_lido_agora(con, ini, repetidas, flagrado):
    trechos(con, (1, 50), (101, 100), (ini, 100))                                 # tP2 = 101-200; tP3 = ini..ini+99
    m = marcas(con, "tP3")[0]
    assert bool(m) == flagrado
    if flagrado:
        assert f"({repetidas}% do trecho lido" in m[0]


def test_varias_leituras_pequenas_somam_a_sobreposicao(con):
    trechos(con, (1, 5), (21, 5), (1, 40))                                        # 5 + 5 = 10 de 40 = 25%, cada uma sozinha só 12,5%
    (m,) = marcas(con, "tP3")[0]
    assert m.startswith("releitura de linhas 1–5 e linhas 21–25 (25% do trecho lido")


def test_espiar_poucas_linhas_e_ler_o_arquivo_todo_nao_e_releitura(con):
    trechos(con, (1, 3), (100000, 1), (1, None))                                  # 3 linhas de até 2000: 0,15%
    assert marcas(con, "tP3")[0] == []


def test_ler_um_trecho_que_ja_estava_no_arquivo_inteiro_e_releitura_total(con):
    trechos(con, (1, None), (100000, 1), (300, 20))                               # o arquivo inteiro já tinha tudo
    (m,) = marcas(con, "tP3")[0]
    assert m.startswith("releitura de linhas 300–319 (100% do trecho lido")


def test_arquivo_lido_so_ate_a_linha_2000_nao_cobre_o_resto(con):
    trechos(con, (1, None), (100000, 1), (2500, 100))                             # o Read sem limit para na linha 2000
    assert marcas(con, "tP3")[0] == []


def test_limiar_e_ajustavel_no_config(con, tmp_path):
    trechos(con, (1, 50), (101, 100), (177, 100))                                 # 24%
    assert marcas(con, "tP3")[0] == []
    assert marcas(con, "tP3", releitura_min_sobreposicao=0.2)[0] != []
    assert marcas(con, "tP3", releitura_min_sobreposicao=0)[0] != []              # 0 = qualquer sobreposição, como antes
    (tmp_path / "config.json").write_text('{"diagnostico": {"releitura_min_sobreposicao": 0.5}}', encoding="utf-8")
    limites, avisos = diagnostico.carregar_limites(tmp_path / "config.json")
    assert limites["releitura_min_sobreposicao"] == 0.5 and avisos == []
