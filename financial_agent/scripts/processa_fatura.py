"""Gmail -> Drive -> Sheets: arquiva o CSV da fatura e carrega os lançamentos.

    python scripts/processa_fatura.py              # mostra o que faria
    python scripts/processa_fatura.py --apply      # faz

Sem --apply nada é escrito. Ver CLAUDE.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_fatura import RE_NOME_ANEXO, ciclo_do_nome, parse_fatura_detalhado

RAIZ = Path(__file__).resolve().parent.parent

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Evita baixar o corpo HTML dos e-mails do Nubank (passa de 250 KB) só para
# descobrir o nome dos anexos.
CAMPOS = (
    "id,internalDate,payload(parts(filename,body/attachmentId,"
    "parts(filename,body/attachmentId)))"
)


def aviso(*partes: object) -> None:
    """Mensagens vão para stderr; stdout fica livre para o resumo."""
    print(*partes, file=sys.stderr)


# ---------------------------------------------------------------- auth

def autentica(secrets_dir: Path):
    """Credenciais com cache em token.json, reconsentindo quando expira.

    No modo *Testing* do Google o refresh token morre em 7 dias, então o
    consentimento no navegador reaparece em praticamente toda execução mensal.
    É esperado, não é bug.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = secrets_dir / "token.json"
    segredo = secrets_dir / "client_secret.json"
    cred = None

    if token.exists():
        cred = Credentials.from_authorized_user_file(str(token), SCOPES)
        if cred.expired and cred.refresh_token:
            try:
                cred.refresh(Request())
            except RefreshError:
                aviso("token expirado, pedindo consentimento de novo")
                cred = None

    if cred is None or not cred.valid:
        if not segredo.exists():
            raise SystemExit(
                f"falta {segredo}\nVer a seção 'Integrações Google' do CLAUDE.md "
                "para o setup no Google Cloud."
            )
        cred = InstalledAppFlow.from_client_secrets_file(str(segredo), SCOPES).run_local_server(port=0)
        secrets_dir.mkdir(parents=True, exist_ok=True)
        token.write_text(cred.to_json(), encoding="utf-8")

    if not cred.has_scopes(SCOPES):
        token.unlink(missing_ok=True)
        raise SystemExit(
            "consentimento incompleto. Rode de novo e mantenha todas as caixas "
            "de permissão marcadas na tela do Google."
        )

    from googleapiclient.discovery import build

    opts = {"credentials": cred, "cache_discovery": False}
    return (
        build("gmail", "v1", **opts),
        build("drive", "v3", **opts),
        build("sheets", "v4", **opts),
        cred,
    )


# ---------------------------------------------------------------- gmail

def _parts(part: dict):
    if part:
        yield part
        for filho in part.get("parts") or ():
            yield from _parts(filho)


def busca_anexos(gmail, remetente: str) -> dict[str, tuple[str, str, str]]:
    """``{ciclo: (message_id, attachment_id, nome_do_arquivo)}``, só CSV.

    Quando o mesmo arquivo aparece em vários e-mails (o Nubank reenvia), vence o
    mais recente — por isso a varredura vai do mais antigo para o mais novo.
    """
    query = f"from:{remetente} has:attachment filename:csv"
    msgs = gmail.users().messages().list(userId="me", q=query, maxResults=60).execute()
    refs = msgs.get("messages") or []

    achados = []
    for ref in refs:
        msg = gmail.users().messages().get(
            userId="me", id=ref["id"], format="full", fields=CAMPOS
        ).execute()
        for part in _parts(msg.get("payload") or {}):
            nome = part.get("filename") or ""
            anexo_id = (part.get("body") or {}).get("attachmentId")
            if anexo_id and RE_NOME_ANEXO.match(nome):
                achados.append((int(msg.get("internalDate", 0)), nome, msg["id"], anexo_id))

    ciclos: dict[str, tuple[str, str, str]] = {}
    for _, nome, msg_id, anexo_id in sorted(achados):
        ciclos[ciclo_do_nome(nome)] = (msg_id, anexo_id, nome)
    return ciclos


def baixa(gmail, anexo: tuple[str, str, str], pasta: Path) -> Path:
    """Baixa preservando o nome original do anexo."""
    msg_id, anexo_id, nome = anexo
    corpo = gmail.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=anexo_id
    ).execute()
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / nome
    destino.write_bytes(base64.urlsafe_b64decode(corpo["data"]))
    return destino


# ---------------------------------------------------------------- drive

def sobe(drive, folder_id: str, caminho: Path, aplicar: bool) -> str:
    """Sobe o CSV se ainda não existir na pasta. Nunca sobrescreve."""
    from googleapiclient.http import MediaFileUpload

    nome = caminho.name.replace("'", "\\'")
    achados = drive.files().list(
        q=f"name = '{nome}' and '{folder_id}' in parents and trashed = false",
        fields="files(id)",
    ).execute().get("files") or []
    if achados:
        return "já estava lá"
    if not aplicar:
        return "subiria"

    drive.files().create(
        body={"name": caminho.name, "parents": [folder_id]},
        media_body=MediaFileUpload(str(caminho), mimetype="text/csv"),
        fields="id",
    ).execute()
    return "subiu"


# ---------------------------------------------------------------- sheets

def formata_valor(valor: Decimal) -> str:
    """``Decimal('20075.5')`` -> ``'R$ 20,075.50'``, igual às linhas que já existem."""
    return "R$ {:,.2f}".format(valor)


CABECALHO = ["Mês fatura", "Data", "Descrição", "Valor (R$)", "Parcela"]
LINHA_CABECALHO = 1
PRIMEIRA_LINHA = 2


def atualiza_planilha(
    sheets, sid: str, aba: str, ciclo: str, lancamentos, aplicar: bool, backups: Path
) -> dict:
    """Substitui as linhas do ciclo reescrevendo o bloco inteiro.

    Reescrever tudo em vez de apagar linhas é deliberado: num ``batchUpdate`` os
    deletes deslocam os índices das requests seguintes, e esse bug apaga dados
    sem ninguém notar. Aqui a única aritmética é ``PRIMEIRA_LINHA + len(linhas)``.
    """
    propriedades = next(
        f["properties"]
        for f in sheets.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties"
        ).execute()["sheets"]
        if f["properties"]["title"] == aba
    )
    total_grid = propriedades["gridProperties"]["rowCount"]

    tabela = sheets.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{aba}!A1:E{total_grid}"
    ).execute().get("values", [])
    tabela = [(linha + [""] * 5)[:5] for linha in tabela]

    cabecalho = [c.strip() for c in tabela[LINHA_CABECALHO - 1]] if tabela else []
    if cabecalho != CABECALHO:
        raise SystemExit(
            f"a aba '{aba}' não tem o layout esperado.\n"
            f"  esperado na linha {LINHA_CABECALHO}: {CABECALHO}\n"
            f"  encontrado:            {cabecalho}"
        )

    antigas = [l for l in tabela[LINHA_CABECALHO:] if any(c.strip() for c in l)]
    novas = [
        [ciclo, l.data.isoformat(), l.titulo, formata_valor(l.valor), l.parcela or ""]
        for l in lancamentos
    ]
    apagadas = [l for l in antigas if l[0] == ciclo]
    finais = sorted(
        [l for l in antigas if l[0] != ciclo] + novas, key=lambda l: (l[0], l[1], l[2])
    )

    if apagadas and not novas:
        raise SystemExit(f"abortado: apagaria {len(apagadas)} linhas e não escreveria nenhuma")

    relatorio = {
        "apagadas": len(apagadas),
        "escritas": len(novas),
        "total_depois": len(finais),
        "ciclos_depois": sorted({l[0] for l in finais}),
    }

    if not aplicar:
        return relatorio

    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"Lancamentos_{ciclo}_{datetime.now():%Y%m%d-%H%M%S}.json"
    backup.write_text(
        json.dumps({"aba": aba, "linhas": antigas}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    relatorio["backup"] = str(backup)

    # 211 linhas por ciclo contra um grid de 1000: sem isto a planilha enche em
    # ~3 ciclos e a execução morre com um erro opaco de range.
    faltam = (PRIMEIRA_LINHA + len(finais) + 200) - total_grid
    if faltam > 0:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"appendDimension": {
                "sheetId": propriedades["sheetId"], "dimension": "ROWS", "length": faltam}}]},
        ).execute()

    # RAW é obrigatório: com USER_ENTERED, uma planilha pt-BR leria
    # "R$ 186.63" como milhar e gravaria 18663 em todas as linhas.
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{aba}!A{PRIMEIRA_LINHA}:E{LINHA_CABECALHO + len(finais)}",
        valueInputOption="RAW",
        body={"values": finais},
    ).execute()

    if len(finais) < len(antigas):
        sheets.spreadsheets().values().clear(
            spreadsheetId=sid,
            range=f"{aba}!A{PRIMEIRA_LINHA + len(finais)}:E{LINHA_CABECALHO + len(antigas)}",
            body={},
        ).execute()

    return relatorio


# ---------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="escreve de verdade (padrão: só mostra)")
    p.add_argument("--cycle", metavar="AAAA-MM", help="força o ciclo")
    p.add_argument("--csv", type=Path, help="usa um CSV local em vez do Gmail")
    p.add_argument("--spreadsheet-id", help="aponta para outra planilha (ex.: uma cópia de teste)")
    p.add_argument("--auth-check", action="store_true", help="confere o acesso e sai")
    p.add_argument("--config", type=Path, default=RAIZ / "config.json")
    args = p.parse_args(argv)

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    sid = args.spreadsheet_id or cfg["spreadsheet_id"]

    if args.csv:
        ciclo = args.cycle or ciclo_do_nome(args.csv.name)
        csv_local = args.csv
        gmail = drive = sheets = None
        if not args.csv.exists():
            raise SystemExit(f"não existe: {args.csv}")
        _, _, sheets, _ = autentica(Path(cfg["secrets_dir"]))
    else:
        gmail, drive, sheets, cred = autentica(Path(cfg["secrets_dir"]))

        if args.auth_check:
            print("conta: ", gmail.users().getProfile(userId="me").execute()["emailAddress"])
            print("escopos:", " ".join(sorted(cred.scopes or [])))
            return 0

        ciclos = busca_anexos(gmail, cfg["gmail_sender"])
        if not ciclos:
            raise SystemExit(
                "nenhum extrato Nubank_AAAA-MM-DD.csv no Gmail.\n"
                "Peça o extrato da fatura em CSV no app do Nubank e rode de novo."
            )
        ciclo = args.cycle or max(ciclos)
        if ciclo not in ciclos:
            raise SystemExit(
                f"o ciclo {ciclo} não tem CSV no e-mail — nada foi escrito.\n"
                f"Disponíveis: {', '.join(sorted(ciclos))}.\n"
                "Se o ciclo é novo, peça o extrato em CSV no app do Nubank."
            )

        # O ciclo escolhido é o mais recente *com CSV*. Se a fatura do mês já
        # fechou mas você ainda não pediu o extrato, o script processa o mês
        # anterior em silêncio — por isso o ciclo sempre aparece na saída.
        csv_local = baixa(gmail, ciclos[ciclo], RAIZ / "out")

    fatura = parse_fatura_detalhado(csv_local, ciclo)

    print(f"ciclo {ciclo}: {fatura.quantidade} lançamentos, total {formata_valor(fatura.total)}")
    print(f"  descartados: {len(fatura.descartados)} ajustes")
    if fatura.residuo_iof:
        print(f"  resíduo de IOF sem par neste ciclo: {formata_valor(fatura.residuo_iof)}")

    if drive:
        print(f"  drive: {csv_local.name} — {sobe(drive, cfg['drive_folder_id'], csv_local, args.apply)}")

    r = atualiza_planilha(
        sheets, sid, cfg["sheet_tab"], ciclo, fatura.mantidos, args.apply, RAIZ / "backups"
    )
    verbo = "apagadas" if args.apply else "apagaria"
    print(f"  planilha: {verbo} {r['apagadas']}, escritas {r['escritas']}, total {r['total_depois']}")
    print(f"  ciclos:   {', '.join(r['ciclos_depois'])}")
    if "backup" in r:
        print(f"  backup:   {r['backup']}")
    if not args.apply:
        print("\nnada foi escrito. Rode de novo com --apply para aplicar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
