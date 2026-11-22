"""Rótulo derivado do prompt (hoje só `debug`). O TEXTO NUNCA SAI DAQUI.

Exceção à regra 1 do AGENTS.md, aprovada pelo dono do projeto: na leitura, o texto de um prompt pode ser
examinado em memória para decidir se ele fala de um erro a corrigir; só o rótulo (`debug` ou None) é gravado.
Por isso este módulo:
  - recebe o texto, devolve só o rótulo e não guarda nada;
  - não imprime, não loga e não inclui o texto em nenhuma mensagem (nem de erro);
  - não é chamado de nenhum outro lugar além do adaptador, no momento da leitura do prompt.

As palavras são propostas iniciais, em português e inglês, comparadas sem acento e sem diferença de caixa.
Inspirado nas palavras de depuração do CodeBurn, reimplementadas.
"""

from __future__ import annotations

import re
import unicodedata

DEBUG = "debug"

_PALAVRAS_DEBUG = re.compile(
    r"\b(?:"
    # português (já sem acento)
    r"erros?|bugs?|bugad[oa]s?|corrig\w*|corrij\w*|conserta\w*|conserte\w*|problemas?|inesperad[oa]s?|"
    r"falh(?:a|as|ou|ando|ar|aram)|quebr(?:ou|ado|ada|ados|adas)|trav(?:ou|ando|ado|ada)|"
    r"nao (?:esta )?funcion(?:a|ando|ou)|parou de funcionar|excecao|excecoes|depur\w*|"
    # inglês
    r"fix(?:es|ed|ing)?|errors?|broken|fail(?:s|ed|ing|ure|ures)?|crash(?:es|ed|ing)?|debug\w*|"
    r"exceptions?|not working|doesn'?t work|wrong|unexpected|stack ?trace|traceback"
    r")\b"
)


def _sem_acento(texto: str) -> str:
    base = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in base if not unicodedata.combining(c))


def rotular_prompt(texto) -> str | None:
    """`debug` se o prompt fala de erro, falha ou correção; senão None. Qualquer valor que não seja texto dá None."""
    if not isinstance(texto, str) or not texto:
        return None
    return DEBUG if _PALAVRAS_DEBUG.search(_sem_acento(texto)) else None
