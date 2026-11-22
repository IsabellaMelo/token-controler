"""`tc live --simples`: o painel antigo (uma tabela de hoje). Sai com Ctrl+C (KeyboardInterrupt), sem erro.

O painel novo, com rich, é testado em tests/test_painel.py."""

from datetime import date
from pathlib import Path

import pytest

from token_controler import cli

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code" / "projects"


class HojeFixo(date):
    """Congela o 'hoje' no dia dos logs sintéticos (2026-11-21), para o teste não depender do relógio."""

    @classmethod
    def today(cls):
        return date(2026, 11, 21)


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    monkeypatch.setattr(cli, "date", HojeFixo)
    return str(tmp_path / "live.db")


def rodar_quadros(banco, monkeypatch, capsys, quadros, *extra):
    """Roda o live até o `quadros`-ésimo sleep e simula o Ctrl+C. Devolve (saída, número de sleeps)."""
    chamadas = []

    def dormir(segundos):
        chamadas.append(segundos)
        if len(chamadas) >= quadros:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", dormir)
    cli.main(["--db", banco, "live", "--simples", "--intervalo", "3", *extra])  # Ctrl+C é a saída normal: não levanta
    assert set(chamadas) == {3}                                    # dorme o intervalo pedido
    return capsys.readouterr().out, len(chamadas)


def test_live_desenha_o_painel_de_hoje_e_sai_com_ctrl_c(live, monkeypatch, capsys):
    saida, _ = rodar_quadros(live, monkeypatch, capsys, 1)
    assert "\033[2J\033[H" in saida                                # limpa a tela antes de desenhar
    assert "token-controler · hoje · atualizado" in saida and "(a cada 3s, Ctrl+C para sair)" in saida
    assert "rpps" in saida and "$0.12" in saida                    # lê os logs sozinho e mostra o dia
    assert "clin11" not in saida                                   # sessão de 20/11: fora de "hoje"
    assert "Custo estimado hoje: $0.12" in saida


def test_live_redesenha_a_cada_intervalo(live, monkeypatch, capsys):
    saida, n = rodar_quadros(live, monkeypatch, capsys, 3)
    assert n == 3 and saida.count("\033[2J") == 3


def test_live_pega_o_que_aparece_depois_do_primeiro_quadro(live, monkeypatch, capsys, tmp_path):
    novo = tmp_path / "logs" / "projects" / "novo"
    novo.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "logs"))
    log = novo / "s.jsonl"
    linha = ('{"type": "assistant", "sessionId": "sess-live", "cwd": "C:\\\\x\\\\projlive", "timestamp": "%s", '
             '"requestId": "r%d", "message": {"id": "m%d", "model": "claude-opus-5-5", '
             '"usage": {"input_tokens": 1000, "output_tokens": 1000}, "content": []}}')
    log.write_text(linha % ("2026-11-21T12:00:00Z", 1, 1) + "\n", encoding="utf-8")
    passos = []

    def dormir(_):
        passos.append(1)
        if len(passos) == 1:                                       # entre o 1º e o 2º quadro, o log cresce
            with log.open("a", encoding="utf-8") as f:
                f.write(linha % ("2026-11-21T12:00:05Z", 2, 2) + "\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", dormir)
    cli.main(["--db", live, "live", "--simples", "--intervalo", "1"])
    primeiro, segundo = [q for q in capsys.readouterr().out.split("\033[2J\033[H") if q.strip()]
    assert "Custo estimado hoje: $0.02" in primeiro                # 1 chamada de US$ 0,024
    assert "Custo estimado hoje: $0.05" in segundo                 # 2 chamadas: o live releu o log que cresceu


def test_live_respeita_o_filtro_de_projeto_na_tabela_e_no_rodape(live, monkeypatch, capsys):
    saida, _ = rodar_quadros(live, monkeypatch, capsys, 1, "--projeto", "nao-existe")
    assert "(sem dados no período)" in saida and "rpps" not in saida
    assert "Custo estimado hoje: -" in saida                       # o rodapé não pode somar os outros projetos


def test_live_rodape_soma_so_o_projeto_filtrado(live, monkeypatch, capsys):
    saida, _ = rodar_quadros(live, monkeypatch, capsys, 1, "--projeto", "rpps")
    assert "rpps" in saida and "Custo estimado hoje: $0.12" in saida
