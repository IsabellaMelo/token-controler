"""Banco de demonstração do `tc web --demo`: dados FICTÍCIOS e determinísticos, parecidos com os dos mockups.

Serve para olhar a tela sem dados reais e para os testes visuais (dois prints da mesma versão saem idênticos). Nada aqui
vem de log nenhum: projetos, sessões, tokens e erros são inventados a partir de uma semente fixa. Os horários são
definidos no fuso local da máquina, para a tela mostrar o mesmo dia e as mesmas horas em qualquer fuso.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .custo import decidir
from .precos import achar_preco

DIA = (2026, 11, 22)
AGORA_LOCAL = datetime(*DIA, 11, 7, 42)          # o "agora" congelado da demonstração (horário local)
FERRAMENTA = "claude_code"


@dataclass(frozen=True)
class Sessao:
    inicio: tuple[int, int]      # hora e minuto locais
    projeto: str
    modelo: str
    duracao_min: int
    ativo_min: int
    pausas: int                  # pausas acima de 5 min (elas fazem a diferença entre duração e tempo ativo)
    chamadas: int
    custo_usd: float             # alvo aproximado; o custo final sai dos tokens x tabela de preços
    cache: float                 # taxa de leitura de cache
    erros: int
    prompts: int


SESSOES = (
    Sessao((7, 52), "token-controler", "claude-opus-5-5", 58, 41, 2, 142, 1.64, 0.93, 6, 5),
    Sessao((9, 1), "analisador-curriculos", "claude-opus-5-5", 47, 30, 3, 96, 1.56, 0.71, 11, 4),
    Sessao((9, 40), "site-psicologa", "claude-sonnet-5-5", 31, 25, 1, 58, 0.92, 0.90, 2, 3),
    Sessao((10, 12), "token-controler", "claude-opus-5-5", 39, 30, 1, 74, 0.67, 0.94, 5, 4),
    Sessao((10, 58), "analisador-curriculos", "claude-haiku-5-5", 9, 9, 0, 12, 0.03, 0.85, 1, 2),
)
# tools e peso de cada uma (parecido com o mockup); a categoria vale para as falhas daquela tool
TOOLS = (("Bash", 30, "teste_falhou"), ("Read", 28, "arquivo_ausente"), ("Edit", 19, "edicao_invalida"),
         ("Grep", 12, "outro"), ("Write", 6, "edicao_invalida"), ("WebSearch", 5, "rede"))
SUBTIPOS = ("teste", "git", "leitura", "build", "outro")


def _utc(local: datetime) -> str:
    """Horário local -> ISO 8601 UTC com milissegundos, como os logs do Claude Code."""
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{local.microsecond // 1000:03d}Z"


def agora() -> datetime:
    """O momento congelado da demonstração, em UTC."""
    return AGORA_LOCAL.astimezone(timezone.utc)


def _horarios(s: Sessao, n: int, sorteio: random.Random) -> list[datetime]:
    """`n` horários dentro da sessão: o tempo ativo dividido em trechos, separados por pausas de mais de 5 min."""
    inicio = datetime(*DIA, *s.inicio)
    pausa_total = (s.duracao_min - s.ativo_min) * 60
    pausas = [pausa_total / s.pausas] * s.pausas if s.pausas else []
    trechos = s.pausas + 1
    ativo = s.ativo_min * 60
    por_trecho = max(1, n // trechos)
    saida, t = [], inicio
    for k in range(trechos):
        quantos = por_trecho if k < trechos - 1 else n - por_trecho * (trechos - 1)
        passo = (ativo / trechos) / max(1, quantos)
        for _ in range(quantos):
            saida.append(t)
            t += timedelta(seconds=passo * sorteio.uniform(0.8, 1.2))
        if k < len(pausas):
            t += timedelta(seconds=pausas[k])
    return saida


def _tokens(s: Sessao, sorteio: random.Random) -> tuple[int, int, int, int]:
    """(input, output, gravação de cache 5 min, leitura de cache) de uma chamada, para chegar perto do custo e da taxa alvo."""
    p_in, p_w5, _, p_read, p_out = achar_preco(s.modelo)
    por_chamada = s.custo_usd / s.chamadas * sorteio.uniform(0.7, 1.3) * 1e6
    saida = round(0.35 * por_chamada / p_out)
    k = (1 - s.cache) / s.cache
    leitura = round(0.65 * por_chamada / (p_read + k * p_w5))
    entrada = 3
    gravacao = max(0, round(leitura * k) - entrada)
    return entrada, saida, gravacao, leitura


def _tool_da_vez(sorteio: random.Random) -> tuple[str, str]:
    nomes = [t[0] for t in TOOLS]
    pesos = [t[1] for t in TOOLS]
    nome = sorteio.choices(nomes, pesos)[0]
    return nome, dict((t[0], t[2]) for t in TOOLS)[nome]


def gerar(caminho: Path) -> Path:
    """Cria (do zero) o banco de demonstração em `caminho` e devolve o caminho."""
    for sufixo in ("", "-wal", "-shm", "-journal"):
        Path(str(caminho) + sufixo).unlink(missing_ok=True)
    con = db.conectar(caminho)
    con.execute("PRAGMA journal_mode=DELETE")    # sem WAL: o banco é só lido depois, por conexões `mode=ro`
    sorteio = random.Random(20261122)
    arquivo_n = 0
    for n_sessao, s in enumerate(SESSOES, 1):
        sessao_id = f"demo{n_sessao:04d}-0000-4000-8000-{n_sessao:012d}"
        arquivo_n += 1
        arquivo = f"demo/sessao-{n_sessao}.jsonl"
        horarios = _horarios(s, s.chamadas, sorteio)
        prompts_em = {round(i * s.chamadas / s.prompts) for i in range(s.prompts)}
        erros_em = set(sorteio.sample(range(s.chamadas), s.erros))
        seq, turno = 0, 0
        for i, quando in enumerate(horarios):
            ts = _utc(quando)
            if i in prompts_em:
                turno += 1
                con.execute("INSERT INTO eventos VALUES (?,?,?,?,?,?,?)",
                            (FERRAMENTA, sessao_id, s.projeto, _utc(quando - timedelta(seconds=2)), "prompt", arquivo, None))
            entrada, saida, gravacao, leitura = _tokens(s, sorteio)
            custo, origem = decidir("auto", s.modelo, entrada, saida, gravacao, 0, leitura, None)
            seq += 1
            chamada_id = f"{FERRAMENTA}:msg_demo_{n_sessao}_{i}:req_{n_sessao}_{i}"
            con.execute("""INSERT INTO chamadas (id, ferramenta, sessao_id, projeto, ts, modelo, input_tokens, output_tokens,
                           cache_write_5m, cache_write_1h, cache_read, custo_usd, subagente, erro_api, arquivo,
                           custo_registrado, origem_custo, turno, seq, subagente_id)
                           VALUES (?,?,?,?,?,?,?,?,?,0,?,?,0,0,?,NULL,?,?,?,NULL)""",
                        (chamada_id, FERRAMENTA, sessao_id, s.projeto, ts, s.modelo, entrada, saida, gravacao, leitura,
                         custo, arquivo, origem, turno, seq))
            con.execute("INSERT INTO eventos VALUES (?,?,?,?,?,?,?)",
                        (FERRAMENTA, sessao_id, s.projeto, ts, "atividade", arquivo, None))
            # 2 a 3 tools por chamada; a falha (quando há) é a primeira tool da chamada
            for k in range(sorteio.choice((2, 3, 3))):
                falhou = k == 0 and i in erros_em
                if falhou:   # as falhas se concentram no shell e na busca na web, como no mockup
                    nome = sorteio.choices(("Bash", "WebSearch", "Edit", "Read"), (12, 5, 2, 2))[0]
                    categoria = dict((t[0], t[2]) for t in TOOLS)[nome]
                else:
                    nome, categoria = _tool_da_vez(sorteio)
                seq += 1
                quando_tool = quando + timedelta(seconds=1 + k)
                con.execute("""INSERT INTO tool_calls (id, ferramenta, sessao_id, projeto, ts, tool, erro, subagente, arquivo,
                               duracao_ms, alvo_hash, subtipo, turno, seq, chamada_id, exit_code, erro_categoria)
                               VALUES (?,?,?,?,?,?,?,0,?,?,NULL,?,?,?,?,?,?)""",
                            (f"{FERRAMENTA}:toolu_demo_{n_sessao}_{i}_{k}", FERRAMENTA, sessao_id, s.projeto,
                             _utc(quando_tool), nome, int(falhou), arquivo, sorteio.randint(80, 4000),
                             sorteio.choice(SUBTIPOS) if nome == "Bash" else None, turno, seq, chamada_id,
                             1 if falhou and nome == "Bash" else None, categoria if falhou else None))
                con.execute("INSERT INTO eventos VALUES (?,?,?,?,?,?,?)",
                            (FERRAMENTA, sessao_id, s.projeto, _utc(quando_tool), "atividade", arquivo, None))
        con.execute("INSERT INTO arquivos VALUES (?,?,?,?,?,?)",
                    (arquivo, FERRAMENTA, 1000 * n_sessao, 0.0, _utc(AGORA_LOCAL - timedelta(seconds=2)), 0))
    con.commit()
    con.close()
    return caminho


if __name__ == "__main__":  # pragma: no cover - atalho manual
    import sys
    print(gerar(Path(sys.argv[1] if len(sys.argv) > 1 else "demo.db")))
