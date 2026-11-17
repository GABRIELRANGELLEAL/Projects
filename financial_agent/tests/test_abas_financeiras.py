"""Testes das abas de custo no débito e de receita.

Offline: os parsers recebem as linhas como a API do Sheets as devolve, então
não é preciso rede nem credencial. Os fixtures inline são os dados reais das
duas abas em 17/11/2026 — inclusive os dois formatos de mês, que diferem entre
elas (``Nov - 2026`` contra ``Nov 2026``).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

import pytest  # noqa: E402

from relatorio import (  # noqa: E402
    ValorIlegivel,
    mes_para_ciclo,
    monta_saldo,
    parse_custos,
    parse_receita,
    taxa_usd_brl,
    valor_us,
)

CUSTOS = [
    ["Mes", "Nome do gasto", "total", "", "R$ to EU", "$ to EU"],
    ["Nov - 2026", "Lilian", "R$400", "", "0.1571", "0.90869"],
    ["Nov - 2026", "Moradia", "R$3,819"],
    ["Nov - 2026", "Irmãos", "R$300"],
    ["Nov - 2026", "Dupla cidadania", "R$1,714"],
    ["Nov - 2026", "honorár", "R$300"],
    ["Nov - 2026", "Raquel", "R$400"],
    ["Nov - 2026", "imposto", "R$2,000"],
    ["Dec - 2026", "Lilian", "R$600"],
    ["Dec - 2026", "Moradia", "R$3,819"],
    ["Dec - 2026", "Irmãos", "R$300"],
    ["Dec - 2026", "honorár", "R$300"],
    ["Dec - 2026", "Raquel", "R$400"],
    ["Dec - 2026", "honorários", "R$300"],
    ["Dec - 2026", "imposto", "R$1,200"],
]

RECEITA = [
    ["Mes", "empresa", "valor em dolar"],
    ["Nov 2026", "Rakbank", "4000"],
]


# ------------------------------------------------------------------ mês

@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Nov - 2026", "2026-11"),   # aba de custos
        ("Nov 2026", "2026-11"),     # aba de receita
        ("2024-Apr", "2024-04"),     # aba de investimentos
        ("2026-11", "2026-11"),      # já no formato do ciclo
        ("dez/2026", "2026-12"),     # português, que é questão de tempo
        ("  Nov - 2026  ", "2026-11"),
    ],
)
def test_mes_para_ciclo_aceita_os_formatos_da_planilha(texto, esperado) -> None:
    assert mes_para_ciclo(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "",                 # célula vazia
        "Nov",              # sem ano
        "2026",             # sem mês
        "Nov - Dez 2026",   # dois meses
        "Nov - 2026 2027",  # dois anos
        "Novembro passado",
        "13/2026",
        "2026-13",          # mês fora da faixa
    ],
)
def test_mes_ilegivel_nunca_virou_palpite(texto) -> None:
    """Preferir ``None`` a chutar: mês errado joga o gasto no mês errado."""
    assert mes_para_ciclo(texto) is None


# ---------------------------------------------------------------- valor

@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("R$400", "400"),
        ("R$3,819", "3819"),        # vírgula é milhar, como em Lançamentos
        ("R$ 1,234.56", "1234.56"),
        ("4000", "4000"),
        ("0.1571", "0.1571"),       # taxa de câmbio, sem símbolo
        ("", "0"),
        ("  R$ 300 ", "300"),
    ],
)
def test_valor_us(texto, esperado) -> None:
    assert valor_us(texto) == Decimal(esperado)


def test_milhar_ambiguo_e_recusado() -> None:
    """``R$ 3.819`` é 3819 em BR e 3,819 em US — erro, não palpite.

    É o mesmo princípio do validador do parser de CSV: corrupção silenciosa de
    valor é o pior erro possível neste pipeline.
    """
    with pytest.raises(ValorIlegivel):
        valor_us("R$ 3.819")
    with pytest.raises(ValorIlegivel):
        valor_us("R$3.000")


def test_decimal_sem_simbolo_continua_valido() -> None:
    """A recusa é só para valor com moeda: taxa de câmbio tem ponto decimal."""
    assert valor_us("0.905") == Decimal("0.905")


def test_texto_nao_numerico() -> None:
    with pytest.raises(ValorIlegivel):
        valor_us("a combinar")


# ------------------------------------------------------------- parsers

def test_parse_custos_le_a_aba_real() -> None:
    linhas, problemas = parse_custos(CUSTOS)
    assert problemas == []
    assert len(linhas) == 14
    nov = [l for l in linhas if l["ciclo"] == "2026-11"]
    dez = [l for l in linhas if l["ciclo"] == "2026-12"]
    assert sum(l["valor"] for l in nov) == Decimal("8933")
    assert sum(l["valor"] for l in dez) == Decimal("6919")
    assert {l["nome"] for l in nov} >= {"Moradia", "Dupla cidadania", "imposto"}


def test_parse_custos_ignora_as_celulas_de_cambio() -> None:
    """As taxas moram em E/F da mesma aba e não podem entrar como gasto."""
    linhas, _ = parse_custos(CUSTOS)
    assert all(l["valor"] != Decimal("0.1571") for l in linhas)
    assert len(linhas) == len(CUSTOS) - 1


def test_parse_custos_relata_linha_torta_em_vez_de_somar_zero() -> None:
    tortas = CUSTOS + [
        ["sei lá", "Alguma coisa", "R$100"],
        ["Jan - 2027", "Outra", "R$ 2.500"],
    ]
    linhas, problemas = parse_custos(tortas)
    assert len(linhas) == 14
    assert len(problemas) == 2
    assert "mês ilegível" in problemas[0]
    assert "linha 17" in problemas[1]


def test_cabecalho_trocado_nao_e_lido() -> None:
    """Coluna renomeada ou reordenada à mão para a seção, não gera número."""
    linhas, problemas = parse_custos([["Mes", "total", "Nome do gasto"], ["Nov - 2026", "R$400", "Lilian"]])
    assert linhas == []
    assert "cabeçalho inesperado" in problemas[0]


def test_parse_receita_le_a_aba_real() -> None:
    linhas, problemas = parse_receita(RECEITA)
    assert problemas == []
    assert linhas == [{"ciclo": "2026-11", "empresa": "Rakbank", "usd": Decimal("4000.00")}]


def test_aba_vazia_nao_explode() -> None:
    assert parse_custos([]) == ([], ["cabeçalho inesperado: esperado "
                                     "['Mes', 'Nome do gasto', 'total'], encontrado ['', '', '']"])


# -------------------------------------------------------------- câmbio

def test_taxa_sai_da_razao_entre_as_duas_pontas_de_euro() -> None:
    """A planilha não guarda US$ -> R$; guarda R$ -> € e US$ -> €."""
    usd_brl, brl_eur, usd_eur = taxa_usd_brl(CUSTOS)
    assert (brl_eur, usd_eur) == (Decimal("0.1571"), Decimal("0.90869"))
    assert usd_brl == Decimal("0.90869") / Decimal("0.1571")
    assert Decimal("5.78") < usd_brl < Decimal("5.79")


def test_taxa_procurada_pelo_rotulo_nao_pela_coluna() -> None:
    """Uma coluna nova no meio da aba não pode mudar a taxa lida."""
    deslocado = [
        ["Mes", "Nome do gasto", "total", "", "", "obs", "$ to EU", "R$ to EU"],
        ["Nov - 2026", "Lilian", "R$400", "", "", "x", "0.90869", "0.1571"],
    ]
    assert taxa_usd_brl(deslocado)[0] == taxa_usd_brl(CUSTOS)[0]


@pytest.mark.parametrize(
    "valores",
    [
        [["Mes", "Nome do gasto", "total"]],                                    # só cabeçalho
        [["Mes", "total", "R$ to EU"], ["Nov - 2026", "R$400", "0.1571"]],      # falta $ to EU
        [["Mes", "R$ to EU", "$ to EU"], ["Nov - 2026", "0", "0.90869"]],       # taxa zero
    ],
)
def test_sem_cambio_confiavel_devolve_none(valores) -> None:
    assert taxa_usd_brl(valores) is None


# --------------------------------------------------------------- saldo

def test_saldo_cruza_as_tres_fontes() -> None:
    custos, _ = parse_custos(CUSTOS)
    receitas, _ = parse_receita(RECEITA)
    usd_brl = taxa_usd_brl(CUSTOS)[0]
    saldo = monta_saldo({"2026-11": Decimal("25722.00")}, custos, receitas, usd_brl)

    assert [l["ciclo"] for l in saldo] == ["2026-11", "2026-12"]
    nov = saldo[0]
    assert nov["receita"] == Decimal("23136.60")  # 4000 * 5.78415...
    assert nov["custo"] == Decimal("8933")
    assert nov["fatura"] == Decimal("25722.00")
    assert nov["saldo"] == Decimal("23136.60") - Decimal("8933") - Decimal("25722.00")


def test_mes_sem_receita_fica_sem_saldo() -> None:
    """Dezembro só tem custo planejado: receita ausente não é receita zero.

    Sem isso o mês futuro apareceria com um saldo negativo enorme, que não é
    informação — é dado que falta.
    """
    custos, _ = parse_custos(CUSTOS)
    receitas, _ = parse_receita(RECEITA)
    dez = monta_saldo({}, custos, receitas, Decimal("5.78"))[1]
    assert dez["ciclo"] == "2026-12"
    assert dez["receita"] is None
    assert dez["saldo"] is None
    assert dez["custo"] == Decimal("6919")


def test_sem_cambio_o_saldo_nao_e_inventado() -> None:
    custos, _ = parse_custos(CUSTOS)
    receitas, _ = parse_receita(RECEITA)
    nov = monta_saldo({"2026-11": Decimal("100")}, custos, receitas, None)[0]
    assert nov["receita_usd"] == Decimal("4000.00")
    assert nov["receita"] is None and nov["saldo"] is None


def test_fatura_sem_receita_nem_debito_fica_de_fora() -> None:
    """A tabela de saldo é sobre os meses das abas novas, não sobre 8 ciclos."""
    saldo = monta_saldo({"2026-04": Decimal("1000")}, [], [], Decimal("5.78"))
    assert saldo == []


# ------------------------------------------------- palavras-chave de assinatura

def test_cuidados_conhecidos_nao_entram_como_palavra_chave() -> None:
    """O bloco de ressalvas vive dentro de ``## Assinaturas`` e não é chave.

    Regressão: enquanto "Cuidados conhecidos" era negrito em vez de ``###``, o
    loader ia até o fim da seção e carregava os bullets de lá. ``Spiti*Finclass``
    virava palavra-chave — justamente o que aquele bullet manda não fazer — e o
    relatório somava R$ 39,95 por ciclo de um parcelamento ao total de
    assinaturas. O loader para no primeiro cabeçalho, então basta o ``###``.
    """
    from regras import carrega_assinaturas

    chaves = carrega_assinaturas()
    assert "spiti*finclass" not in chaves
    assert len(chaves) == 15
    # As de verdade continuam lá, incluindo a que a ressalva comenta.
    assert {"anthropic", "netflix", "disney", "openai"} <= set(chaves)
