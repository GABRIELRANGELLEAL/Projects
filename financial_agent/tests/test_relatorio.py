"""Testes da dedução de compra internacional no relatório.

Offline, contra os dois fixtures reais. Eles cobrem os dois formatos de título
de IOF que o Nubank exporta, que é exatamente o que a função precisa tratar:
agosto usa ``IOF de "<comerciante>"`` e abril usa ``IOF de compra
internacional``, sem nome nenhum.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

from parse_fatura import parse_fatura_detalhado  # noqa: E402
from relatorio import comerciantes_internacionais  # noqa: E402

FIXTURE_AGOSTO = RAIZ / "tests" / "fixtures" / "Nubank_2026-08-25.csv"
FIXTURE_ABRIL = RAIZ / "tests" / "fixtures" / "Nubank_2026-04-01.csv"


def test_agosto_acha_pelo_titulo_da_cobranca() -> None:
    """No formato antigo a cobrança nomeia o comerciante, e é o caminho curto."""
    nomes = comerciantes_internacionais(parse_fatura_detalhado(FIXTURE_AGOSTO, "2026-08"))
    assert "Tfl Travel Ch" in nomes
    assert "Sainsburys S/Mkts" in nomes
    # Compra nacional não entra: não há IOF para ela.
    assert "Pix no Crédito - MARIA INES GONZALEZ SOLARI" not in nomes


def test_abril_acha_pela_devolucao() -> None:
    """A regressão que motivou a função.

    Abril usa ``IOF de compra internacional``, que não carrega nome. Lendo só a
    cobrança o conjunto vinha vazio e a seção de exterior dava 0%; o nome está
    na devolução, e os dois pares de abril fecham exatos.
    """
    r = parse_fatura_detalhado(FIXTURE_ABRIL, "2026-04")
    assert [l.titulo for l, _ in r.descartados].count("IOF de compra internacional") == 2
    assert comerciantes_internacionais(r) == {
        "Udemy Subscription",
        "Openai *Chatgpt Subscr",
    }


def test_so_a_cobranca_nao_bastaria_em_abril() -> None:
    """Fixa a diferença entre as duas fontes, para o caso de alguém 'simplificar'.

    Se um dia a função voltar a ler só a cobrança, este teste cai junto com o
    número da seção de exterior — que é o que ninguém percebe sozinho.
    """
    import re

    r = parse_fatura_detalhado(FIXTURE_ABRIL, "2026-04")
    so_cobranca = {
        achado.group(1)
        for lanc, _ in r.descartados
        if (achado := re.match(r'^IOF de "(.+)"$', lanc.titulo))
    }
    assert so_cobranca == set()
    assert len(comerciantes_internacionais(r)) == 2
