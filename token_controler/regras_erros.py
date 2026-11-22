"""Categoria e código de saída de um erro de tool (T23, design 6.4).

Quando uma tool falha, a mensagem de erro do `tool_result` é examinada AQUI, em memória, por regras fixas (regex, em
português e inglês, para Windows e Unix). Só o rótulo da categoria e o código de saída (um inteiro) vão para o banco.
Este módulo é a única parte do código que lê a mensagem:

  - não grava, não imprime e não loga a mensagem;
  - não a inclui em nenhum erro ou exceção;
  - não a guarda em variável de módulo: tudo aqui é função pura.

Regra 1 do AGENTS.md, quarta exceção (aprovada pelo dono do projeto em 22/11/2026).
As categorias são testadas NA ORDEM de `CATEGORIAS`: a primeira que casar vence.
"""

from __future__ import annotations

import re

# Ordem de teste. `outro` é o que sobra e fica por último.
CATEGORIAS = (
    "recusado_usuario",
    "bloqueado_permissao",
    "mcp_auth",
    "modulo_ausente",
    "comando_ausente",
    "sintaxe_shell",
    "edicao_invalida",
    "arquivo_ausente",
    "permissao",
    "timeout",
    "teste_falhou",
    "rede",
    "outro",
)

# O que o `tc diagnostico` recomenda para cada categoria (T29). `recusado_usuario` não gera recomendação: não é erro.
RECOMENDACOES = {
    "recusado_usuario": None,
    "bloqueado_permissao": "Se o bloqueio não era esperado, ajuste as regras de permissão do projeto (settings.json).",
    "mcp_auth": "Reautentique o servidor MCP antes de começar.",
    "modulo_ausente": "Registre no CLAUDE.md como ativar o ambiente.",
    "comando_ausente": "Registre os comandos do projeto no CLAUDE.md e confira o PATH.",
    "sintaxe_shell": "Diga no CLAUDE.md qual shell o projeto usa, com exemplos.",
    "edicao_invalida": "Leia o arquivo antes de editar e peça trechos únicos e menores.",
    "arquivo_ausente": "Abra a sessão na raiz do projeto e descreva a estrutura no CLAUDE.md.",
    "permissao": "Ajuste a permissão ou fale com a TI.",
    "timeout": "Rode só os testes relevantes.",
    "teste_falhou": "Normal se o código muda entre tentativas; se nada muda, é loop.",
    "rede": "Evite pedir instalações ou downloads.",
    "outro": None,
}

# Valores de `toolDenialKind` que o Claude Code grava na linha do resultado (T21). Vêm de uma lista fixa; o texto não é lido.
NEGACAO_USUARIO = "user-rejected"
NEGACAO_BLOQUEIO = frozenset({"permission-rule", "automode-blocked"})

TOOLS_DE_EDICAO = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
TOOLS_DE_SHELL = frozenset({"Bash", "PowerShell", "Shell"})

_LIMITE = 20000  # só o começo da mensagem importa; limita o custo das regex


def _rx(padrao: str, ignorar_caixa: bool = True) -> "re.Pattern[str]":
    return re.compile(padrao, re.IGNORECASE if ignorar_caixa else 0)


_RECUSA_TEXTO = _rx(r"doesn't want to proceed with this tool use|tool use was rejected|"
                    r"user rejected|\[Request interrupted by user for tool use\]|"
                    r"usu[aá]rio (recusou|rejeitou|n[aã]o quis)")
_MCP_AUTH = _rx(r"\b40[13]\b|unauthori[sz]ed|not authenticated|needs? (to )?(re-?)?authenticat|"
                r"authentication (failed|required|error)|(access |auth |oauth )?token (has )?(expired|is invalid|invalid)|"
                r"expired token|invalid[_ ]token|re-?auth|n[aã]o (est[aá] )?autenticado|n[aã]o autorizado|"
                r"autentica[cç][aã]o (falhou|necess[aá]ria|expirou)|sess[aã]o expirou")
_MODULO = _rx(r"ModuleNotFoundError|No module named|\bImportError\b|Cannot find module|MODULE_NOT_FOUND|"
              r"Cannot find package|nenhum m[oó]dulo chamado|m[oó]dulo .{1,60} n[aã]o encontrado")
_COMANDO = _rx(r"command not found|is not recognized as|n[aã]o [eé] reconhecido como|n[aã]o foi reconhecido|"
               r"\b(sh|bash|zsh|dash): (line )?\d*:? ?\S+: not found|comando n[aã]o encontrado")
_SINTAXE = _rx(r"syntax error|\bParserError\b|Unexpected token|unexpected EOF|unexpected end of file|"
               r"unterminated (quoted )?string|unmatched ['\"`]|bad substitution|missing (closing|terminator)|"
               r"string is missing the terminator|erro de sintaxe|token inesperado|fim inesperado")
_EDICAO = _rx(r"String to replace not found|has not been read yet|read it first|has been (unexpectedly )?modified|"
              r"Found \d+ matches? of the string|No changes to make|old_string and new_string are exactly|"
              r"texto a substituir n[aã]o|n[aã]o foi lido")
_ARQUIVO = _rx(r"No such file or directory|FileNotFoundError|\bENOENT\b|cannot find the (file|path)|"
               r"can'?t find the (file|path)|Cannot find path|"
               r"(file|directory|path|folder|diret[oó]rio|arquivo|pasta|caminho)\b[^\n]{0,80}"
               r"(does not exist|not found|n[aã]o existe|n[aã]o encontrad[oa])|"
               r"o sistema n[aã]o pode encontrar|n[aã]o foi poss[ií]vel (localizar|encontrar) (o |a )?(arquivo|caminho|pasta)")
_PERMISSAO = _rx(r"permission denied|\bEACCES\b|\bEPERM\b|access is denied|acesso negado|operation not permitted|"
                 r"PermissionError|UnauthorizedAccessException|permiss[aã]o negada|sem permiss[aã]o")
# "timeout" de rede (ConnectTimeout, ReadTimeout, connection timed out) é `rede`, não `timeout`
_TIMEOUT = _rx(r"(?<![A-Za-z])(?<!connection )(timed? ?out|timeout)|deadline exceeded|tempo (limite|esgotado)|"
               r"excedeu o tempo")
_TESTE = re.compile(r"(?-i:\bFAILED\b)|\b\d+ failed\b|AssertionError|\btests? failed\b|Tests:\s+\d+ failed|"
                    r"\b\d+ failing\b|\d+ falh(ou|aram)|(?-i:\bFAIL\b)", re.IGNORECASE)
_REDE = _rx(r"ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|getaddrinfo|network is unreachable|"
            r"no route to host|connection (refused|reset|aborted|error)|could not resolve host|"
            r"name or service not known|(connect|read|connection)[ _]?timeout|connection timed out|ProxyError|proxy (error|authentication)|"
            r"\bSSL(Error|: )|certificate verify|failed to connect|unable to connect|"
            r"n[aã]o foi poss[ií]vel conectar|falha (de|na) conex[aã]o|conex[aã]o recusada")
_SAIDA = re.compile(r"^\s*(?:Exit code|C[oó]digo de sa[ií]da)[:\s]+(-?\d{1,5})\b", re.IGNORECASE)


def _texto(valor) -> str:
    return valor[:_LIMITE] if isinstance(valor, str) else ""


def classificar(texto, tool: str | None = None, servidor_mcp: str | None = None,
                negacao: str | None = None) -> str:
    """Categoria de um erro de tool. Função pura: devolve só o rótulo (um item de `CATEGORIAS`).

    `texto` é a mensagem de erro (lida em memória, descartada logo depois); `tool` é o nome da tool; `servidor_mcp`
    é o servidor, se a tool for MCP; `negacao` é o `toolDenialKind` da linha do resultado, quando existir.
    """
    msg = _texto(texto)
    tool = tool or ""
    if negacao == NEGACAO_USUARIO or (negacao is None and _RECUSA_TEXTO.search(msg)):
        return "recusado_usuario"
    if negacao in NEGACAO_BLOQUEIO:
        return "bloqueado_permissao"
    if servidor_mcp and _MCP_AUTH.search(msg):
        return "mcp_auth"
    if _MODULO.search(msg):
        return "modulo_ausente"
    if _COMANDO.search(msg):
        return "comando_ausente"
    if _SINTAXE.search(msg):
        return "sintaxe_shell"
    if tool in TOOLS_DE_EDICAO and _EDICAO.search(msg):
        return "edicao_invalida"
    if _ARQUIVO.search(msg):
        return "arquivo_ausente"
    if _PERMISSAO.search(msg):
        return "permissao"
    if _TIMEOUT.search(msg):
        return "timeout"
    if _TESTE.search(msg):
        return "teste_falhou"
    if _REDE.search(msg):
        return "rede"
    return "outro"


def codigo_de_saida(texto, tool: str | None = None) -> int | None:
    """Código de saída de um comando de shell que falhou (`Exit code N` no início da mensagem); senão None."""
    if tool is not None and tool not in TOOLS_DE_SHELL:
        return None
    achou = _SAIDA.match(_texto(texto))
    return int(achou.group(1)) if achou else None


def servidor_mcp_da_tool(tool: str | None) -> str | None:
    """`mcp__google-drive__search_files` -> `google-drive`; tool que não é MCP -> None."""
    if not isinstance(tool, str) or not tool.startswith("mcp__"):
        return None
    partes = tool.split("__", 2)
    return partes[1] if len(partes) == 3 and partes[1] else None
