"""Carrega as regras de negócio de ``references/regras.md``.

A lógica de negócio vive no markdown, não aqui: este módulo só lê, valida e
compila. Ver ``references/regras.md`` para o formato esperado dos bullets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Seções do markdown de onde as regras são lidas.
SECAO_IGNORADOS = "Padrões ignorados"
SECAO_ASSINATURAS = "Assinaturas"

#: Captura o primeiro trecho entre backticks de um bullet markdown.
_BULLET = re.compile(r"^\s*[-*]\s+`([^`]+)`")

#: Regex de parcelamento. Fixo no código de propósito: é a estrutura do dado,
#: não uma regra de negócio ajustável.
RE_PARCELA = re.compile(r"Parcela (\d+/\d+)")


class RegrasInvalidas(ValueError):
    """O arquivo de regras não pôde ser lido ou contém um regex inválido."""


@dataclass(frozen=True)
class Regra:
    """Um padrão a ignorar, com a origem para mensagens de erro."""

    padrao: str
    regex: re.Pattern[str]
    linha: int

    def casa(self, titulo: str) -> bool:
        return self.regex.search(titulo) is not None


@dataclass(frozen=True)
class Regras:
    """Conjunto de padrões a ignorar, carregado do markdown."""

    ignorados: tuple[Regra, ...]
    origem: Path

    def motivo_para_ignorar(self, titulo: str) -> str | None:
        """Devolve o padrão que fez o título ser ignorado, ou ``None``."""
        for regra in self.ignorados:
            if regra.casa(titulo):
                return regra.padrao
        return None

    def deve_ignorar(self, titulo: str) -> bool:
        return self.motivo_para_ignorar(titulo) is not None


def caminho_padrao() -> Path:
    """``references/regras.md`` relativo à raiz do projeto."""
    return Path(__file__).resolve().parent.parent / "references" / "regras.md"


def _extrai_secao(texto: str, titulo: str) -> list[tuple[int, str]]:
    """Linhas (1-based) da seção ``## <titulo>``, até o próximo cabeçalho."""
    linhas = texto.splitlines()
    inicio: int | None = None
    for i, linha in enumerate(linhas):
        if linha.strip().startswith("##") and linha.strip().lstrip("#").strip() == titulo:
            inicio = i + 1
            break
    if inicio is None:
        raise RegrasInvalidas(f"seção '## {titulo}' não encontrada")

    coletadas: list[tuple[int, str]] = []
    for i in range(inicio, len(linhas)):
        if linhas[i].strip().startswith("##"):
            break
        coletadas.append((i + 1, linhas[i]))
    return coletadas


def carrega_regras(caminho: Path | None = None) -> Regras:
    """Lê e compila os padrões a ignorar.

    Levanta :class:`RegrasInvalidas` se o arquivo não existir, se a seção
    estiver ausente ou vazia, ou se algum regex não compilar.
    """
    caminho = caminho or caminho_padrao()
    try:
        texto = caminho.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegrasInvalidas(f"não foi possível ler {caminho}: {exc}") from exc

    regras: list[Regra] = []
    for numero, linha in _extrai_secao(texto, SECAO_IGNORADOS):
        achado = _BULLET.match(linha)
        if achado is None:
            continue
        padrao = achado.group(1)
        try:
            compilado = re.compile(padrao)
        except re.error as exc:
            raise RegrasInvalidas(
                f"{caminho}:{numero}: regex inválido {padrao!r}: {exc}"
            ) from exc
        regras.append(Regra(padrao=padrao, regex=compilado, linha=numero))

    if not regras:
        raise RegrasInvalidas(
            f"{caminho}: a seção '## {SECAO_IGNORADOS}' não tem nenhum padrão"
        )
    return Regras(ignorados=tuple(regras), origem=caminho)


def carrega_assinaturas(caminho: Path | None = None) -> tuple[str, ...]:
    """Palavras-chave que marcam um comerciante como assinatura digital.

    Comparadas por substring em minúsculas — ver ``references/regras.md``.
    """
    caminho = caminho or caminho_padrao()
    try:
        texto = caminho.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegrasInvalidas(f"não foi possível ler {caminho}: {exc}") from exc

    chaves = [
        achado.group(1).lower()
        for _, linha in _extrai_secao(texto, SECAO_ASSINATURAS)
        if (achado := _BULLET.match(linha))
    ]
    if not chaves:
        raise RegrasInvalidas(
            f"{caminho}: a seção '## {SECAO_ASSINATURAS}' não tem nenhuma palavra-chave"
        )
    return tuple(chaves)


def e_assinatura(titulo: str, chaves: tuple[str, ...]) -> bool:
    minusculo = titulo.lower()
    return any(chave in minusculo for chave in chaves)


def extrai_parcela(titulo: str) -> str | None:
    """``'Tap Web Vip - Parcela 4/10'`` -> ``'4/10'``; sem parcela -> ``None``."""
    achado = RE_PARCELA.search(titulo)
    return achado.group(1) if achado else None


if __name__ == "__main__":
    regras = carrega_regras()
    print(f"{len(regras.ignorados)} padrões carregados de {regras.origem}")
    for regra in regras.ignorados:
        print(f"  linha {regra.linha}: {regra.padrao}")
