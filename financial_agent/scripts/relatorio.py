"""Relatório em texto das finanças, lendo a planilha `life`.

    .\\.venv\\Scripts\\python.exe scripts\\relatorio.py

Lê três abas: `Lançamentos` (fatura do cartão), `custos mensais no débito`
(custo fixo pago no débito) e `Receita` (em dólar). Só lê — quem escreve na
planilha é só o processa_fatura.py.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_fatura import ciclo_do_nome, parse_fatura_detalhado
from processa_fatura import CABECALHO, LINHA_CABECALHO, autentica
from regras import carrega_assinaturas, e_assinatura

RAIZ = Path(__file__).resolve().parent.parent
RE_SUFIXO_PARCELA = re.compile(r"\s*-\s*Parcela \d+/\d+\s*$")

#: Comerciante com IOF = compra internacional. O IOF é filtrado antes de chegar
#: à planilha, então estas seções leem os CSVs originais de ``out/``.
#:
#: São dois padrões porque a **cobrança** só traz o nome do comerciante no
#: formato antigo. O formato novo é ``IOF de compra internacional``, sem nome
#: nenhum — ver ``references/regras.md``. A **devolução** traz o nome em todos
#: os ciclos, e é ela que sustenta a seção quando a cobrança não serve.
RE_IOF_COBRANCA = re.compile(r'^IOF de "(.+)"$')
RE_IOF_DEVOLUCAO = re.compile(r"^IOF de volta de (.+)$")
PREFIXO_PIX = "Pix no Crédito"


def brl(valor: Decimal) -> str:
    inteiro, _, centavos = f"{abs(valor):.2f}".partition(".")
    grupos = f"{int(inteiro):,}".replace(",", ".")
    return f"{'-' if valor < 0 else ''}R$ {grupos},{centavos}"


def soma_meses(ciclo: str, n: int) -> str:
    ano, mes = (int(p) for p in ciclo.split("-"))
    total = (ano * 12 + mes - 1) + n
    return f"{total // 12}-{total % 12 + 1:02d}"


def carrega_config() -> dict:
    return json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))


def le_planilha(sheets, cfg: dict) -> list[dict]:
    aba = cfg["sheet_tab"]

    valores = sheets.spreadsheets().values().get(
        spreadsheetId=cfg["spreadsheet_id"], range=f"{aba}!A1:E"
    ).execute().get("values", [])

    if not valores or [c.strip() for c in (valores[0] + [""] * 5)[:5]] != CABECALHO:
        raise SystemExit(f"a aba '{aba}' não tem o layout esperado: {CABECALHO}")

    linhas = []
    for bruta in valores[LINHA_CABECALHO:]:
        c = (list(bruta) + [""] * 5)[:5]
        if not c[0].strip():
            continue
        linhas.append({
            "ciclo": c[0].strip(),
            "data": c[1].strip(),
            "titulo": c[2].strip(),
            # Formato da planilha: vírgula é milhar, ponto é decimal.
            "valor": Decimal(c[3].replace("R$", "").replace(",", "").strip() or "0"),
            "parcela": c[4].strip(),
            "base": RE_SUFIXO_PARCELA.sub("", c[2].strip()),
        })
    return linhas


def secao_ciclos(linhas: list[dict]) -> None:
    print("## Total por ciclo\n")
    totais: dict[str, Decimal] = defaultdict(Decimal)
    contagem: dict[str, int] = defaultdict(int)
    for l in linhas:
        totais[l["ciclo"]] += l["valor"]
        contagem[l["ciclo"]] += 1

    print(f"{'Ciclo':<10} {'Lanç.':>6} {'Total':>16} {'Variação':>16} {'%':>8}")
    anterior: Decimal | None = None
    for ciclo in sorted(totais):
        total = totais[ciclo]
        if anterior is None:
            var, pct = "—", "—"
        else:
            delta = total - anterior
            var = brl(delta)
            pct = f"{delta / anterior * 100:+.1f}%"
        print(f"{ciclo:<10} {contagem[ciclo]:>6} {brl(total):>16} {var:>16} {pct:>8}")
        anterior = total


def secao_parcelamentos(linhas: list[dict]) -> None:
    print("\n## Parcelamentos em aberto\n")
    ultimo = max(l["ciclo"] for l in linhas)
    abertos = []
    for l in linhas:
        if l["ciclo"] != ultimo or not l["parcela"]:
            continue
        atual, total_p = (int(p) for p in l["parcela"].split("/"))
        restam = total_p - atual
        if restam > 0:
            abertos.append({**l, "restam": restam, "falta": restam * l["valor"]})

    if not abertos:
        print(f"Nenhum parcelamento ativo no ciclo {ultimo}.")
        return

    falta_total = sum((a["falta"] for a in abertos), Decimal(0))
    print(f"Falta pagar: {brl(falta_total)} em {len(abertos)} parcelamentos\n")
    print(f"{'Compra':<40} {'Parc.':>7} {'Por mês':>12} {'Falta':>13} {'Última':>9}")
    for a in sorted(abertos, key=lambda x: x["falta"], reverse=True):
        print(
            f"{a['base'][:40]:<40} {a['parcela']:>7} {brl(a['valor']):>12} "
            f"{brl(a['falta']):>13} {soma_meses(ultimo, a['restam']):>9}"
        )

    futuro: dict[str, Decimal] = defaultdict(Decimal)
    for a in abertos:
        for k in range(1, a["restam"] + 1):
            futuro[soma_meses(ultimo, k)] += a["valor"]
    print("\nCompromisso por ciclo futuro:")
    for ciclo in sorted(futuro):
        print(f"  {ciclo}  {brl(futuro[ciclo]):>12}")


def secao_recorrentes(linhas: list[dict], top: int = 15) -> None:
    ciclos = sorted({l["ciclo"] for l in linhas})
    print(f"\n## Gastos recorrentes ({len(ciclos)} ciclos carregados)\n")

    por_titulo: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for l in linhas:
        if not l["parcela"]:
            por_titulo[l["base"]][l["ciclo"]] += l["valor"]

    recorrentes = [
        (titulo, len(ciclos_do_titulo), sum(ciclos_do_titulo.values()) / len(ciclos_do_titulo))
        for titulo, ciclos_do_titulo in por_titulo.items()
        if len(ciclos_do_titulo) >= 2
    ]
    if not recorrentes:
        print("Nenhum título aparece em dois ou mais ciclos ainda.")
        return

    # Em todos os ciclos = assinatura de verdade; nos demais, comerciante repetido.
    fixos = [r for r in recorrentes if r[1] == len(ciclos)]
    eventuais = [r for r in recorrentes if r[1] < len(ciclos)]

    print(f"Em todos os {len(ciclos)} ciclos ({len(fixos)} títulos) — assinaturas:")
    for titulo, _, media in sorted(fixos, key=lambda r: r[2], reverse=True)[:top]:
        print(f"  {titulo[:44]:<44} {brl(Decimal(media)):>12} /ciclo")
    print(f"  {'soma':<44} {brl(sum((Decimal(r[2]) for r in fixos), Decimal(0))):>12} /ciclo")

    print(f"\nEm parte dos ciclos ({len(eventuais)} títulos) — comerciantes repetidos:")
    for titulo, n, media in sorted(eventuais, key=lambda r: r[2], reverse=True)[:top]:
        print(f"  {titulo[:40]:<40} {n}/{len(ciclos)} {brl(Decimal(media)):>12} /ciclo")


def comerciantes_internacionais(resultado) -> set[str]:
    """Comerciantes com IOF no ciclo — a única pista de compra fora do país.

    Lê as **duas pontas** do par de IOF, porque nenhuma sozinha cobre todos os
    ciclos: a cobrança só traz o nome no formato antigo (``IOF de "X"``), e o
    formato novo (``IOF de compra internacional``) não traz nome nenhum. A
    devolução (``IOF de volta de X``) traz em todos.

    Medido nos quatro ciclos em que as duas fontes existem (2026-07 a 2026-10),
    o percentual de exterior sai idêntico pelos dois caminhos — é o que mostra
    que somar a devolução não distorce. Nos ciclos de formato novo só ela
    produz número: em 2026-11 a seção dava 0%, e o real é 87,8%.

    Ressalva: a devolução costuma cair um ciclo depois da compra, então um
    comerciante pode ser contado no ciclo da devolução. O resíduo de IOF por
    ciclo (``ResultadoParse.residuo_iof``) mede esse descasamento e é de
    centavos nos ciclos medidos.
    """
    nomes: set[str] = set()
    for lanc, _ in resultado.descartados:
        for padrao in (RE_IOF_COBRANCA, RE_IOF_DEVOLUCAO):
            if achado := padrao.match(lanc.titulo):
                nomes.add(achado.group(1))
    return nomes


def secao_csvs(pasta: Path) -> None:
    """Indicadores que só os CSVs originais permitem calcular.

    O Pix no crédito não é consumo no cartão, e o IOF — única pista de compra
    internacional — é filtrado antes de chegar à planilha. Por isso esta seção
    lê ``out/``, e fica silenciosa se os arquivos não estiverem lá.
    """
    arquivos = sorted(pasta.glob("Nubank_*.csv"))
    if not arquivos:
        print(f"\n(sem CSVs em {pasta}; rode processa_fatura.py para baixá-los)")
        return

    chaves = carrega_assinaturas()
    print("\n## Consumo, Pix no crédito e exterior\n")
    print(f"{'Ciclo':<10} {'Consumo':>14} {'Pix crédito':>14} {'Exterior':>10}")

    assinaturas: dict[str, dict] = {}
    for caminho in arquivos:
        ciclo = ciclo_do_nome(caminho.name)
        r = parse_fatura_detalhado(caminho, ciclo)

        internacionais = comerciantes_internacionais(r)
        pix = consumo = intl = Decimal(0)
        for l in r.mantidos:
            if l.titulo.startswith(PREFIXO_PIX):
                pix += l.valor
                continue
            consumo += l.valor
            if l.titulo in internacionais:
                intl += l.valor
            base = RE_SUFIXO_PARCELA.sub("", l.titulo)
            if e_assinatura(base, chaves):
                reg = assinaturas.setdefault(base, {"total": Decimal(0), "ciclos": set()})
                reg["total"] += l.valor
                reg["ciclos"].add(ciclo)

        pct = f"{intl / consumo * 100:.0f}%" if consumo else "—"
        print(f"{ciclo:<10} {brl(consumo):>14} {brl(pix):>14} {pct:>10}")

    n_ciclos = len(arquivos)
    print(f"\n## Assinaturas ({n_ciclos} ciclos)\n")
    print(f"{'Serviço':<30} {'Ciclos':>7} {'Média/ciclo':>14}")
    fixas = Decimal(0)
    ordenadas = sorted(assinaturas.items(), key=lambda p: -p[1]["total"] / len(p[1]["ciclos"]))
    for nome, reg in ordenadas:
        n = len(reg["ciclos"])
        media = reg["total"] / n
        print(f"{nome[:30]:<30} {f'{n}/{n_ciclos}':>7} {brl(media):>14}")
        if n >= n_ciclos - 1:  # presente em quase todos = custo fixo
            fixas += media
    print(f"\nFixas (em {n_ciclos - 1} ciclos ou mais): {brl(fixas)} por ciclo")


# ----------------------------------------- abas de custo no débito e receita

#: Cabeçalhos exigidos nas abas novas. Como em ``Lançamentos``, o layout é
#: conferido antes de ler: coluna trocada à mão viraria número errado, em
#: silêncio, numa aba que ninguém revisa.
CABECALHO_CUSTOS = ["Mes", "Nome do gasto", "total"]
CABECALHO_RECEITA = ["Mes", "empresa", "valor em dolar"]

#: Rótulos das taxas de câmbio, que moram na linha 1 da aba de custos ao lado
#: dos dados. A planilha não guarda US$ -> R$ direto; guarda as duas pontas
#: para euro, e a taxa sai da razão entre elas.
ROTULO_BRL_EUR = "R$ to EU"
ROTULO_USD_EUR = "$ to EU"

#: Mês digitado à mão. Já aparece em três formatos na planilha (``Nov - 2026``
#: na aba de custos, ``Nov 2026`` na de receita, ``2024-Apr`` na de
#: investimentos), e abreviação em português é questão de tempo.
MESES = {
    "jan": 1, "january": 1, "janeiro": 1,
    "feb": 2, "february": 2, "fev": 2, "fevereiro": 2,
    "mar": 3, "march": 3, "março": 3, "marco": 3,
    "apr": 4, "april": 4, "abr": 4, "abril": 4,
    "may": 5, "mai": 5, "maio": 5,
    "jun": 6, "june": 6, "junho": 6,
    "jul": 7, "july": 7, "julho": 7,
    "aug": 8, "august": 8, "ago": 8, "agosto": 8,
    "sep": 9, "sept": 9, "september": 9, "set": 9, "setembro": 9,
    "oct": 10, "october": 10, "out": 10, "outubro": 10,
    "nov": 11, "november": 11, "novembro": 11,
    "dec": 12, "december": 12, "dez": 12, "dezembro": 12,
}
RE_CICLO = re.compile(r"^(\d{4})-(\d{2})$")
#: Um ano de 4 dígitos ou uma palavra. Pontuação (o ``-`` de ``Nov - 2026``)
#: fica de fora de propósito, para não virar token desconhecido.
RE_TOKEN_MES = re.compile(r"\d{4}|[^\W\d_]+")
#: Ponto seguido de exatamente 3 dígitos no fim de um valor com símbolo de
#: moeda: ``R$ 3.819`` é milhar em BR e decimal em US, e os dois formatos
#: convivem nesta planilha. Ambíguo não vira palpite — vira erro.
RE_MILHAR_AMBIGUO = re.compile(r"\.\d{3}$")


class ValorIlegivel(ValueError):
    """Célula de valor que não dá para converter sem adivinhar."""


def mes_para_ciclo(texto: str) -> str | None:
    """``'Nov - 2026'`` -> ``'2026-11'``; ``None`` quando não dá para ler.

    Lê por tokens (um ano de 4 dígitos e um nome de mês) em vez de exigir um
    formato fixo, porque o campo é digitado à mão em abas diferentes. O que não
    casar devolve ``None`` e a linha sai listada como problema — nunca é
    atribuída a um mês chutado.
    """
    texto = texto.strip()
    if achado := RE_CICLO.match(texto):
        return texto if 1 <= int(achado.group(2)) <= 12 else None

    ano = mes = None
    for token in RE_TOKEN_MES.findall(texto):
        if token.isdigit():
            if ano is not None:
                return None
            ano = int(token)
        elif (numero := MESES.get(token.lower())) is not None:
            if mes is not None:
                return None
            mes = numero
        else:
            return None
    return f"{ano}-{mes:02d}" if ano is not None and mes is not None else None


def valor_us(texto: str) -> Decimal:
    """``'R$ 3,819'`` -> ``Decimal('3819')``. Formato da planilha, não do CSV.

    Na planilha a vírgula é milhar e o ponto é decimal — o inverso do CSV do
    Nubank (ver CLAUDE.md). Célula vazia vale zero; o resto levanta
    :class:`ValorIlegivel`, inclusive o milhar ambíguo de ``R$ 3.819``, que em
    silêncio viraria um valor mil vezes menor.
    """
    tem_simbolo = "R$" in texto or "$" in texto
    limpo = texto.replace("R$", "").replace("$", "").replace(",", "").strip()
    if not limpo:
        return Decimal(0)
    if tem_simbolo and "," not in texto and RE_MILHAR_AMBIGUO.search(limpo):
        raise ValorIlegivel(
            f"{texto!r}: '.' seguido de 3 dígitos é milhar em BR e decimal em US"
        )
    try:
        return Decimal(limpo)
    except InvalidOperation as exc:
        raise ValorIlegivel(f"{texto!r} não é um número") from exc


def arredonda(valor: Decimal) -> Decimal:
    """2 casas com ``ROUND_HALF_UP``, como manda a convenção do projeto."""
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def usd(valor: Decimal) -> str:
    """``Decimal('4000')`` -> ``'US$ 4,000.00'``, no estilo da própria coluna."""
    return "US$ {:,.2f}".format(valor)


def le_aba(sheets, sid: str, aba: str, colunas: str) -> list[list[str]] | None:
    """Valores de ``aba!A1:<colunas>``, ou ``None`` se a aba não existir.

    Aba ausente não é erro fatal: as seções novas se calam e o resto do
    relatório continua. O usuário pode ter renomeado ou ainda não ter criado.
    """
    from googleapiclient.errors import HttpError

    try:
        return sheets.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{aba}'!A1:{colunas}"
        ).execute().get("values", [])
    except HttpError as exc:
        if exc.resp.status == 400:
            return None
        raise


def _confere_cabecalho(valores: list[list[str]], esperado: list[str]) -> str | None:
    n = len(esperado)
    achado = [c.strip() for c in (list(valores[0] if valores else []) + [""] * n)[:n]]
    if achado != esperado:
        return f"cabeçalho inesperado: esperado {esperado}, encontrado {achado}"
    return None


def parse_custos(valores: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Linhas da aba de custos no débito, mais a lista de problemas.

    Devolve ``(linhas, problemas)`` em vez de abortar: a aba é digitada à mão e
    uma célula torta não deve derrubar o relatório. O que não deu para ler sai
    impresso no fim da seção — nunca somado como zero, que é o jeito de um
    gasto desaparecer sem ninguém notar.
    """
    if erro := _confere_cabecalho(valores, CABECALHO_CUSTOS):
        return [], [erro]

    linhas: list[dict] = []
    problemas: list[str] = []
    for numero, bruta in enumerate(valores[1:], start=2):
        c = (list(bruta) + [""] * 3)[:3]
        if not any(x.strip() for x in c):
            continue
        ciclo = mes_para_ciclo(c[0])
        if ciclo is None:
            problemas.append(f"linha {numero}: mês ilegível {c[0]!r}")
            continue
        try:
            valor = valor_us(c[2])
        except ValorIlegivel as exc:
            problemas.append(f"linha {numero}: {exc}")
            continue
        linhas.append({
            "ciclo": ciclo,
            "nome": c[1].strip() or "(sem nome)",
            "valor": arredonda(valor),
        })
    return linhas, problemas


def parse_receita(valores: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Linhas da aba de receita, em dólar, mais a lista de problemas."""
    if erro := _confere_cabecalho(valores, CABECALHO_RECEITA):
        return [], [erro]

    linhas: list[dict] = []
    problemas: list[str] = []
    for numero, bruta in enumerate(valores[1:], start=2):
        c = (list(bruta) + [""] * 3)[:3]
        if not any(x.strip() for x in c):
            continue
        ciclo = mes_para_ciclo(c[0])
        if ciclo is None:
            problemas.append(f"linha {numero}: mês ilegível {c[0]!r}")
            continue
        try:
            valor = valor_us(c[2])
        except ValorIlegivel as exc:
            problemas.append(f"linha {numero}: {exc}")
            continue
        linhas.append({
            "ciclo": ciclo,
            "empresa": c[1].strip() or "(sem empresa)",
            "usd": arredonda(valor),
        })
    return linhas, problemas


def taxa_usd_brl(valores: list[list[str]]) -> tuple[Decimal, Decimal, Decimal] | None:
    """``(usd_brl, brl_eur, usd_eur)`` lido da linha 2 da aba de custos.

    A planilha só guarda as duas taxas para euro, então US$ -> R$ sai da razão
    entre elas. Procura pelo **rótulo** na linha 1, não por coluna fixa: essas
    células convivem com os dados e já mudariam de lugar com uma coluna nova.
    Sem as duas taxas devolve ``None``, e a receita aparece só em dólar.
    """
    if len(valores) < 2:
        return None
    cabecalho = [c.strip() for c in valores[0]]
    dados = list(valores[1])
    try:
        brl_eur = valor_us(dados[cabecalho.index(ROTULO_BRL_EUR)])
        usd_eur = valor_us(dados[cabecalho.index(ROTULO_USD_EUR)])
    except (ValueError, IndexError):
        return None
    if brl_eur <= 0 or usd_eur <= 0:
        return None
    return usd_eur / brl_eur, brl_eur, usd_eur


def monta_saldo(
    fatura_por_ciclo: dict[str, Decimal],
    custos: list[dict],
    receitas: list[dict],
    usd_brl: Decimal | None,
) -> list[dict]:
    """Uma linha por mês que tem receita ou custo no débito.

    **Supõe que o ciclo da fatura é o mês do débito** — o ciclo ``2026-11``
    fecha em novembro e é o que se paga em novembro, embora contenha compras de
    outubro. É a mesma aproximação que o usuário faz à mão na planilha.

    Mês sem receita fica com ``saldo = None`` em vez de um negativo gigante:
    receita ausente é dado que falta, não receita zero. Os meses futuros da aba
    de custos (planejamento) caem justamente nesse caso.
    """
    por_ciclo: dict[str, dict] = {}
    for l in custos:
        reg = por_ciclo.setdefault(l["ciclo"], {})
        reg["custo"] = reg.get("custo", Decimal(0)) + l["valor"]
    for l in receitas:
        reg = por_ciclo.setdefault(l["ciclo"], {})
        reg["usd"] = reg.get("usd", Decimal(0)) + l["usd"]

    saldo: list[dict] = []
    for ciclo in sorted(por_ciclo):
        reg = por_ciclo[ciclo]
        custo = reg.get("custo")
        bruto_usd = reg.get("usd")
        receita = (
            arredonda(bruto_usd * usd_brl)
            if bruto_usd is not None and usd_brl is not None
            else None
        )
        fatura = fatura_por_ciclo.get(ciclo)
        total_saidas = (custo or Decimal(0)) + (fatura or Decimal(0))
        saldo.append({
            "ciclo": ciclo,
            "receita_usd": bruto_usd,
            "receita": receita,
            "custo": custo,
            "fatura": fatura,
            "saldo": receita - total_saidas if receita is not None else None,
        })
    return saldo


def _imprime_problemas(problemas: list[str]) -> None:
    if problemas:
        print(f"\n  {len(problemas)} linha(s) não lida(s):")
        for p in problemas:
            print(f"    {p}")


def secao_custos_debito(custos: list[dict], problemas: list[str], aba: str, meses: int = 6) -> None:
    print(f"\n## Custo fixo no débito (aba '{aba}')\n")
    if not custos:
        print("Nenhuma linha legível.")
        _imprime_problemas(problemas)
        return

    ciclos = sorted({l["ciclo"] for l in custos})[-meses:]
    total: dict[str, Decimal] = defaultdict(Decimal)
    contagem: dict[str, int] = defaultdict(int)
    por_nome: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for l in custos:
        total[l["ciclo"]] += l["valor"]
        contagem[l["ciclo"]] += 1
        por_nome[l["nome"]][l["ciclo"]] += l["valor"]

    print(f"{'Mês':<10} {'Gastos':>7} {'Total':>16} {'Variação':>16} {'%':>8}")
    anterior: Decimal | None = None
    for ciclo in ciclos:
        if anterior is None:
            var, pct = "—", "—"
        else:
            delta = total[ciclo] - anterior
            var, pct = brl(delta), f"{delta / anterior * 100:+.1f}%"
        print(f"{ciclo:<10} {contagem[ciclo]:>7} {brl(total[ciclo]):>16} {var:>16} {pct:>8}")
        anterior = total[ciclo]

    print(f"\nPor gasto:\n{'Gasto':<24}" + "".join(f"{c:>14}" for c in ciclos))
    ordenados = sorted(por_nome.items(), key=lambda p: -sum(p[1].values()))
    for nome, valores in ordenados:
        celulas = "".join(
            f"{brl(valores[c]) if c in valores else '—':>14}" for c in ciclos
        )
        print(f"{nome[:24]:<24}{celulas}")
    _imprime_problemas(problemas)


def secao_receita(
    receitas: list[dict], problemas: list[str], aba: str, cambio, meses: int = 6
) -> None:
    print(f"\n## Receita (aba '{aba}')\n")
    if cambio:
        usd_brl, brl_eur, usd_eur = cambio
        print(
            f"Câmbio: US$ 1 = {brl(arredonda(usd_brl))} "
            f"(razão das taxas para euro da aba de custos: {usd_eur} / {brl_eur})\n"
        )
    else:
        print("Sem as taxas de câmbio na aba de custos — só o valor em dólar.\n")

    if not receitas:
        print("Nenhuma linha legível.")
        _imprime_problemas(problemas)
        return

    ciclos = sorted({l["ciclo"] for l in receitas})[-meses:]
    print(f"{'Mês':<10} {'Empresa':<24} {'Em US$':>16} {'Em R$':>16}")
    for ciclo in ciclos:
        do_ciclo = [l for l in receitas if l["ciclo"] == ciclo]
        for l in sorted(do_ciclo, key=lambda x: -x["usd"]):
            em_brl = brl(arredonda(l["usd"] * cambio[0])) if cambio else "—"
            print(f"{ciclo:<10} {l['empresa'][:24]:<24} {usd(l['usd']):>16} {em_brl:>16}")
        if len(do_ciclo) > 1:
            soma = sum((l["usd"] for l in do_ciclo), Decimal(0))
            em_brl = brl(arredonda(soma * cambio[0])) if cambio else "—"
            print(f"{'':<10} {'total do mês':<24} {usd(soma):>16} {em_brl:>16}")
    _imprime_problemas(problemas)


def secao_saldo(saldo: list[dict]) -> None:
    print("\n## Saldo mensal (receita − débito − fatura)\n")
    if not saldo:
        print("Sem receita nem custo no débito para cruzar.")
        return

    print("O ciclo da fatura é tratado como o mês em que ela é paga.\n")
    print(f"{'Mês':<10} {'Receita':>16} {'Débito':>16} {'Fatura':>16} {'Saldo':>16}")
    for l in saldo:
        print(
            f"{l['ciclo']:<10} "
            f"{(brl(l['receita']) if l['receita'] is not None else '—'):>16} "
            f"{(brl(l['custo']) if l['custo'] is not None else '—'):>16} "
            f"{(brl(l['fatura']) if l['fatura'] is not None else '—'):>16} "
            f"{(brl(l['saldo']) if l['saldo'] is not None else '—'):>16}"
        )

    faltantes = [
        f"  {l['ciclo']}: sem " + ", ".join(
            nome for nome, valor in (
                ("receita", l["receita"]), ("custo no débito", l["custo"]),
                ("fatura", l["fatura"]),
            ) if valor is None
        )
        for l in saldo
        if l["receita"] is None or l["custo"] is None or l["fatura"] is None
    ]
    if faltantes:
        print("\nMeses incompletos (saldo sai '—' quando falta a receita):")
        print("\n".join(faltantes))


def main() -> int:
    cfg = carrega_config()
    _, _, sheets, _ = autentica(Path(cfg["secrets_dir"]))
    sid = cfg["spreadsheet_id"]

    linhas = le_planilha(sheets, cfg)
    if not linhas:
        raise SystemExit("a planilha não tem lançamentos")
    ciclos = sorted({l["ciclo"] for l in linhas})
    print(f"# Fatura — {len(linhas)} lançamentos em {len(ciclos)} ciclos: {', '.join(ciclos)}")
    print("(já sem os ajustes: IOF, estornos, pagamento recebido)\n")
    secao_ciclos(linhas)
    secao_parcelamentos(linhas)
    secao_recorrentes(linhas)
    secao_csvs(RAIZ / "out")

    aba_custos, aba_receita = cfg["tab_custos_debito"], cfg["tab_receita"]
    # A..F na aba de custos: as colunas E e F guardam as taxas de câmbio.
    brutos_custos = le_aba(sheets, sid, aba_custos, "F")
    brutos_receita = le_aba(sheets, sid, aba_receita, "C")

    if brutos_custos is None:
        print(f"\n(a aba '{aba_custos}' não existe nesta planilha)")
        custos, problemas_custos, cambio = [], [], None
    else:
        custos, problemas_custos = parse_custos(brutos_custos)
        cambio = taxa_usd_brl(brutos_custos)
        secao_custos_debito(custos, problemas_custos, aba_custos)

    if brutos_receita is None:
        print(f"\n(a aba '{aba_receita}' não existe nesta planilha)")
        receitas = []
    else:
        receitas, problemas_receita = parse_receita(brutos_receita)
        secao_receita(receitas, problemas_receita, aba_receita, cambio)

    fatura_por_ciclo: dict[str, Decimal] = defaultdict(Decimal)
    for l in linhas:
        fatura_por_ciclo[l["ciclo"]] += l["valor"]
    secao_saldo(monta_saldo(fatura_por_ciclo, custos, receitas, cambio[0] if cambio else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
