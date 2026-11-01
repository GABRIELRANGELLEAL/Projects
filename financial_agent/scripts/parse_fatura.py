"""Parser do CSV de extrato de fatura do Nubank (passos 1 a 4 do workflow).

Roda isolado, sem depender do resto do pipeline::

    python scripts/parse_fatura.py tests/fixtures/Nubank_2026-08-25.csv
    python scripts/parse_fatura.py caminho.csv --json out/2026-08_rows.json

A única dependência interna é ``regras.py``, porque os padrões a ignorar são
lógica de negócio e vivem em ``references/regras.md`` por convenção do projeto.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from regras import Regras, carrega_regras, extrai_parcela  # noqa: E402

COLUNAS_ESPERADAS = ("date", "title", "amount")
CENTAVOS = Decimal("0.01")

#: ``Nubank_2026-08-25.csv`` -> ciclo ``2026-08``. O dia é descartado.
#: Só CSV: o pipeline não lida com PDF.
RE_NOME_ANEXO = re.compile(r"^Nubank_(\d{4})-(\d{2})-(\d{2})\.csv$", re.IGNORECASE)

#: Estrutura válida de um valor BR, já sem sinal, moeda e espaços.
#: Ou grupos de milhar bem-formados (``1.234.567,89``), ou nenhum separador de
#: milhar (``10616,67``). Rejeita agrupamento inválido como ``12.34.56``, que de
#: outra forma viraria ``123456`` silenciosamente ao remover os pontos.
RE_VALOR_BR = re.compile(r"^(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?$")

#: O espelho do anterior, para os extratos antigos: ``1,234.56`` e ``12.10``.
RE_VALOR_US = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$")

#: Pistas de qual formato o arquivo usa: o que vem depois do último separador.
RE_CENTAVOS_BR = re.compile(r",\d{1,2}$")
RE_CENTAVOS_US = re.compile(r"\.\d{1,2}$")


class FaturaInvalida(ValueError):
    """O CSV não tem o formato esperado, ou uma linha não pôde ser convertida."""


@dataclass(frozen=True)
class Lancamento:
    """Uma linha normalizada: ``[ciclo, data, título, valor, parcela]``."""

    ciclo: str
    data: date
    titulo: str
    valor: Decimal
    parcela: str | None

    def para_dict(self) -> dict[str, Any]:
        return {
            "ciclo": self.ciclo,
            "data": self.data.isoformat(),
            "titulo": self.titulo,
            "valor": self.valor,
            "parcela": self.parcela,
        }


@dataclass
class ResultadoParse:
    """Lançamentos mantidos, descartados e os agregados do ciclo."""

    ciclo: str
    origem: Path
    formato: str = "br"
    mantidos: list[Lancamento] = field(default_factory=list)
    descartados: list[tuple[Lancamento, str]] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return _arredonda(sum((l.valor for l in self.mantidos), Decimal(0)))

    @property
    def quantidade(self) -> int:
        return len(self.mantidos)

    @property
    def descartados_por_regra(self) -> dict[str, int]:
        return dict(Counter(padrao for _, padrao in self.descartados))

    @property
    def somas_descartadas(self) -> dict[str, Decimal]:
        """Soma por regra de descarte. Serve de diagnóstico de reconciliação.

        Os pares de IOF (``^IOF de "`` e ``^IOF de volta de ``) quase se
        cancelam, mas **não exatamente**: na borda da janela do ciclo sobra
        uma cobrança cuja devolução caiu no ciclo vizinho. O resíduo aparece
        aqui em vez de desaparecer silenciosamente.
        """
        somas: dict[str, Decimal] = {}
        for lancamento, padrao in self.descartados:
            somas[padrao] = somas.get(padrao, Decimal(0)) + lancamento.valor
        return {padrao: _arredonda(v) for padrao, v in somas.items()}

    @property
    def residuo_iof(self) -> Decimal:
        """Quanto de IOF sobrou sem par neste ciclo (idealmente zero)."""
        somas = self.somas_descartadas
        return _arredonda(
            sum(
                (v for padrao, v in somas.items() if padrao.startswith("^IOF")),
                Decimal(0),
            )
        )

    def top_comerciantes(self, n: int = 10) -> list[tuple[str, Decimal, int]]:
        """``(título, soma, ocorrências)`` dos maiores gastos agregados."""
        somas: dict[str, Decimal] = {}
        contagens: Counter[str] = Counter()
        for l in self.mantidos:
            somas[l.titulo] = somas.get(l.titulo, Decimal(0)) + l.valor
            contagens[l.titulo] += 1
        ordenado = sorted(somas.items(), key=lambda par: par[1], reverse=True)
        return [(t, _arredonda(v), contagens[t]) for t, v in ordenado[:n]]


def _arredonda(valor: Decimal) -> Decimal:
    return valor.quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def ciclo_do_nome(nome: str) -> str:
    """``'Nubank_2026-10-25.csv'`` -> ``'2026-10'``.

    O dia é descartado sem nenhuma aritmética de mês: o ciclo é literalmente o
    ``YYYY-MM`` do nome do arquivo. Ver ``CLAUDE.md`` para por que o nome do
    anexo é mais confiável que as datas das linhas.
    """
    achado = RE_NOME_ANEXO.match(Path(nome).name)
    if achado is None:
        raise ValueError(
            f"nome de anexo fora do padrão Nubank_AAAA-MM-DD.csv: {nome!r}"
        )
    return f"{achado.group(1)}-{achado.group(2)}"


def _limpa(bruto: str) -> tuple[bool, str]:
    """Separa o sinal e tira moeda e espaços. ``"- 10.616,67"`` -> ``(True, '10.616,67')``."""
    texto = bruto.strip()
    if not texto:
        raise FaturaInvalida("valor vazio")
    negativo = texto.startswith("-")
    if negativo:
        texto = texto[1:]
    return negativo, texto.replace("R$", "").replace(" ", "").replace("\xa0", "")


def detecta_formato(amounts: Iterable[str]) -> str:
    """``'br'`` (vírgula decimal) ou ``'us'`` (ponto decimal).

    Os extratos antigos do Nubank usam formato americano (``12.10``); os atuais
    usam o brasileiro (``"- 10.616,67"``). Como um arquivo inteiro é de um
    formato só, decide por maioria dos centavos em vez de tentar adivinhar
    valor a valor — ``1.234`` sozinho é ambíguo, o arquivo todo não é.
    """
    br = us = 0
    for bruto in amounts:
        try:
            _, texto = _limpa(bruto)
        except FaturaInvalida:
            continue
        if RE_CENTAVOS_BR.search(texto):
            br += 1
        elif RE_CENTAVOS_US.search(texto):
            us += 1
    return "us" if us > br else "br"


def parse_valor(bruto: str, formato: str = "br") -> Decimal:
    """Converte valor para ``Decimal`` com 2 casas, no formato indicado.

    Rejeita agrupamento de milhar malformado em vez de remover os separadores
    e aceitar um número errado — corrupção silenciosa é o pior erro aqui.
    """
    negativo, texto = _limpa(bruto)

    if formato == "us":
        if not RE_VALOR_US.match(texto):
            raise FaturaInvalida(f"valor não reconhecido (formato us): {bruto!r}")
        texto = texto.replace(",", "")
    else:
        if not RE_VALOR_BR.match(texto):
            raise FaturaInvalida(f"valor não reconhecido (formato br): {bruto!r}")
        texto = texto.replace(".", "").replace(",", ".")

    try:
        valor = Decimal(texto)
    except InvalidOperation as exc:
        raise FaturaInvalida(f"valor não reconhecido: {bruto!r}") from exc

    return _arredonda(-valor if negativo else valor)


def parse_valor_br(bruto: str) -> Decimal:
    """Atalho para o formato brasileiro, que é o dos extratos atuais."""
    return parse_valor(bruto, "br")


def parse_data_iso(bruto: str) -> date:
    texto = bruto.strip()
    try:
        return date.fromisoformat(texto)
    except ValueError as exc:
        raise FaturaInvalida(f"data fora do formato AAAA-MM-DD: {bruto!r}") from exc


def _valida_cabecalho(nomes: Iterable[str] | None, origem: Path) -> None:
    if nomes is None:
        raise FaturaInvalida(f"{origem}: arquivo vazio ou sem cabeçalho")
    atual = tuple(n.strip().lstrip("﻿") for n in nomes)
    if atual[: len(COLUNAS_ESPERADAS)] != COLUNAS_ESPERADAS:
        raise FaturaInvalida(
            f"{origem}: cabeçalho inesperado {atual!r}; "
            f"esperado começar com {COLUNAS_ESPERADAS!r}"
        )


def parse_fatura_detalhado(
    path: str | Path,
    ciclo: str | None = None,
    regras: Regras | None = None,
) -> ResultadoParse:
    """Faz o parse completo, preservando o que foi descartado e por qual regra."""
    origem = Path(path)
    ciclo = ciclo or ciclo_do_nome(origem.name)
    regras = regras or carrega_regras()

    with origem.open(encoding="utf-8-sig", newline="") as fh:
        leitor = csv.DictReader(fh)
        _valida_cabecalho(leitor.fieldnames, origem)
        linhas = list(leitor)

    formato = detecta_formato((l.get("amount") or "") for l in linhas)
    resultado = ResultadoParse(ciclo=ciclo, origem=origem, formato=formato)

    for numero, linha in enumerate(linhas, start=2):
        titulo = (linha.get("title") or "").strip()
        if not titulo and not (linha.get("amount") or "").strip():
            continue  # linha em branco no fim do arquivo
        try:
            lancamento = Lancamento(
                ciclo=ciclo,
                data=parse_data_iso(linha.get("date") or ""),
                titulo=titulo,
                valor=parse_valor(linha.get("amount") or "", formato),
                parcela=extrai_parcela(titulo),
            )
        except FaturaInvalida as exc:
            raise FaturaInvalida(f"{origem}:{numero}: {exc}") from exc

        padrao = regras.motivo_para_ignorar(titulo)
        if padrao is None:
            resultado.mantidos.append(lancamento)
        else:
            resultado.descartados.append((lancamento, padrao))

    return resultado


def parse_fatura(path: str | Path, ciclo: str | None = None) -> list[dict[str, Any]]:
    """Função principal do módulo, conforme o contrato do ``CLAUDE.md``.

    Devolve os lançamentos normalizados e já filtrados, como dicts com as
    chaves ``ciclo``, ``data``, ``titulo``, ``valor`` e ``parcela``.
    """
    return [l.para_dict() for l in parse_fatura_detalhado(path, ciclo).mantidos]


def _json_default(obj: Any) -> str:
    if isinstance(obj, Decimal):
        return f"{obj:.2f}"
    raise TypeError(f"não serializável: {type(obj)!r}")


def _formata_brl(valor: Decimal) -> str:
    """Formato de leitura humana (BR), só para o relatório do terminal."""
    inteiro, _, centavos = f"{abs(valor):.2f}".partition(".")
    grupos = f"{int(inteiro):,}".replace(",", ".")
    sinal = "-" if valor < 0 else ""
    return f"{sinal}R$ {grupos},{centavos}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Faz o parse de um CSV de fatura do Nubank e resume o resultado."
    )
    parser.add_argument("csv", type=Path, help="caminho do CSV")
    parser.add_argument(
        "--ciclo",
        help="força o ciclo (padrão: derivado do nome do arquivo)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="ARQUIVO",
        help="grava os lançamentos normalizados em JSON",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        metavar="N",
        help="quantos comerciantes listar (0 desliga)",
    )
    args = parser.parse_args(argv)

    try:
        resultado = parse_fatura_detalhado(args.csv, args.ciclo)
    except (FaturaInvalida, ValueError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 4

    print(f"ciclo:        {resultado.ciclo}")
    print(f"arquivo:      {resultado.origem}")
    print(f"lançamentos:  {resultado.quantidade}")
    print(f"total:        {_formata_brl(resultado.total)}")
    print(f"descartados:  {len(resultado.descartados)}")
    somas = resultado.somas_descartadas
    for padrao, quantos in sorted(resultado.descartados_por_regra.items()):
        print(f"  {quantos:>4}x  {_formata_brl(somas[padrao]):>16}  {padrao}")

    residuo = resultado.residuo_iof
    if residuo:
        print(f"  resíduo de IOF sem par neste ciclo: {_formata_brl(residuo)}")

    parcelados = [l for l in resultado.mantidos if l.parcela]
    print(f"parcelados:   {len(parcelados)}")

    if args.top:
        print(f"\ntop {args.top} por comerciante:")
        for titulo, soma, ocorrencias in resultado.top_comerciantes(args.top):
            sufixo = f"  ({ocorrencias}x)" if ocorrencias > 1 else ""
            print(f"  {_formata_brl(soma):>16}  {titulo}{sufixo}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ciclo": resultado.ciclo,
            "origem": str(resultado.origem),
            "quantidade": resultado.quantidade,
            "total": resultado.total,
            "lancamentos": [l.para_dict() for l in resultado.mantidos],
        }
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        print(f"\njson gravado em {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
