"""Relatório em texto dos gastos, lendo a planilha `life`.

    .\\.venv\\Scripts\\python.exe scripts\\relatorio.py

Mesmos indicadores do dashboard, mas para ler no terminal ou colar no chat.
Só lê — quem escreve na planilha é só o processa_fatura.py.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_fatura import ciclo_do_nome, parse_fatura_detalhado
from processa_fatura import CABECALHO, LINHA_CABECALHO, autentica
from regras import carrega_assinaturas, e_assinatura

RAIZ = Path(__file__).resolve().parent.parent
RE_SUFIXO_PARCELA = re.compile(r"\s*-\s*Parcela \d+/\d+\s*$")

#: Comerciante com IOF cobrado = compra internacional. O IOF é filtrado antes de
#: chegar à planilha, então estas seções leem os CSVs originais de ``out/``.
RE_IOF = re.compile(r'^IOF de "(.+)"$')
PREFIXO_PIX = "Pix no Crédito"


def brl(valor: Decimal) -> str:
    inteiro, _, centavos = f"{abs(valor):.2f}".partition(".")
    grupos = f"{int(inteiro):,}".replace(",", ".")
    return f"{'-' if valor < 0 else ''}R$ {grupos},{centavos}"


def soma_meses(ciclo: str, n: int) -> str:
    ano, mes = (int(p) for p in ciclo.split("-"))
    total = (ano * 12 + mes - 1) + n
    return f"{total // 12}-{total % 12 + 1:02d}"


def le_planilha() -> list[dict]:
    cfg = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
    _, _, sheets, _ = autentica(Path(cfg["secrets_dir"]))
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

        internacionais = {
            achado.group(1)
            for lanc, _ in r.descartados
            if (achado := RE_IOF.match(lanc.titulo))
        }
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


def main() -> int:
    linhas = le_planilha()
    if not linhas:
        raise SystemExit("a planilha não tem lançamentos")
    ciclos = sorted({l["ciclo"] for l in linhas})
    print(f"# Fatura — {len(linhas)} lançamentos em {len(ciclos)} ciclos: {', '.join(ciclos)}")
    print("(já sem os ajustes: IOF, estornos, pagamento recebido)\n")
    secao_ciclos(linhas)
    secao_parcelamentos(linhas)
    secao_recorrentes(linhas)
    secao_csvs(RAIZ / "out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
