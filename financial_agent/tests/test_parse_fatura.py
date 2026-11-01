"""Testes do parser de CSV e do loader de regras.

Todos offline. O fixture ``Nubank_2026-08-25.csv`` é o arquivo real, byte a
byte, baixado do Drive do usuário (4405 bytes, 101 lançamentos).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

from parse_fatura import (  # noqa: E402
    FaturaInvalida,
    ciclo_do_nome,
    detecta_formato,
    parse_data_iso,
    parse_fatura,
    parse_fatura_detalhado,
    parse_valor,
    parse_valor_br,
)
from regras import RegrasInvalidas, carrega_regras, extrai_parcela  # noqa: E402

FIXTURE_AGOSTO = RAIZ / "tests" / "fixtures" / "Nubank_2026-08-25.csv"
FIXTURE_ABRIL = RAIZ / "tests" / "fixtures" / "Nubank_2026-04-01.csv"


# --------------------------------------------------------------------------
# Conversão de valores BR
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bruto, esperado",
    [
        ("339,09", "339.09"),
        ("0,65", "0.65"),
        ("10.616,67", "10616.67"),          # ponto de milhar
        ("- 8,53", "-8.53"),                # espaço após o sinal
        ("- 10.616,67", "-10616.67"),       # sinal + espaço + milhar
        ("-8,53", "-8.53"),                 # sinal sem espaço
        ("R$ 186,63", "186.63"),            # prefixo de moeda
        ("  54,06  ", "54.06"),             # espaços nas bordas
        ("20,00", "20.00"),
        ("1.234.567,89", "1234567.89"),     # dois grupos de milhar
    ],
)
def test_parse_valor_br(bruto: str, esperado: str) -> None:
    assert parse_valor_br(bruto) == Decimal(esperado)


def test_parse_valor_br_preserva_duas_casas() -> None:
    assert str(parse_valor_br("20,00")) == "20.00"
    assert str(parse_valor_br("- 0,65")) == "-0.65"


@pytest.mark.parametrize("bruto", ["", "   ", "abc", "1,2,3", "R$", "12.34.56"])
def test_parse_valor_br_rejeita_invalido(bruto: str) -> None:
    with pytest.raises(FaturaInvalida):
        parse_valor_br(bruto)


def test_parse_valor_br_nao_usa_float() -> None:
    """0,1 + 0,2 tem de dar exatamente 0,30 — é o ponto de usar Decimal."""
    assert parse_valor_br("0,10") + parse_valor_br("0,20") == Decimal("0.30")


# --------------------------------------------------------------------------
# Formato americano dos extratos antigos
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bruto, esperado",
    [
        ("12.10", "12.10"),
        ("940.92", "940.92"),
        ("1,234.56", "1234.56"),       # vírgula de milhar
        ("- 8.53", "-8.53"),
        ("8411.26", "8411.26"),
    ],
)
def test_parse_valor_us(bruto: str, esperado: str) -> None:
    assert parse_valor(bruto, "us") == Decimal(esperado)


@pytest.mark.parametrize("bruto", ["12,34,56", "1.234,56", "abc"])
def test_parse_valor_us_rejeita_invalido(bruto: str) -> None:
    with pytest.raises(FaturaInvalida):
        parse_valor(bruto, "us")


def test_detecta_formato() -> None:
    assert detecta_formato(["12.10", "15.50", "29.96"]) == "us"
    assert detecta_formato(["339,09", "- 0,65", "10.616,67"]) == "br"
    assert detecta_formato([]) == "br"           # sem pistas, assume o atual
    assert detecta_formato(["1.234", "5.678"]) == "br"  # ambíguo isolado


def test_detecta_formato_decide_por_maioria() -> None:
    """Uma linha ambígua não vira o arquivo inteiro."""
    assert detecta_formato(["12.10", "15.50", "1.234"]) == "us"
    assert detecta_formato(["339,09", "54,06", "1.234"]) == "br"


def test_abril_e_formato_americano() -> None:
    r = parse_fatura_detalhado(FIXTURE_ABRIL, "2026-04")
    assert r.formato == "us"
    assert r.quantidade == 72
    assert r.total == Decimal("6156.63")


def test_abril_tem_devolucao_de_iof_orfa() -> None:
    """Espelho do caso de agosto: aqui sobra devolução, não cobrança."""
    r = parse_fatura_detalhado(FIXTURE_ABRIL, "2026-04")
    assert r.descartados_por_regra == {
        "^IOF de volta de ": 2,
        '^Crédito de "': 1,
        "^Pagamento recebido": 1,
    }
    assert r.residuo_iof == Decimal("-5.49")


def test_credito_de_e_filtrado() -> None:
    """Mesma família do estorno; entrou nas regras depois de aparecer como
    'assinatura' de valor negativo no relatório."""
    regras = carrega_regras()
    assert regras.deve_ignorar('Crédito de "Google One"')
    assert not regras.deve_ignorar("Google One")


def test_agosto_continua_sendo_lido_como_br() -> None:
    """Regressão: a detecção não pode mudar o que já estava certo."""
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    assert r.formato == "br"
    assert r.total == Decimal("5072.63")


# --------------------------------------------------------------------------
# Datas
# --------------------------------------------------------------------------

def test_parse_data_iso() -> None:
    assert parse_data_iso("2026-08-17").isoformat() == "2026-08-17"


@pytest.mark.parametrize("bruto", ["17/08/2026", "2026-13-01", "", "ontem"])
def test_parse_data_iso_rejeita_invalido(bruto: str) -> None:
    with pytest.raises(FaturaInvalida):
        parse_data_iso(bruto)


# --------------------------------------------------------------------------
# Ciclo derivado do nome do anexo
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "nome, esperado",
    [
        ("Nubank_2026-08-25.csv", "2026-08"),
        ("Nubank_2026-09-25.csv", "2026-09"),
        ("Nubank_2026-10-25.csv", "2026-10"),
        ("Nubank_2026-12-25.csv", "2026-12"),
        ("Nubank_2027-01-25.csv", "2027-01"),
        (r"C:\tmp\Nubank_2026-10-25.csv", "2026-10"),  # caminho completo
    ],
)
def test_ciclo_do_nome(nome: str, esperado: str) -> None:
    assert ciclo_do_nome(nome) == esperado


def test_ciclo_do_nome_descarta_o_dia_sem_aritmetica() -> None:
    """Dia 25 não empurra para o mês seguinte. O ciclo é literal."""
    assert ciclo_do_nome("Nubank_2026-10-25.csv") == "2026-10"
    assert ciclo_do_nome("Nubank_2026-10-01.csv") == "2026-10"
    assert ciclo_do_nome("Nubank_2026-10-31.csv") == "2026-10"


@pytest.mark.parametrize(
    "nome",
    [
        "fatura.csv",
        "Nubank_2026-10.csv",
        "Nubank_2026-10-25.txt",
        "Nubank-2026-10-25.csv",
        "Nubank_2026-10-25.pdf",  # o pipeline é só CSV
    ],
)
def test_ciclo_do_nome_rejeita_fora_do_padrao(nome: str) -> None:
    with pytest.raises(ValueError):
        ciclo_do_nome(nome)


# --------------------------------------------------------------------------
# Parcelamento
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "titulo, esperado",
    [
        ("Tap Web Vip - Parcela 3/10", "3/10"),
        ("Amazon Marketplace - Parcela 9/12", "9/12"),
        ("Spiti*Finclass - Parcela 11/12", "11/12"),
        ("EBW*NuViagens - NuPay - Parcela 3/8", "3/8"),
        ("Safetywing", None),
        ("eSIM de Viagem - 10GB", None),          # tem hífen, não é parcela
        ("Pix no Crédito - Liliane D Avila", None),
    ],
)
def test_extrai_parcela(titulo: str, esperado: str | None) -> None:
    assert extrai_parcela(titulo) == esperado


# --------------------------------------------------------------------------
# Regras carregadas do markdown
# --------------------------------------------------------------------------

def test_carrega_as_quatro_regras() -> None:
    regras = carrega_regras()
    padroes = [r.padrao for r in regras.ignorados]
    assert padroes == [
        "^IOF de volta de ",
        '^IOF de "',
        '^Estorno de "',
        '^Crédito de "',
        "^Pagamento recebido",
    ]


def test_loader_ignora_bullets_de_outras_secoes() -> None:
    """A seção 'Padrões que NÃO devem ser filtrados' também tem backticks."""
    regras = carrega_regras()
    assert not regras.deve_ignorar("Pix no Crédito - PAGSEGURO INTERNATIONAL")
    assert not regras.deve_ignorar("eSIM de Viagem - 10GB")
    assert not regras.deve_ignorar("Tap Web Vip - Parcela 3/10")


@pytest.mark.parametrize(
    "titulo",
    [
        "IOF de volta de Tfl Travel Ch",
        'IOF de "Tfl Travel Ch"',
        'Estorno de "Apple.Com/Bill" (Apple)',
        "Pagamento recebido",
    ],
)
def test_regras_pegam_os_ajustes_reais(titulo: str) -> None:
    assert carrega_regras().deve_ignorar(titulo)


def test_regras_falham_alto_se_arquivo_sumir(tmp_path: Path) -> None:
    with pytest.raises(RegrasInvalidas):
        carrega_regras(tmp_path / "nao_existe.md")


def test_regras_falham_alto_se_regex_invalido(tmp_path: Path) -> None:
    ruim = tmp_path / "regras.md"
    ruim.write_text("## Padrões ignorados\n\n- `[invalido` — regex quebrado\n", encoding="utf-8")
    with pytest.raises(RegrasInvalidas):
        carrega_regras(ruim)


def test_regras_falham_alto_se_secao_vazia(tmp_path: Path) -> None:
    vazio = tmp_path / "regras.md"
    vazio.write_text("## Padrões ignorados\n\nsem bullets aqui.\n", encoding="utf-8")
    with pytest.raises(RegrasInvalidas):
        carrega_regras(vazio)


# --------------------------------------------------------------------------
# Parse do CSV real de agosto
# --------------------------------------------------------------------------

def test_fixture_existe() -> None:
    assert FIXTURE_AGOSTO.exists(), "fixture real de agosto ausente"
    assert FIXTURE_AGOSTO.stat().st_size == 4405, "fixture divergiu do arquivo do Drive"


def test_parse_agosto_agregados() -> None:
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    assert r.ciclo == "2026-08"
    assert r.quantidade == 54
    assert r.total == Decimal("5072.63")
    assert len(r.descartados) == 47
    assert r.quantidade + len(r.descartados) == 101


def test_parse_agosto_descartes_por_regra() -> None:
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    assert r.descartados_por_regra == {
        "^IOF de volta de ": 22,
        '^IOF de "': 23,
        '^Estorno de "': 1,
        "^Pagamento recebido": 1,
    }


def test_pares_de_iof_nao_cancelam_exatamente_no_ciclo() -> None:
    """Efeito de borda real: sobra uma cobrança cuja devolução caiu no ciclo
    anterior. Documentado em references/regras.md."""
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    somas = r.somas_descartadas
    assert somas['^IOF de "'] == Decimal("107.02")
    assert somas["^IOF de volta de "] == Decimal("-104.52")
    assert r.residuo_iof == Decimal("2.50")


def test_pagamento_recebido_tem_milhar_negativo() -> None:
    """A linha que exercita '- 10.616,67' ponta a ponta."""
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    assert r.somas_descartadas["^Pagamento recebido"] == Decimal("-10616.67")


def test_titulo_com_aspas_escapadas_sobrevive() -> None:
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    titulos = {l.titulo for l, _ in r.descartados}
    assert 'IOF de "Tfl Travel Ch"' in titulos
    assert 'Estorno de "Apple.Com/Bill" (Apple)' in titulos


def test_parcelados_do_ciclo() -> None:
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    parcelas = sorted(l.parcela for l in r.mantidos if l.parcela)
    assert parcelas == ["11/12", "3/10", "3/8", "9/12", "9/12"]


def test_titulo_preserva_sufixo_de_parcela() -> None:
    """A descrição mantém ' - Parcela n/m'; a coluna Parcela repete o valor."""
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    linha = next(l for l in r.mantidos if l.parcela == "3/10")
    assert linha.titulo == "Tap Web Vip - Parcela 3/10"


def test_todos_mantidos_recebem_o_ciclo() -> None:
    r = parse_fatura_detalhado(FIXTURE_AGOSTO, ciclo="2026-99")
    assert {l.ciclo for l in r.mantidos} == {"2026-99"}


def test_valores_sempre_com_duas_casas() -> None:
    r = parse_fatura_detalhado(FIXTURE_AGOSTO)
    assert all(-l.valor.as_tuple().exponent == 2 for l in r.mantidos)


def test_contrato_publico_parse_fatura() -> None:
    """A assinatura que o CLAUDE.md promete: list[dict] já filtrado."""
    linhas = parse_fatura(FIXTURE_AGOSTO)
    assert len(linhas) == 54
    assert set(linhas[0]) == {"ciclo", "data", "titulo", "valor", "parcela"}
    assert linhas[0]["data"] == "2026-08-17"
    assert isinstance(linhas[0]["valor"], Decimal)


# --------------------------------------------------------------------------
# Rejeição de CSV malformado
# --------------------------------------------------------------------------

def test_rejeita_cabecalho_errado(tmp_path: Path) -> None:
    ruim = tmp_path / "x.csv"
    ruim.write_text("data,descricao,valor\n2026-01-01,X,\"1,00\"\n", encoding="utf-8")
    with pytest.raises(FaturaInvalida, match="cabeçalho inesperado"):
        parse_fatura_detalhado(ruim, ciclo="2026-01")


def test_rejeita_arquivo_vazio(tmp_path: Path) -> None:
    vazio = tmp_path / "x.csv"
    vazio.write_text("", encoding="utf-8")
    with pytest.raises(FaturaInvalida):
        parse_fatura_detalhado(vazio, ciclo="2026-01")


def test_erro_de_linha_diz_qual_linha(tmp_path: Path) -> None:
    ruim = tmp_path / "x.csv"
    ruim.write_text(
        'date,title,amount\n2026-01-01,Ok,"1,00"\n2026-01-02,Ruim,"xpto"\n',
        encoding="utf-8",
    )
    with pytest.raises(FaturaInvalida, match=r":3:"):
        parse_fatura_detalhado(ruim, ciclo="2026-01")


def test_tolera_linha_em_branco_no_fim(tmp_path: Path) -> None:
    ok = tmp_path / "x.csv"
    ok.write_text('date,title,amount\n2026-01-01,Ok,"1,00"\n\n', encoding="utf-8")
    assert parse_fatura_detalhado(ok, ciclo="2026-01").quantidade == 1


def test_aceita_bom_no_inicio(tmp_path: Path) -> None:
    com_bom = tmp_path / "x.csv"
    com_bom.write_bytes('date,title,amount\n2026-01-01,Ok,"1,00"\n'.encode("utf-8-sig"))
    assert parse_fatura_detalhado(com_bom, ciclo="2026-01").quantidade == 1
