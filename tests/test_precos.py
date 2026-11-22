from token_controler.precos import achar_preco, calcular_custo


def test_prefixo_mais_longo_vence():
    assert achar_preco("claude-opus-5-5")[0] == 4.00
    assert achar_preco("claude-opus-5")[0] == 5.00
    assert achar_preco("claude-opus-4-1-20250805")[0] == 15.00
    assert achar_preco("claude-sonnet-4-5-20250929")[0] == 3.00


def test_haiku_55_muda_de_patamar_acima_de_100k():
    assert achar_preco("claude-haiku-5-5", 100_000)[0] == 0.10
    assert achar_preco("claude-haiku-5-5", 100_001)[0] == 0.50


def test_modelo_desconhecido_nao_tem_preco():
    assert achar_preco("gpt-9") is None
    assert achar_preco(None) is None
    assert calcular_custo("<synthetic>", 10, 10, 0, 0, 0) is None


def test_custo_com_todos_os_tipos_de_token():
    # sonnet 5.5: 1M de cada tipo = 2 + 10 + 2.5 + 4 + 0.10
    assert calcular_custo("claude-sonnet-5-5", 10**6, 10**6, 10**6, 10**6, 10**6) == 18.6
