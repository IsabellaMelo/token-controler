"""T16: `tc diagnostico`. Os dados entram direto no banco (números sintéticos), para controlar cada limite."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from token_controler import db, diagnostico as dg
from token_controler.cli import main

BASE = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
LIM = dg.carregar_limites(None)[0]
_n = [0]


def ts(minutos=0.0):
    return (BASE + timedelta(minutes=minutos)).strftime("%Y-%m-%dT%H:%M:%SZ")


def proximo():
    _n[0] += 1
    return _n[0]


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "d.db")
    yield c
    c.close()


def chamada(con, sessao="s1", min=0, projeto="proj", modelo="claude-opus-5-5", inp=100, out=10, w5=0, w1=0, cr=0,
            custo=0.01, sub=0, erro_api=0):
    con.execute("""INSERT INTO chamadas (id, ferramenta, sessao_id, projeto, ts, modelo, input_tokens, output_tokens,
                   cache_write_5m, cache_write_1h, cache_read, custo_usd, subagente, erro_api, arquivo)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"claude_code:m{proximo()}:r", "claude_code", sessao, projeto, ts(min), modelo, inp, out, w5, w1, cr,
                 custo, sub, erro_api, "f"))


def tool(con, nome="Bash", sessao="s1", min=0, erro=0, dur_ms=None, alvo=None, sub=0, projeto="proj"):
    con.execute("""INSERT INTO tool_calls (id, ferramenta, sessao_id, projeto, ts, tool, erro, subagente, arquivo,
                   duracao_ms, alvo_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (f"claude_code:t{proximo()}", "claude_code", sessao, projeto, ts(min), nome, erro, sub, "f", dur_ms, alvo))


def evento(con, sessao="s1", min=0, tipo="atividade", projeto="proj"):
    con.execute("INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo) VALUES (?,?,?,?,?,?)",
                ("claude_code", sessao, projeto, ts(min), tipo, "f"))


def sinais(con, sessao="s1", limites=None):
    return {r["sinal"]: r for r in dg.diagnosticar(con, limites or LIM, [sessao])}


# ---------- cache baixo ----------

def test_cache_baixo_exige_mais_de_10_chamadas_e_taxa_abaixo_do_minimo(con):
    for i in range(10):
        chamada(con, min=i, inp=1000, cr=100)
    assert "cache_baixo" not in sinais(con)                  # 10 chamadas: ainda não passa de 10
    chamada(con, min=11, inp=1000, cr=100)
    r = sinais(con)["cache_baixo"]
    assert r["severidade"] == "alta" and r["dados"]["chamadas"] == 11
    assert r["dados"]["taxa"] == pytest.approx(0.0909, abs=1e-3)
    assert "cache 9% em 11 chamadas" in r["evidencia"]


def test_cache_baixo_severidade_media_e_sem_sinal_quando_ok(con):
    for i in range(12):
        chamada(con, min=i, inp=500, cr=500)                 # 50%: abaixo de 80%, não abaixo de 50%
    assert sinais(con)["cache_baixo"]["severidade"] == "media"
    con.execute("UPDATE chamadas SET input_tokens = 100, cache_read = 900")
    assert "cache_baixo" not in sinais(con)                  # 90%


# ---------- re-criação de cache e pausa longa ----------

def test_recriacao_de_cache_so_apos_pausa_e_com_gravacao_grande(con):
    chamada(con, min=0, w5=20000)                            # a primeira chamada não conta
    chamada(con, min=2, w5=20000)                            # gravou muito, mas sem pausa
    chamada(con, min=20, w5=5000)                            # pausa, mas gravou pouco
    assert "recriacao_cache" not in sinais(con)
    chamada(con, min=40, w5=20000)                           # pausa de 20 min e 20k gravados
    r = sinais(con)["recriacao_cache"]
    assert r["dados"]["recriacoes"] == 1 and r["dados"]["tokens_regravados"] == 20000
    assert r["severidade"] == "baixa"
    assert r["impacto_usd"] == pytest.approx(20000 * (5.00 - 0.20) / 1e6)   # opus 5.5: gravar 5,00 contra ler 0,20


def test_recriacao_usa_o_preco_da_gravacao_de_1h_e_ignora_subagentes(con):
    chamada(con, min=0)
    chamada(con, min=10, w1=20000)
    chamada(con, min=30, w5=50000, sub=1)                    # subagente tem cache próprio: fora da conta
    r = sinais(con)["recriacao_cache"]
    assert r["dados"]["recriacoes"] == 1
    assert r["impacto_usd"] == pytest.approx(20000 * (8.00 - 0.20) / 1e6)


def test_recriacao_de_modelo_sem_preco_fica_sem_impacto(con):
    chamada(con, min=0, modelo="modelo-novo-9")
    chamada(con, min=10, modelo="modelo-novo-9", w5=20000)
    r = sinais(con)["recriacao_cache"]
    assert r["impacto_usd"] is None and r["dados"]["sem_preco"] == 1 and "sem preço" in r["evidencia"]


def test_tres_recriacoes_viram_severidade_media(con):
    for i in range(4):
        chamada(con, min=i * 20, w5=20000)
    assert sinais(con)["recriacao_cache"]["severidade"] == "media"


def test_pausa_longa(con):
    for m in (0, 4, 8):                                      # intervalos de 4 min: abaixo do limite
        evento(con, min=m)
    assert "pausa_longa" not in sinais(con)
    evento(con, min=20)                                      # 12 min
    r = sinais(con)["pausa_longa"]
    assert r["dados"]["pausas"] == 1 and r["dados"]["maior_s"] == 720 and r["severidade"] == "baixa"
    for m in (40, 60):
        evento(con, min=m)
    assert sinais(con)["pausa_longa"]["severidade"] == "media"


# ---------- loop de retry ----------

def test_loop_de_retry_3_media_5_alta(con):
    for i in range(3):
        tool(con, "Bash", min=i, erro=1)
    r = sinais(con)["loop_retry"]
    assert (r["severidade"], r["dados"]["maior_sequencia"], r["dados"]["tool"]) == ("media", 3, "Bash")
    for i in range(3, 5):
        tool(con, "Bash", min=i, erro=1)
    assert sinais(con)["loop_retry"]["severidade"] == "alta"


def test_loop_exige_falhas_seguidas_da_mesma_tool(con):
    tool(con, "Bash", min=0, erro=1)
    tool(con, "Read", min=1, erro=0)                         # sucesso no meio quebra a sequência
    tool(con, "Bash", min=2, erro=1)
    tool(con, "Bash", min=3, erro=1)
    tool(con, "Bash", min=4, erro=None)                      # sem resultado também quebra
    tool(con, "Bash", min=5, erro=1)
    tool(con, "Edit", min=6, erro=1)                         # outra tool: recomeça
    tool(con, "Bash", min=7, erro=1)
    assert "loop_retry" not in sinais(con)


def test_loop_ignora_tools_de_subagentes(con):
    tool(con, "Bash", min=0, erro=1)
    tool(con, "Bash", min=1, erro=1, sub=1)
    tool(con, "Bash", min=2, erro=1, sub=1)
    tool(con, "Bash", min=3, erro=1)
    assert "loop_retry" not in sinais(con)


# ---------- leitura repetida ----------

def test_leitura_repetida(con):
    for i in range(2):
        tool(con, "Read", min=i, alvo="aaaa")
    assert "leitura_repetida" not in sinais(con)
    tool(con, "Read", min=3, alvo="aaaa")
    r = sinais(con)["leitura_repetida"]
    assert (r["severidade"], r["dados"]["arquivos"], r["dados"]["maximo_leituras"]) == ("baixa", 1, 3)
    for i in range(4, 6):
        tool(con, "Read", min=i, alvo="aaaa")
    assert sinais(con)["leitura_repetida"]["severidade"] == "media"


def test_leitura_repetida_so_conta_read_da_thread_principal_com_alvo(con):
    for i in range(5):
        tool(con, "Edit", min=i, alvo="bbbb")                # edição não é leitura
        tool(con, "Read", min=i, alvo="cccc", sub=1)         # subagente relê o que o pai leu: esperado
        tool(con, "Read", min=i, alvo=None)                  # sem alvo conhecido
    assert "leitura_repetida" not in sinais(con)


# ---------- tool lenta ----------

def popular_bash(con, n=20, dur_ms=1000):
    for i in range(n):
        tool(con, "Bash", min=i, dur_ms=dur_ms)


def test_tool_lenta_precisa_passar_do_p95_da_tool_e_do_piso(con):
    popular_bash(con)
    tool(con, "Bash", min=30, dur_ms=90_000)                 # 21 medições: o p95 é 1 s
    r = sinais(con)["tool_lenta"]
    assert r["dados"]["chamadas"] == 1 and r["dados"]["por_tool"]["Bash"]["p95_s"] == 1.0
    assert r["dados"]["total_s"] == 90 and r["severidade"] == "baixa"
    assert "pior: Bash" in r["evidencia"]


def test_tool_lenta_abaixo_do_piso_ou_sem_amostra_suficiente_nao_conta(con):
    popular_bash(con)
    tool(con, "Bash", min=30, dur_ms=20_000)                 # acima do p95, mas abaixo de 30 s
    for i in range(5):
        tool(con, "Grep", min=i, dur_ms=1000)
    tool(con, "Grep", min=9, dur_ms=300_000)                 # só 6 medições do Grep: sem p95
    assert "tool_lenta" not in sinais(con)


def test_tools_que_esperam_a_pessoa_nao_sao_lentas(con):
    for i in range(20):
        tool(con, "AskUserQuestion", min=i, dur_ms=1000)
    tool(con, "AskUserQuestion", min=30, dur_ms=900_000)
    assert "tool_lenta" not in sinais(con)


def test_tool_lenta_media_quando_o_tempo_somado_passa_de_10_min(con):
    popular_bash(con, n=40)                                  # 42 medições: o p95 continua sendo 1 s
    for i in range(2):
        tool(con, "Bash", min=30 + i, dur_ms=400_000)
    assert sinais(con)["tool_lenta"]["severidade"] == "media"


# ---------- sessão cara ----------

def sessoes_do_projeto(con, custos, projeto="proj"):
    for i, custo in enumerate(custos):
        chamada(con, sessao=f"p{i:02d}", min=i, projeto=projeto, custo=custo)


def test_sessao_cara_acima_do_p90_do_projeto(con):
    sessoes_do_projeto(con, [1.0] * 10 + [5.0])              # 11 sessões: o p90 é 1,0
    r = sinais(con, "p10")["sessao_cara"]
    assert r["severidade"] == "media" and r["impacto_usd"] == pytest.approx(4.0)
    assert r["dados"]["sessoes_no_projeto"] == 11
    assert "sessao_cara" not in sinais(con, "p00")


def test_p90_so_vale_com_sessoes_suficientes_no_projeto(con):
    sessoes_do_projeto(con, [1.0] * 8 + [5.0])               # 9 sessões: abaixo do mínimo de 10
    assert "sessao_cara" not in sinais(con, "p08")


def test_custo_absoluto_alto_e_severidade_alta_mesmo_sem_p90(con):
    chamada(con, sessao="solo", custo=12.0)
    r = sinais(con, "solo")["sessao_cara"]
    assert r["severidade"] == "alta" and r["impacto_usd"] is None
    assert "acima de US$ 10.00" in r["evidencia"]


# ---------- pressão de contexto ----------

def test_pressao_de_contexto_so_com_janelas_configuradas(con):
    chamada(con, inp=50, w5=1000, cr=98_950)                 # 100k tokens
    assert "pressao_contexto" not in sinais(con)             # sem janelas: desligado, nada é presumido
    lim = {**LIM, "janelas_contexto": {"claude-opus": 120_000}}
    assert sinais(con, limites=lim)["pressao_contexto"]["severidade"] == "media"       # 83%
    lim["janelas_contexto"] = {"claude-opus": 100_000}
    assert sinais(con, limites=lim)["pressao_contexto"]["severidade"] == "alta"        # 100%
    lim["janelas_contexto"] = {"claude-opus": 1_000_000}
    assert "pressao_contexto" not in sinais(con, limites=lim)
    lim["janelas_contexto"] = {"gpt": 1000}
    assert "pressao_contexto" not in sinais(con, limites=lim)                          # modelo sem janela: ignorado


def test_janela_de_prefixo_mais_longo_vence(con):
    chamada(con, modelo="claude-opus-5-5", inp=90_000)
    lim = {**LIM, "janelas_contexto": {"claude": 1_000_000, "claude-opus": 100_000}}
    assert sinais(con, limites=lim)["pressao_contexto"]["dados"]["janela"] == 100_000


# ---------- ordem ----------

def test_ordenacao_por_severidade_depois_impacto_depois_custo(con):
    chamada(con, sessao="a", custo=12.0)                     # alta (sessão cara)
    for i in range(3):
        tool(con, "Bash", sessao="b", min=i, erro=1)         # média (loop)
    chamada(con, sessao="c", min=0)
    chamada(con, sessao="c", min=10, w5=20000)               # baixa (re-criação), com impacto
    chamada(con, sessao="d", min=0)
    for i in range(3):
        tool(con, "Read", sessao="d", min=i, alvo="x")       # baixa (leitura), sem impacto
    recs = dg.diagnosticar(con, LIM, ["a", "b", "c", "d"])
    assert [(r["severidade"], r["sinal"]) for r in recs] == [
        ("alta", "sessao_cara"), ("media", "loop_retry"), ("baixa", "recriacao_cache"), ("baixa", "leitura_repetida")]


def test_sessao_sem_sinais_nao_gera_recomendacao(con):
    chamada(con)
    tool(con, "Read", dur_ms=100, alvo="x")
    assert dg.diagnosticar(con, LIM, ["s1"]) == []


# ---------- config.json ----------

def test_limites_padrao_sem_arquivo(tmp_path):
    lim, avisos = dg.carregar_limites(tmp_path / "nao-existe.json")
    assert lim == dg.LIMITES_PADRAO and avisos == []


def test_config_troca_limites_e_ignora_o_invalido_com_aviso(tmp_path):
    arq = tmp_path / "config.json"
    arq.write_text(json.dumps({"diagnostico": {
        "cache_taxa_min": 0.9, "loop_min": 4, "janelas_contexto": {"claude-opus": 200000},
        "pausa_s": "muito", "leitura_min": True, "limite_inventado": 1, "tools_que_esperam": ["Bash"],
        "custo_alto_usd": -5}}), encoding="utf-8")
    lim, avisos = dg.carregar_limites(arq)
    assert (lim["cache_taxa_min"], lim["loop_min"]) == (0.9, 4)
    assert lim["janelas_contexto"] == {"claude-opus": 200000} and lim["tools_que_esperam"] == ["Bash"]
    assert lim["pausa_s"] == 300 and lim["leitura_min"] == 3 and lim["custo_alto_usd"] == 10.0   # inválidos: padrão
    assert len(avisos) == 4 and any("desconhecido 'limite_inventado'" in a for a in avisos)
    assert "limite_inventado" not in lim                     # chave inventada nunca entra nos limites
    lim["janelas_contexto"]["x"] = 1                        # o padrão não pode ser alterado por quem recebeu a cópia
    assert dg.LIMITES_PADRAO["janelas_contexto"] == {}


@pytest.mark.parametrize("texto", ["{nao é json", "[]", '{"diagnostico": 3}'])
def test_config_ilegivel_cai_nos_padroes(tmp_path, texto):
    arq = tmp_path / "config.json"
    arq.write_text(texto, encoding="utf-8")
    lim, avisos = dg.carregar_limites(arq)
    assert lim == dg.LIMITES_PADRAO and len(avisos) == 1


def test_percentil_pelo_metodo_do_agenttrace():
    assert dg.percentil([], 0.95) is None
    assert dg.percentil([5], 0.95) == 5
    assert dg.percentil(list(range(1, 11)), 0.90) == 9       # ceil(10 * 0.9) - 1 = índice 8
    assert dg.percentil(list(range(1, 22)), 0.95) == 20      # ceil(21 * 0.95) - 1 = índice 19


# ---------- CLI ----------

@pytest.fixture
def banco(tmp_path):
    caminho = tmp_path / "cli.db"
    c = db.conectar(caminho)
    chamada(c, sessao="abc12345-aaaa", custo=12.0)
    for i in range(3):
        tool(c, "Bash", sessao="abc12345-aaaa", min=i, erro=1)
        evento(c, sessao="abc12345-aaaa", min=i)
    chamada(c, sessao="abd99999-bbbb", min=100)
    evento(c, sessao="abd99999-bbbb", min=100)
    c.commit()
    c.close()
    return str(caminho)


def test_cli_tabela(banco, capsys):
    main(["--db", banco, "diagnostico"])
    saida = capsys.readouterr().out
    assert "[alta] abc12345 · proj · sessão cara" in saida
    assert "evidência: custo US$ 12.00" in saida and "→ " in saida
    assert "[media] abc12345 · proj · loop de retry" in saida
    assert "2 de 2 recomendação(ões), em 1 sessão(ões)" in saida
    assert "Pressão de contexto: não avaliada" in saida and "propostas iniciais" in saida


def test_cli_json_e_limite(banco, capsys):
    main(["--db", banco, "diagnostico", "--json"])
    recs = json.loads(capsys.readouterr().out)
    assert [r["sinal"] for r in recs] == ["sessao_cara", "loop_retry"]
    assert {"sessao", "projeto", "sinal", "severidade", "evidencia", "dados", "impacto_usd", "acao"} <= set(recs[0])
    main(["--db", banco, "diagnostico", "--json", "--limite", "1"])
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_cli_sessao_por_prefixo(banco, capsys):
    main(["--db", banco, "diagnostico", "--sessao", "abc1", "--json"])
    assert {r["sessao"] for r in json.loads(capsys.readouterr().out)} == {"abc12345-aaaa"}
    main(["--db", banco, "diagnostico", "--sessao", "abd9"])
    assert "Nenhum sinal acima dos limites em 1 sessão(ões)." in capsys.readouterr().out
    with pytest.raises(SystemExit, match="não encontrada"):
        main(["--db", banco, "diagnostico", "--sessao", "zzz"])
    with pytest.raises(SystemExit, match="ambígua"):
        main(["--db", banco, "diagnostico", "--sessao", "ab"])


def test_cli_le_o_config_ao_lado_do_banco(banco, tmp_path, capsys):
    (tmp_path / "config.json").write_text(
        json.dumps({"diagnostico": {"custo_alto_usd": 100, "loop_min": 4, "inventado": 1}}), encoding="utf-8")
    main(["--db", banco, "diagnostico", "--json"])
    capturado = capsys.readouterr()
    assert json.loads(capturado.out) == []                   # com os limites mais altos, nada dispara
    assert "limite desconhecido 'inventado'" in capturado.err


def test_cli_filtros_de_periodo_e_projeto(banco, capsys):
    main(["--db", banco, "diagnostico", "--projeto", "outro", "--json"])
    assert json.loads(capsys.readouterr().out) == []
