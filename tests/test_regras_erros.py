"""T23: categorias de erro (design 6.4). Todas as mensagens abaixo são SINTÉTICAS, escritas à mão; nenhuma é de log real."""

import pytest

from token_controler.regras_erros import (CATEGORIAS, RECOMENDACOES, classificar, codigo_de_saida,
                                          servidor_mcp_da_tool)

# (categoria, tool, servidor_mcp, mensagem). Cada categoria tem casos em inglês e português, Windows e Unix.
CASOS = [
    # recusado_usuario: pelo texto só vale em log antigo, sem o campo toolDenialKind
    ("recusado_usuario", "Bash", None, "The user doesn't want to proceed with this tool use. The tool use was rejected."),
    ("recusado_usuario", "Write", None, "[Request interrupted by user for tool use]"),
    ("recusado_usuario", "Bash", None, "O usuário recusou esta chamada."),
    # mcp_auth: só em tool de servidor MCP
    ("mcp_auth", "mcp__drive__buscar", "drive", "HTTP 401 Unauthorized"),
    ("mcp_auth", "mcp__drive__buscar", "drive", "Error: token expired, please re-authenticate"),
    ("mcp_auth", "mcp__drive__buscar", "drive", "Servidor não autenticado: autenticação necessária"),
    ("mcp_auth", "mcp__agenda__listar", "agenda", "status 403 forbidden"),
    # modulo_ausente
    ("modulo_ausente", "Bash", None, "Traceback (most recent call last):\nModuleNotFoundError: No module named 'pandas'"),
    ("modulo_ausente", "PowerShell", None, "ImportError: DLL load failed while importing foo"),
    ("modulo_ausente", "Bash", None, "Error: Cannot find module 'express'"),
    ("modulo_ausente", "Bash", None, "Nenhum módulo chamado 'requests'"),
    # comando_ausente
    ("comando_ausente", "Bash", None, "bash: pytest: command not found"),
    ("comando_ausente", "PowerShell", None,
     "The term 'npm' is not recognized as the name of a cmdlet, function, script file, or operable program."),
    ("comando_ausente", "Bash", None, "'node' is not recognized as an internal or external command,\noperable program or batch file."),
    ("comando_ausente", "PowerShell", None, "O termo 'git' não é reconhecido como nome de cmdlet, função, arquivo de script."),
    ("comando_ausente", "Bash", None, "/bin/sh: 1: foo: not found"),
    # sintaxe_shell
    ("sintaxe_shell", "Bash", None, "bash: syntax error near unexpected token `)'"),
    ("sintaxe_shell", "PowerShell", None, "ParserError: Unexpected token '}' in expression or statement."),
    ("sintaxe_shell", "Bash", None, "unexpected EOF while looking for matching `\"'"),
    ("sintaxe_shell", "PowerShell", None, "The string is missing the terminator: \"."),
    ("sintaxe_shell", "Bash", None, "erro de sintaxe próximo ao token inesperado `('"),
    # edicao_invalida: só em tools de edição
    ("edicao_invalida", "Edit", None, "<tool_use_error>String to replace not found in file.</tool_use_error>"),
    ("edicao_invalida", "Edit", None, "<tool_use_error>File has not been read yet. Read it first before writing to it.</tool_use_error>"),
    ("edicao_invalida", "Write", None, "File has been modified since read, either by the user or by a linter."),
    ("edicao_invalida", "Edit", None, "Found 3 matches of the string to replace, but replace_all is false."),
    # arquivo_ausente
    ("arquivo_ausente", "Bash", None, "cat: dados.csv: No such file or directory"),
    ("arquivo_ausente", "Read", None, "<tool_use_error>File does not exist.</tool_use_error>"),
    ("arquivo_ausente", "PowerShell", None, "Cannot find path 'C:\\x\\y.txt' because it does not exist."),
    ("arquivo_ausente", "PowerShell", None, "Get-Content : O sistema não pode encontrar o arquivo especificado."),
    ("arquivo_ausente", "Bash", None, "FileNotFoundError: [Errno 2] No such file or directory: 'a.json'"),
    ("arquivo_ausente", "Bash", None, "Error: ENOENT: no such file or directory, open 'x'"),
    ("arquivo_ausente", "Bash", None, "O arquivo 'config.yml' não existe"),
    # permissao
    ("permissao", "Bash", None, "mkdir: cannot create directory 'x': Permission denied"),
    ("permissao", "PowerShell", None, "Access is denied."),
    ("permissao", "PowerShell", None, "Acesso negado ao caminho 'C:\\Windows\\x'."),
    ("permissao", "Bash", None, "Error: EACCES: permission denied, mkdir '/opt/x'"),
    ("permissao", "Bash", None, "PermissionError: [Errno 13]"),
    # timeout
    ("timeout", "Bash", None, "Command timed out after 120000ms"),
    ("timeout", "PowerShell", None, "Timeout: the operation exceeded the limit"),
    ("timeout", "Bash", None, "Tempo limite excedido ao rodar o comando"),
    # teste_falhou
    ("teste_falhou", "Bash", None, "FAILED tests/test_a.py::test_b - AssertionError: assert 1 == 2\n1 failed, 3 passed in 0.2s"),
    ("teste_falhou", "PowerShell", None, "Tests: 2 failed, 5 passed, 7 total"),
    ("teste_falhou", "Bash", None, "3 failing\n  1) soma"),
    ("teste_falhou", "Bash", None, "Traceback (most recent call last):\nAssertionError"),
    ("teste_falhou", "Bash", None, "2 falharam, 8 passaram"),
    # rede
    ("rede", "Bash", None, "curl: (7) Failed to connect to exemplo.test port 443: Connection refused"),
    ("rede", "Bash", None, "Error: getaddrinfo ENOTFOUND registry.exemplo.test"),
    ("rede", "PowerShell", None, "Unable to connect to the remote server"),
    ("rede", "Bash", None, "requests.exceptions.ConnectTimeout: HTTPSConnectionPool"),
    ("rede", "Bash", None, "requests.exceptions.ProxyError: Cannot connect to proxy"),
    ("rede", "Bash", None, "Não foi possível conectar ao servidor"),
    ("rede", "WebFetch", None, "SSLError: certificate verify failed"),
    ("rede", "Bash", None, "connection timed out"),
]

# o que NÃO tem regra: tem de cair em `outro`
OUTROS = [
    ("Bash", None, "Exit code 1"),
    ("Bash", None, "Exit code 1\nalgo deu errado de um jeito que nenhuma regra conhece"),
    ("WebSearch", None, "Search failed upstream"),
    ("Edit", None, "Edição não aplicada por motivo desconhecido"),
    ("Bash", None, ""),
    ("Bash", None, "Traceback (most recent call last):\nKeyError: 'nome'"),
    ("Bash", None, "Traceback (most recent call last):\nSyntaxError: invalid syntax"),   # Python, não é sintaxe de shell
    ("Read", None, "Error: unsupported file type"),
    ("mcp__drive__buscar", "drive", "Erro interno do servidor"),
]


@pytest.mark.parametrize("categoria, tool, servidor, texto", CASOS)
def test_categoria(categoria, tool, servidor, texto):
    assert classificar(texto, tool, servidor) == categoria


@pytest.mark.parametrize("tool, servidor, texto", OUTROS)
def test_cai_em_outro(tool, servidor, texto):
    assert classificar(texto, tool, servidor) == "outro"


def test_toda_categoria_tem_pelo_menos_um_caso_em_cada_lista():
    cobertas = {c[0] for c in CASOS} | {"outro", "bloqueado_permissao"}
    assert cobertas == set(CATEGORIAS)
    assert set(RECOMENDACOES) == set(CATEGORIAS)


def test_a_ordem_das_categorias_e_a_do_design():
    assert CATEGORIAS[:3] == ("recusado_usuario", "bloqueado_permissao", "mcp_auth")
    assert CATEGORIAS[-1] == "outro"


# ---------- precedência: a primeira da tabela vence ----------

def test_negacao_vem_do_campo_e_dispensa_o_texto():
    assert classificar("", "WebSearch", None, "user-rejected") == "recusado_usuario"
    assert classificar("nada a ver", "Bash", None, "user-rejected") == "recusado_usuario"
    assert classificar("", "Bash", None, "permission-rule") == "bloqueado_permissao"
    assert classificar("Permission denied", "Bash", None, "automode-blocked") == "bloqueado_permissao"


def test_com_o_campo_de_negacao_o_texto_de_recusa_nao_e_usado():
    # um valor de negação que não conhecemos não vira recusa pelo texto: segue para as regras de texto
    assert classificar("The tool use was rejected", "Bash", None, "outro-tipo") == "outro"


def test_recusa_vence_um_401_de_mcp():
    assert classificar("401 Unauthorized", "mcp__drive__x", "drive", "user-rejected") == "recusado_usuario"
    assert classificar("The user doesn't want to proceed with this tool use. 401", "mcp__drive__x", "drive") == "recusado_usuario"


def test_mcp_auth_vence_modulo_e_arquivo_em_tool_mcp():
    assert classificar("401 Unauthorized: file not found", "mcp__drive__x", "drive") == "mcp_auth"


def test_401_fora_de_mcp_nao_e_mcp_auth():
    assert classificar("HTTP 401 Unauthorized", "WebFetch", None) == "outro"
    assert classificar("HTTP 401 Unauthorized", "Bash", None) == "outro"


def test_edicao_invalida_so_vale_para_tools_de_edicao():
    msg = "String to replace not found in file."
    assert classificar(msg, "Edit") == "edicao_invalida"
    assert classificar(msg, "Bash") == "outro"


def test_edicao_em_arquivo_inexistente_e_arquivo_ausente():
    assert classificar("<tool_use_error>File does not exist.</tool_use_error>", "Edit") == "arquivo_ausente"


def test_timeout_de_rede_e_rede_e_nao_timeout():
    assert classificar("ReadTimeout: HTTPSConnectionPool", "Bash") == "rede"
    assert classificar("ConnectTimeout", "Bash") == "rede"
    assert classificar("pytest timed out after 60s", "Bash") == "timeout"


def test_timeout_vence_teste_falhou():
    assert classificar("FAILED tests/x.py - Timeout", "Bash") == "timeout"


def test_modulo_ausente_vence_teste_falhou():
    assert classificar("FAILED tests/x.py\nModuleNotFoundError: No module named 'x'", "Bash") == "modulo_ausente"


def test_entradas_invalidas_nao_quebram():
    for v in (None, 123, b"bytes", ["lista"], {"a": 1}):
        assert classificar(v, "Bash") == "outro"
    assert classificar("x" * 500000, "Bash") == "outro"
    assert classificar("Permission denied", None, None) == "permissao"


# ---------- exit code ----------

@pytest.mark.parametrize("texto, tool, esperado", [
    ("Exit code 1\nalgo", "Bash", 1),
    ("Exit code 128\nfatal", "Bash", 128),
    ("Exit code 255", "PowerShell", 255),
    ("Exit code: 2", "Bash", 2),
    ("Código de saída 3", "Bash", 3),
    ("  Exit code -1", "Bash", -1),
    ("algo\nExit code 1", "Bash", None),               # só vale no começo
    ("Exit code 1", "Edit", None),                      # só tools de shell
    ("Exit code 1", None, 1),                           # tool desconhecida: aceita
    ("Exit code abc", "Bash", None),
    ("Exit code 123456789", "Bash", None),
    ("", "Bash", None),
    (None, "Bash", None),
])
def test_codigo_de_saida(texto, tool, esperado):
    assert codigo_de_saida(texto, tool) == esperado


@pytest.mark.parametrize("tool, esperado", [
    ("mcp__google-drive__search_files", "google-drive"),
    ("mcp__drive__a__b", "drive"),
    ("mcp__so_dois", None),
    ("mcp____x", None),
    ("Bash", None),
    (None, None),
    (3, None),
])
def test_servidor_mcp_da_tool(tool, esperado):
    assert servidor_mcp_da_tool(tool) == esperado
