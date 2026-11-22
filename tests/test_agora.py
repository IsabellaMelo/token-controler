"""T20: `tc live`, a tela "agora". Fixture sintética em tests/fixtures/claude_code_agora (relógio fixo: 2026-11-21 13:00 UTC).

  alfa   ativa há 1 min,  branch feature/relatorio, 3 falhas seguidas de Bash às 12:50 (alerta recente)
  beta   ativa há 6 min,  sem branch no log, 1 chamada + 1 erro de API, menos de 1 min de tempo ativo
  gama   ativa há 14 min, branch feature/x e depois main, cache nunca lido (cache baixo), pausa de 22 min às 11:28
         e re-criação de cache às 11:50 (mais de 60 min atrás: não alertam)
  delta  parada há 40 min: só entra no custo de hoje
"""

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from token_controler import agora, cli, db, diagnostico, painel
from token_controler.fontes import claude_code

LOGS = Path(__file__).parent / "fixtures" / "claude_code_agora" / "projects"
MOMENTO = datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)
LIM = diagnostico.carregar_limites(None)[0]


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "a.db")
    claude_code.ingerir(c, pasta=LOGS)
    yield c
    c.close()


@pytest.fixture
def dados(con):
    return agora.coletar(con, MOMENTO, LIM)


def por_projeto(dados):
    return {s["projeto"]: s for s in dados["sessoes"]}


def hora_local(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")


# ---------- sessões ativas ----------

def test_tres_sessoes_simultaneas_e_a_parada_fica_de_fora(dados):
    assert [s["projeto"] for s in dados["sessoes"]] == ["alfa", "beta", "gama"]      # da atividade mais recente à mais antiga
    assert [s["atras_s"] for s in dados["sessoes"]] == [60, 360, 840]
    assert "delta" not in por_projeto(dados)


def test_janela_de_sessao_ativa_e_um_limite(con):
    assert [s["projeto"] for s in agora.coletar(con, MOMENTO, {**LIM, "live_sessao_ativa_min": 5})["sessoes"]] == ["alfa"]
    assert [s["projeto"] for s in agora.coletar(con, MOMENTO, {**LIM, "live_sessao_ativa_min": 60})["sessoes"]] == \
           ["alfa", "beta", "gama", "delta"]
    assert agora.coletar(con, MOMENTO, {**LIM, "live_sessao_ativa_min": 0})["sessoes"] == []


def test_o_limite_da_janela_pode_vir_do_config(tmp_path):
    (tmp_path / "config.json").write_text('{"diagnostico": {"live_sessao_ativa_min": 30, "live_alerta_janela_min": 120}}',
                                          encoding="utf-8")
    limites, avisos = diagnostico.carregar_limites(tmp_path / "config.json")
    assert (limites["live_sessao_ativa_min"], limites["live_alerta_janela_min"], avisos) == (30, 120, [])


def test_branch_vem_do_log_e_sem_branch_mostra_a_hora_de_inicio(dados):
    s = por_projeto(dados)
    assert s["alfa"]["branch"] == "feature/relatorio"
    assert s["gama"]["branch"] == "main"                              # trocou de feature/x para main: vale o último
    assert s["beta"]["branch"] is None
    assert agora.coluna_branch(s["alfa"]) == "feature/relatorio"
    assert agora.coluna_branch(s["beta"]) == "desde " + hora_local("2026-11-21T12:30:00Z")


def test_o_branch_nunca_vai_para_o_banco(con, tmp_path):
    con.commit()
    conteudo = (tmp_path / "a.db").read_bytes()
    for texto in (b"feature/relatorio", b"feature/x", b"hotfix/x", b"main", b"SEGREDO"):
        assert texto not in conteudo, texto


def test_sem_leitor_de_branch_nao_le_log_nenhum(con):
    d = agora.coletar(con, MOMENTO, LIM, ler_branch=None)
    assert all(s["branch"] is None for s in d["sessoes"])


def test_erros_de_tool_e_de_api(dados):
    s = por_projeto(dados)
    assert (s["alfa"]["erros_tool"], s["alfa"]["erros_api"]) == (3, 0)
    assert (s["beta"]["erros_tool"], s["beta"]["erros_api"]) == (0, 1)
    assert (s["gama"]["erros_tool"], s["gama"]["erros_api"]) == (0, 0)
    assert [agora.erros(s[p]) for p in ("alfa", "beta", "gama")] == ["3", "0 +1 API", "0"]


def test_custo_e_custo_por_hora_do_tempo_ativo(dados, con):
    s = por_projeto(dados)
    for projeto, sessao in (("alfa", "sess-aaaa-0001"), ("beta", "sess-bbbb-0002"), ("gama", "sess-cccc-0003")):
        esperado = con.execute("SELECT SUM(custo_usd) FROM chamadas WHERE sessao_id = ?", (sessao,)).fetchone()[0]
        assert s[projeto]["custo_usd"] == pytest.approx(esperado)
    assert s["alfa"]["custo_hora_usd"] == pytest.approx(s["alfa"]["custo_usd"] / (s["alfa"]["ativo_s"] / 3600))
    assert s["gama"]["custo_hora_usd"] == pytest.approx(s["gama"]["custo_usd"] / (s["gama"]["ativo_s"] / 3600))
    # as pausas longas não contam como tempo ativo: gama ficou ~1h45 aberta, mas só ~30 min ativa
    assert s["gama"]["ativo_s"] < 40 * 60
    assert s["beta"]["ativo_s"] < 60 and s["beta"]["custo_hora_usd"] is None      # pouco tempo ativo: não extrapola


def test_ultima_atividade_em_texto():
    assert agora.atras(3) == "agora" and agora.atras(40) == "há 40s" and agora.atras(130) == "há 2min10s"


# ---------- alertas ----------

def test_alertas_vem_do_tc_sessao_e_trazem_o_comando(dados):
    por_tipo = {a["tipo"]: a for a in dados["alertas"]}
    assert set(por_tipo) == {"cache_baixo", "falha_repetida"}
    assert por_tipo["cache_baixo"]["projeto"] == "gama" and por_tipo["cache_baixo"]["comando"] == "tc sessao sess-ccc"
    assert por_tipo["falha_repetida"]["projeto"] == "alfa" and "Bash falhou 3 vezes seguidas" in por_tipo["falha_repetida"]["motivo"]
    assert por_tipo["falha_repetida"]["comando"] == "tc sessao sess-aaa"
    assert dados["alertas_ocultos"] == 0


def test_alertas_batem_com_o_que_o_tc_sessao_mostra(dados, con):
    from token_controler import detalhe
    sessao = detalhe.analisar(con, "sess-cccc-0003", LIM)
    assert any(p["tipo"] == "cache_baixo" and p["motivo"] == next(a for a in dados["alertas"] if a["tipo"] == "cache_baixo")["motivo"]
               for p in sessao["problemas"])


def test_alertas_ordenados_por_gravidade(dados):
    ordem = {"alta": 0, "media": 1, "baixa": 2}
    sev = [ordem[a["severidade"]] for a in dados["alertas"]]
    assert sev == sorted(sev) and dados["alertas"][0]["tipo"] == "cache_baixo"        # 25% de cache: alta


def test_evento_antigo_nao_alerta_mas_o_estado_da_sessao_sim(con):
    """A pausa de 22 min e as re-criações de gama são de mais de 60 min atrás; o cache baixo é estado e sempre vale."""
    tipos = {a["tipo"] for a in agora.coletar(con, MOMENTO, LIM)["alertas"]}
    assert "pausa_longa" not in tipos and "recriacao_cache" not in tipos
    amplo = agora.coletar(con, MOMENTO, {**LIM, "live_alerta_janela_min": 600})
    assert {"pausa_longa", "recriacao_cache"} <= {a["tipo"] for a in amplo["alertas"]}
    curto = agora.coletar(con, MOMENTO, {**LIM, "live_alerta_janela_min": 5})
    assert {a["tipo"] for a in curto["alertas"]} == {"cache_baixo"}                   # a falha de alfa (12:50) já passou de 5 min


def test_so_alerta_de_sessao_ativa(con):
    d = agora.coletar(con, MOMENTO, {**LIM, "live_sessao_ativa_min": 5})              # só alfa ativa
    assert {a["projeto"] for a in d["alertas"]} == {"alfa"}


def test_limite_de_alertas_mostra_quantos_ficaram_de_fora(con, monkeypatch):
    monkeypatch.setattr(agora, "MAX_ALERTAS", 1)
    d = agora.coletar(con, MOMENTO, LIM)
    assert len(d["alertas"]) == 1 and d["alertas_ocultos"] == 1 and d["alertas"][0]["tipo"] == "cache_baixo"


def test_cache_reaproveita_o_calculo_pesado_ate_o_banco_mudar(con, monkeypatch):
    from token_controler import detalhe
    chamadas = []
    original = detalhe.analisar
    monkeypatch.setattr(detalhe, "analisar", lambda *a, **k: chamadas.append(1) or original(*a, **k))
    cache = {}
    agora.coletar(con, MOMENTO, LIM, cache=cache)
    n = len(chamadas)
    assert n == 3                                                                     # uma análise por sessão ativa
    agora.coletar(con, MOMENTO, LIM, cache=cache)
    assert len(chamadas) == n                                                         # nada mudou: reaproveita
    con.execute("""INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo)
                   VALUES ('claude_code', 'sess-aaaa-0001', 'alfa', '2026-11-21T12:59:30.000Z', 'atividade', 'f')""")
    agora.coletar(con, MOMENTO, LIM, cache=cache)
    assert len(chamadas) == 2 * n                                                     # o banco mudou: refaz


# ---------- hoje ----------

def esperado_de_hoje(con):
    dia = MOMENTO.astimezone().date()
    projetos, horas = {}, [0.0] * 24
    for r in con.execute("SELECT ts, projeto, custo_usd FROM chamadas WHERE custo_usd IS NOT NULL"):
        t = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone()
        if t.date() == dia:
            projetos[r["projeto"]] = projetos.get(r["projeto"], 0) + r["custo_usd"]
            horas[t.hour] += r["custo_usd"]
    return projetos, horas


def test_custo_de_hoje_por_projeto_inclui_a_sessao_parada(dados, con):
    projetos, _ = esperado_de_hoje(con)
    assert {p["projeto"]: p["custo_usd"] for p in dados["projetos"]} == pytest.approx(projetos)
    assert "delta" in {p["projeto"] for p in dados["projetos"]}
    custos = [p["custo_usd"] for p in dados["projetos"]]
    assert custos == sorted(custos, reverse=True)                                     # o mais caro primeiro
    assert dados["custo_hoje_usd"] == pytest.approx(sum(projetos.values()))
    assert [p["sessoes"] for p in dados["projetos"]] == [1, 1, 1, 1]


def test_custo_por_hora_do_dia(dados, con):
    _, horas = esperado_de_hoje(con)
    assert dados["horas"] == pytest.approx(horas) and len(dados["horas"]) == 24
    assert dados["hora_de_pico"] == max(range(24), key=lambda h: horas[h])
    assert sum(dados["horas"]) == pytest.approx(dados["custo_hoje_usd"])
    assert dados["hora_atual"] == MOMENTO.astimezone().hour


def test_mini_grafico_proporcional_a_24_colunas():
    horas = [0.0] * 24
    horas[8], horas[9], horas[10] = 1.0, 4.0, 8.0
    g = agora.mini_grafico(horas)
    assert len(g) == 24 and g.count("·") == 21
    assert g[8] < g[9] < g[10] and g[10] == "█" and g[8] == "▁"                       # blocos mais altos para custos maiores
    assert agora.mini_grafico([0.0] * 24) == "·" * 24 and len(agora.eixo_do_grafico()) == 24


def test_filtro_de_projeto(con):
    d = agora.coletar(con, MOMENTO, LIM, projeto="alfa")
    assert [s["projeto"] for s in d["sessoes"]] == ["alfa"] and [p["projeto"] for p in d["projetos"]] == ["alfa"]
    assert {a["projeto"] for a in d["alertas"]} == {"alfa"}
    assert agora.coletar(con, MOMENTO, LIM, projeto="nao-existe")["sessoes"] == []


# ---------- blocos sem dados somem ----------

def test_banco_vazio_so_diz_que_nao_ha_nada(tmp_path):
    c = db.conectar(tmp_path / "vazio.db")
    d = agora.coletar(c, MOMENTO, LIM)
    texto = agora.em_texto(d)
    assert "Nenhuma sessão ativa nos últimos 15 min." in texto
    for bloco in ("Alertas", "Hoje por projeto", "Custo por hora do dia", "Sessões ativas"):
        assert bloco not in texto
    c.close()


def test_sem_sessao_ativa_o_que_sobra_e_o_custo_de_hoje(con):
    d = agora.coletar(con, datetime(2026, 11, 21, 14, 30, tzinfo=timezone.utc), LIM)      # 1h30 depois: ninguém ativo
    assert d["sessoes"] == [] and d["alertas"] == []
    texto = agora.em_texto(d)
    assert "Nenhuma sessão ativa" in texto and "Alertas" not in texto
    assert "Hoje por projeto" in texto and "Custo por hora do dia" in texto


def test_sem_alerta_o_bloco_de_alertas_some(con):
    texto = agora.em_texto(agora.coletar(con, MOMENTO, LIM, projeto="beta"))
    assert "Sessões ativas" in texto and "Alertas" not in texto


def test_texto_traz_os_quatro_blocos_e_o_comando(dados):
    texto = agora.em_texto(dados)
    for esperado in ("Sessões ativas (últimos 15 min)", "feature/relatorio", "desde ", "há 1min00s", "$/h", "0 +1 API", "Alertas",
                     "tc sessao sess-ccc", "tc sessao sess-aaa", "Hoje por projeto", "Custo por hora do dia", "pico às"):
        assert esperado in texto, esperado
    assert "SEGREDO" not in texto and "pytest" not in texto                          # nada de conversa nem de comando


# ---------- rich ----------

def render(dados, largura=120):
    buf = io.StringIO()
    Console(file=buf, width=largura, force_terminal=False, color_system=None).print(painel.renderizar_agora(dados, largura))
    return buf.getvalue()


def test_rich_desenha_os_quatro_blocos(dados):
    saida = render(dados)
    for esperado in ("agora", "hoje $", "3 sessão(ões) ativa(s)", "Sessões ativas (últimos 15 min)", "feature/relatorio",
                     "Alertas (2)", "tc sessao sess-ccc", "Hoje por projeto", "Custo por hora do dia", "pico às"):
        assert esperado in saida, esperado


def test_rich_em_tela_estreita_empilha_os_blocos_de_hoje(dados):
    largo, estreito = render(dados, 140), render(dados, 70)
    linha_projetos = next(l for l in largo.splitlines() if "Hoje por projeto" in l)
    assert "Custo por hora do dia" in linha_projetos                                  # lado a lado
    linha_estreita = next(l for l in estreito.splitlines() if "Hoje por projeto" in l)
    assert "Custo por hora do dia" not in linha_estreita and "Custo por hora do dia" in estreito


def test_rich_esconde_blocos_sem_dados(con):
    saida = render(agora.coletar(con, datetime(2026, 11, 21, 14, 30, tzinfo=timezone.utc), LIM))
    assert "Nenhuma sessão ativa" in saida and "Sessões ativas" not in saida and "Alertas" not in saida
    assert "Hoje por projeto" in saida


def test_rich_nao_interpreta_colchetes_do_branch(con):
    d = agora.coletar(con, MOMENTO, LIM, ler_branch=lambda arquivo: "feat/[red]x[/red]")
    assert "feat/[red]x[/red]" in render(d)


def test_rich_nao_exibe_texto_de_conversa(dados):
    saida = render(dados)
    assert "SEGREDO" not in saida and "pytest" not in saida


# ---------- laço e comando ----------

@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(LOGS.parent))
    monkeypatch.setattr(agora, "agora_utc", lambda: MOMENTO)
    return str(tmp_path / "live.db")


def teclas(monkeypatch, sequencia):
    restantes = list(sequencia)
    monkeypatch.setattr(painel, "esperar_tecla", lambda timeout: restantes.pop(0))
    return restantes


def test_laco_atualiza_com_r_e_sai_com_q(ambiente, monkeypatch):
    restantes = teclas(monkeypatch, [None, "r", "Q"])
    buf = io.StringIO()
    c = db.conectar(Path(ambiente))
    painel.executar_agora(c, LIM, {}, intervalo=1, console=Console(file=buf, width=120, force_terminal=False))
    c.close()
    assert buf.getvalue().count("Sessões ativas") == 3 and restantes == []


@pytest.mark.parametrize("tecla", ["q", "\x03", "\x1b"])
def test_laco_sai_com_q_ctrl_c_ou_esc(ambiente, monkeypatch, tecla):
    teclas(monkeypatch, [tecla])
    buf = io.StringIO()
    c = db.conectar(Path(ambiente))
    painel.executar_agora(c, LIM, {}, intervalo=1, console=Console(file=buf, width=120, force_terminal=False))
    c.close()
    assert buf.getvalue().count("Sessões ativas") == 1


def test_laco_le_os_logs_a_cada_quadro(ambiente, monkeypatch):
    teclas(monkeypatch, [None, "q"])
    lidos = []
    original = claude_code.ingerir
    monkeypatch.setattr(painel.claude_code, "ingerir", lambda *a, **k: lidos.append(1) or original(*a, **k))
    c = db.conectar(Path(ambiente))
    painel.executar_agora(c, LIM, {}, intervalo=1, console=Console(file=io.StringIO(), width=120, force_terminal=False))
    c.close()
    assert len(lidos) == 2


def test_cli_live_padrao_usa_rich_e_mostra_as_tres_sessoes(ambiente, monkeypatch, capsys):
    teclas(monkeypatch, ["q"])
    cli.main(["--db", ambiente, "live", "--intervalo", "1"])
    saida = capsys.readouterr().out
    for esperado in ("alfa", "beta", "gama", "feature/relatorio", "Alertas", "tc sessao sess-ccc", "Hoje por projeto"):
        assert esperado in saida, esperado
    assert "delta" in saida                                                           # na lista de hoje, não nas sessões ativas


def test_cli_live_filtra_por_projeto(ambiente, monkeypatch, capsys):
    teclas(monkeypatch, ["q"])
    cli.main(["--db", ambiente, "live", "--projeto", "gama"])
    saida = capsys.readouterr().out
    assert "gama" in saida and "alfa" not in saida and "beta" not in saida


def test_cli_live_sai_limpo_com_ctrl_c(ambiente, monkeypatch, capsys):
    def interrompe(timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(painel, "esperar_tecla", interrompe)
    cli.main(["--db", ambiente, "live"])                                              # não pode levantar
    assert "Sessões ativas" in capsys.readouterr().out


def rodar_simples(ambiente, monkeypatch, capsys, quadros, *extra):
    chamadas = []

    def dormir(segundos):
        chamadas.append(segundos)
        if len(chamadas) >= quadros:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", dormir)
    cli.main(["--db", ambiente, "live", "--simples", "--intervalo", "2", *extra])
    assert set(chamadas) == {2}
    return capsys.readouterr().out


def test_cli_live_simples_e_texto_puro_que_redesenha(ambiente, monkeypatch, capsys):
    saida = rodar_simples(ambiente, monkeypatch, capsys, 2)
    assert saida.count("\033[2J\033[H") == 2                                          # limpa a tela a cada quadro
    assert "Sessões ativas" in saida and "tc sessao sess-aaa" in saida and "├" not in saida
    assert "SEGREDO" not in saida


def test_cli_live_simples_respeita_o_filtro(ambiente, monkeypatch, capsys):
    saida = rodar_simples(ambiente, monkeypatch, capsys, 1, "--projeto", "beta")
    assert "beta" in saida and "alfa" not in saida and "Alertas" not in saida


def test_cli_live_sem_rich_cai_no_texto_simples(ambiente, monkeypatch, capsys):
    import token_controler
    monkeypatch.delattr(token_controler, "painel")
    monkeypatch.setitem(sys.modules, "token_controler.painel", None)                  # agora o import falha, como sem rich
    monkeypatch.setattr(cli.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    cli.main(["--db", ambiente, "live"])
    capturado = capsys.readouterr()
    assert "rich não está instalado" in capturado.err
    assert "Sessões ativas" in capturado.out and "tc sessao sess-ccc" in capturado.out


def test_o_log_que_cresce_aparece_no_quadro_seguinte(ambiente, monkeypatch, capsys, tmp_path):
    """Sessão nova entre o 1º e o 2º quadro: o laço relê os logs e a mostra."""
    logs = tmp_path / "logs"
    projeto = logs / "projects" / "novo"
    projeto.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    log = projeto / "s.jsonl"
    linha = ('{"type": "assistant", "sessionId": "sess-nova", "cwd": "C:\\\\x\\\\novoproj", "timestamp": "2026-11-21T12:58:00.000Z", '
             '"requestId": "r1", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 1000, '
             '"output_tokens": 1000}, "content": []}}\n')
    passos = []

    def dormir(_):
        passos.append(1)
        if len(passos) == 1:
            log.write_text(linha, encoding="utf-8")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", dormir)
    cli.main(["--db", ambiente, "live", "--simples", "--intervalo", "1"])
    primeiro, segundo = [q for q in capsys.readouterr().out.split("\033[2J\033[H") if q.strip()]
    assert "novoproj" not in primeiro and "novoproj" in segundo


# ---------- o branch lido do log ----------

def escrever(tmp_path, linhas, nome="s.jsonl"):
    arquivo = tmp_path / nome
    arquivo.write_text("".join(l if l.endswith("\n") else l + "\n" for l in linhas), encoding="utf-8")
    return arquivo


def test_branch_do_log_usa_a_ultima_linha_que_traz_o_campo(tmp_path):
    f = escrever(tmp_path, ['{"gitBranch": "a"}', '{"type": "user"}', '{"gitBranch": "b"}', '{"type": "assistant"}'])
    assert claude_code.branch_do_log(f) == "b"


@pytest.mark.parametrize("linhas", [[], ['{"type": "user"}'], ['{"gitBranch": ""}'], ['{"gitBranch": 5}'],
                                    ['{"gitBranch": null}'], ["não é json gitBranch"], ['["gitBranch"]']])
def test_branch_do_log_sem_branch_valido_devolve_none(tmp_path, linhas):
    assert claude_code.branch_do_log(escrever(tmp_path, linhas)) is None


def test_branch_do_log_que_nao_existe_devolve_none(tmp_path):
    assert claude_code.branch_do_log(tmp_path / "nao-existe.jsonl") is None


def test_branch_do_log_so_olha_o_fim_do_arquivo(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "BYTES_FINAIS", 400)
    enchimento = ['{"type": "user", "x": "' + "z" * 100 + '"}'] * 20
    f = escrever(tmp_path, ['{"gitBranch": "antigo"}', *enchimento, '{"gitBranch": "recente"}', *enchimento[:2]])
    assert claude_code.branch_do_log(f) == "recente"
    g = escrever(tmp_path, ['{"gitBranch": "so-no-comeco"}', *enchimento], "g.jsonl")
    assert claude_code.branch_do_log(g) is None                                       # fora da janela do fim: não procura mais


def test_branch_do_log_com_a_primeira_linha_cortada_nao_quebra(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "BYTES_FINAIS", 60)
    f = escrever(tmp_path, ['{"type": "user", "texto": "' + "q" * 200 + '"}', '{"gitBranch": "ok"}'])
    assert claude_code.branch_do_log(f) == "ok"


def test_branch_do_log_limpa_controle_e_corta_o_tamanho(tmp_path):
    f = escrever(tmp_path, [json.dumps({"gitBranch": "feat/\x00\x1bnome\n"})])
    assert claude_code.branch_do_log(f) == "feat/nome"
    longo = claude_code.branch_do_log(escrever(tmp_path, [json.dumps({"gitBranch": "r" * 200})], "l.jsonl"))
    assert len(longo) == claude_code.BRANCH_MAX and longo.endswith("…")
