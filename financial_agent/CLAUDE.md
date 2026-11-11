# Fatura Agent

Agente que processa a fatura do cartão de crédito Nubank de ponta a ponta: busca o extrato CSV no Gmail, arquiva no Google Drive, carrega os lançamentos numa aba do Google Sheets e garante que reprocessar o mesmo ciclo não duplica nada.

**O pipeline só lida com CSV.** O PDF da fatura foi removido do fluxo por decisão do usuário (18/10/2026) — não é baixado nem arquivado.

Também serve para limpar lançamentos de ajuste, identificar parcelamentos e gerar resumos comparativos entre ciclos.

## Como é acionado

Não é um processo contínuo. Você abre uma sessão do Claude Code e pede algo como "processa a fatura". O Claude lê este arquivo e os de `references/`, e roda `scripts/processa_fatura.py`.

O roteiro é sempre o mesmo, e **dry-run é o padrão**:

1. Claude roda `python scripts\processa_fatura.py` (sem `--apply`) — nada é escrito.
2. Relata o que o script imprimiu: ciclo, lançamentos, total, linhas que seriam apagadas e a ação no Drive.
3. Pede sua confirmação.
4. Com um sim claro, roda `--apply`.
5. Relata o resultado e o caminho do backup.

Se o script sair com erro, a mensagem no stderr já diz o que fazer — Claude relata e para, sem tentar contornar.

### Pré-requisito mensal que depende de você

O CSV **não chega sozinho** — ele só é enviado quando você pede o extrato da fatura no app do Nubank. Logo, o ciclo mensal é: pedir o CSV no app → esperar o e-mail → acionar o Claude.

**Armadilha que isso cria:** o ciclo processado é o mais recente *que tem CSV*. Se a fatura do mês já fechou mas você ainda não pediu o extrato, o script processa o mês anterior sem reclamar — não há como ele saber que existe uma fatura mais nova, já que só olha CSVs. Por isso o ciclo sempre aparece na primeira linha da saída: confira que é o mês que você espera.

## Fluxo de execução

```
1. Gmail: listar anexos Nubank_*.csv → agrupar por ciclo
2. Escolher ciclo = max(ciclo)
   └─ nenhum CSV → aborta, NADA é escrito
3. Baixar o CSV para out\
4. Drive: upload idempotente do CSV na pasta faturas
5. Parse do CSV (passos 1 a 4 do workflow abaixo)
6. Sheets: substituir as linhas do ciclo
```

O upload para o Drive vem **antes** da planilha porque é append-only e idempotente. A planilha é a única escrita destrutiva e fica estritamente por último.

## Workflow de parsing

1. **Parse do CSV** — ler `date,title,amount`, converter `amount` de string BR para `Decimal`.
2. **Filtrar ajustes** (ver `references/regras.md` para a lista completa e atualizável) — remover linhas que não são gasto real:
   - `IOF de "..."` e `IOF de volta de ...` (formam par e quase se cancelam — ver ressalva abaixo)
   - `Estorno de "..." (...)`
   - `Pagamento recebido` (é crédito da fatura anterior)
3. **Detectar parcelamento** — regex `Parcela (\d+/\d+)` no título; guardar em campo separado.
4. **Normalizar** cada lançamento em: `[ciclo, data, título, valor, parcela]`.
5. **Agregar** — total do ciclo, contagem de lançamentos, top-N por comerciante (somando repetições do mesmo título).
6. **Comparar** entre ciclos quando houver mais de um disponível — destacar variações grandes e possíveis causas (ex.: viagem, parcelamento novo).

## Formato de entrada

CSV com colunas `date,title,amount`. **Formato confirmado contra dado real** (`Nubank_2026-08-25.csv`):

```
date,title,amount
2026-08-17,IOF de volta de Tfl Travel Ch,"- 0,65"
2026-08-17,"IOF de ""Tfl Travel Ch""","0,65"
2026-08-17,Safetywing,"339,09"
2026-07-31,"Estorno de ""Apple.Com/Bill"" (Apple)","- 8,53"
2026-07-19,Pagamento recebido,"- 10.616,67"
2026-07-18,Amazonmktplc*Newellbra - Parcela 9/12,"91,58"
```

Particularidades a tratar: datas já vêm em `AAAA-MM-DD`; valores em BR (vírgula decimal, ponto de milhar como em `10.616,67`); negativos como `"- 8,53"`, **com espaço após o sinal**; títulos com `"` escapado como `""`; `Parcela n/m` é sufixo do título.

O validador de valores rejeita agrupamento de milhar malformado (`12.34.56`) em vez de remover os pontos e aceitar `123456` — corrupção silenciosa de valor é o pior erro possível neste pipeline.

### Ressalva: os pares de IOF não se cancelam dentro de um ciclo

Medido no arquivo real de agosto: 23 cobranças de IOF (`R$ 107,02`) contra 22 devoluções (`-R$ 104,52`), deixando resíduo de **R$ 2,50**. A cobrança órfã é a linha mais antiga do arquivo e a devolução dela caiu no ciclo anterior — efeito de borda da janela do ciclo, não erro de parsing, e recorrente sempre que uma compra internacional cai perto do fechamento.

Consequência prática: **o total dos lançamentos filtrados não fecha exatamente com o total impresso na fatura.** Qualquer reconciliação precisa considerar `ResultadoParse.residuo_iof`, que existe para isso. Descartar os dois lados continua correto — o IOF é devolvido, só não no mesmo ciclo.

## Identificação do ciclo

`^Nubank_(\d{4})-(\d{2})-(\d{2})\.csv$` → `ciclo = "{yyyy}-{mm}"`. O dia é **descartado**; nunca somar ou subtrair mês.

O nome do arquivo é a fonte da verdade: é gerado pelo Nubank e é a convenção já adotada à mão no Drive. **As datas das linhas não servem** — `Nubank_2026-08-25.csv` contém compras de `2026-07-18` a `2026-08-17` e o ciclo dele é `2026-08`.

Para escolher o ciclo a processar, usar `max(ciclo)` sobre os anexos, **não** o e-mail mais recente: o Nubank reenvia mensagens, então ordenar por data de recebimento acabaria pegando a fatura errada.

## Integrações Google

| Recurso | ID |
|---|---|
| Remetente no Gmail | `todomundo@nubank.com.br` |
| Pasta no Drive (`faturas`) | `1a4k5ieLkaa8ZsngxmS2NTemmusDk4eO9` |
| Planilha (`life`) | `1efkMTnZV-e6Pp7gAf1NhgGHnWEYeoPGhuWt39o_vwF8` |
| Aba | `Lançamentos` (`sheetId: 0`) |

Esses valores vivem em `config.json` e são sobrescrevíveis por flag, para dar para apontar numa planilha de teste sem editar o arquivo versionado.

### Escopos OAuth

| Escopo | Por quê |
|---|---|
| `gmail.readonly` | `messages.attachments.get` exige acesso ao corpo; `gmail.metadata` não lê anexos. É o mais estreito que funciona. |
| `drive` | Escrever na pasta `faturas`. O escopo estreito `drive.file` só alcança arquivos criados pelo próprio app, e a pasta foi criada à mão — usá-lo exigiria um fallback inteiro para o 404 que ele provoca. Troca consciente: mais acesso, menos código. |
| `spreadsheets` | Leitura e escrita. Não há escopo de escrita mais estreito nem como limitá-lo a uma planilha. |

### Armadilha de auth já conhecida

**O refresh token expira em 7 dias.** Com o app em modo *Testing* e um escopo restrito (`gmail.readonly`), o Google invalida o refresh token nesse prazo. Como o pipeline roda mensalmente, **toda execução vai pedir consentimento no navegador** — isso é esperado, não é bug. O script captura o `RefreshError`, apaga o `token.json` e reabre o fluxo sozinho.

### Setup único no Google Cloud

1. console.cloud.google.com → **New project** → `financial-agent` → selecionar.
2. **APIs & Services → Library** → habilitar **Gmail API**, **Google Drive API** e **Google Sheets API**.
3. **OAuth consent screen** → **External** (conta `@gmail.com` pessoal não oferece *Internal*) → nome `financial agent`, e-mails de suporte e contato.
4. **Audience → Test users → Add users** → a própria conta. Deixar em **Testing**.
5. **Credentials → Create credentials → OAuth client ID** → **Desktop app** → **Download JSON**.
6. Salvar como `client_secret.json` em `C:\Users\Leal\AI\Projects\.secrets\financial_agent\`.
7. Primeira execução abre o navegador: escolher a conta → "Google hasn't verified this app" → **Advanced → Go to financial agent (unsafe)** → **manter todas as caixas marcadas** → Allow.

O passo 7 importa: o Google mostra uma caixa por escopo e um consentimento parcial geraria token incompleto. O script confere os escopos logo após o fluxo e descarta o token se faltar algum, em vez de falhar no meio de uma escrita.

### Onde ficam as credenciais

`client_secret.json` e `token.json` ficam em `C:\Users\Leal\AI\Projects\.secrets\financial_agent\`, caminho definido em `config.json`.

O caminho é absoluto de propósito: `%USERPROFILE%` resolve para `C:\Users\thiag` no shell desta máquina enquanto o projeto vive sob `C:\Users\Leal`, e um caminho derivado apontaria para outro cache de token dependendo de quem executa.

**Atenção:** essa pasta está *dentro* do monorepo git (`C:\Users\Leal\AI\Projects\.git`), então o `.gitignore` do `financial_agent/` não a cobre. A proteção está em `.git\info\exclude` do repo pai, que ignora `.secrets/`, `financial_agent/out/`, `financial_agent/backups/` e `financial_agent/.venv/`. Esse arquivo não é versionado, então a regra não pode vazar junto — mas também não acompanha um clone. Se o projeto for clonado em outra máquina, refazer a exclusão lá.

## Contrato da aba `Lançamentos`

```
Linha 1:  Mês fatura | Data | Descrição | Valor (R$) | Parcela     ← cabeçalho
Linha 2+: 2026-09 | 2026-08-14 | Lime*4 Rides Xbxf         | R$ 186.63 |
          2026-09 | 2026-08-18 | Tap Web Vip - Parcela 4/10 | R$ 356.16 | 4/10
```

- `Mês fatura` é o **ciclo**, não o mês da compra (o ciclo `2026-09` contém datas de agosto).
- A descrição mantém o sufixo ` - Parcela 4/10` **e** a coluna E repete `4/10` — redundância deliberada, para casar com as linhas que já existem.
- Valores são **texto** no formato `R$ 1,234.56` — estilo US (vírgula de milhar, ponto decimal), deliberadamente **diferente** do BR do CSV de entrada. Não reusar o formatador do parser aqui.

**Não há bloco de resumo.** A planilha teve as três linhas de sumário (total acumulado, contagem, última atualização) removidas à mão em 18/10/2026, por decisão do usuário. O script não escreve nada fora de `A2:E`, e os totais aparecem só na saída do terminal.

Antes de escrever, o script exige o cabeçalho da linha 1 **exatamente igual** e aborta imprimindo esperado vs. encontrado. Isso já provou o próprio valor: pegou justamente essa mudança de layout antes de escrever no lugar errado.

### Capacidade do grid

211 linhas por ciclo contra um grid de 1000 — a planilha enche em cerca de **três ciclos**. O script cresce o grid com `appendDimension` **antes** de escrever, deixando 200 linhas de folga. Sem isso a execução morre no meio com um erro opaco de "range exceeds grid limits".

## Idempotência

**Regra:** uma execução do ciclo *C* deixa a planilha num estado que depende apenas do CSV de *C* e das linhas pré-existentes dos outros ciclos. Rodar duas vezes dá o mesmo resultado.

Implementação: **substituir o ciclo** reescrevendo o bloco de dados inteiro.

1. gravar backup JSON local das linhas atuais
2. se necessário, `batchUpdate → appendDimension`
3. `values.update(range="Lançamentos!A2:E{1+len(finais)}", valueInputOption="RAW")`
4. se houver cauda, `values.clear` **só** abaixo do bloco novo, colunas A:E

**Não usar `deleteDimension`.** Requests de `batchUpdate` executam em sequência e cada delete desloca os índices seguintes; com blocos não-contíguos seria preciso emitir os deletes em ordem decrescente de `startIndex`, e a conversão 0-based com `endIndex` exclusivo é mais um lugar para errar. É o tipo de bug que ninguém percebe até 30 linhas desaparecerem. A reescrita completa deixa `PRIMEIRA_LINHA + len(finais)` como única aritmética de índice.

**Não usar `values.append`.** Ele anexa depois da "tabela" que detectar, o que é imprevisível, e não mantém o agrupamento por ciclo.

**`valueInputOption="RAW"` é obrigatório.** Com `USER_ENTERED` numa planilha de locale pt-BR, `R$ 186.63` seria lido com `.` como separador de milhar e gravado como `18663` — silenciosamente, em todas as linhas.

### Rede de segurança

- **Dry-run é o padrão.** Sem `--apply` nada é escrito, nem no Drive nem na planilha.
- Backup em `backups\Lancamentos_<ciclo>_<timestamp>.json` antes de escrever. Guarda as linhas anteriores; para desfazer, basta colar de volta.
- Aborta se o plano apagaria linhas e não escreveria nenhuma.
- No Drive: se já existe arquivo com aquele nome na pasta, não faz nada. Nunca sobrescreve, nunca duplica, nunca apaga.

## Scripts

| Arquivo | Responsabilidade |
|---|---|
| `scripts/processa_fatura.py` | tudo que fala com o Google: auth, Gmail, Drive, Sheets, e a CLI |
| `scripts/parse_fatura.py` | parser CSV dos passos 1 a 4. `parse_fatura(path, ciclo) -> list[dict]`, rodável isolado com `python scripts/parse_fatura.py caminho.csv` |
| `scripts/regras.py` | carrega `references/regras.md` em padrões compilados |
| `scripts/relatorio.py` | relatório em texto, só leitura — nunca escreve na planilha |

São três de propósito. Um pipeline mensal de um usuário não sustenta mais que isso, e a primeira versão deste projeto tinha sete módulos, nove exit codes, envelope JSON e comando de restore — peso que ninguém ia usar. Se a vontade de dividir voltar, o critério é: só separa quando dois arquivos passarem a ter motivos diferentes para mudar.

### Interface do CLI

```
python scripts\processa_fatura.py [flags]

  --apply              escreve de verdade (padrão: só mostra o que faria)
  --cycle 2026-10      força o ciclo
  --csv <path>         usa um CSV local em vez do Gmail (não arquiva nada)
  --spreadsheet-id ID  aponta para outra planilha, ex.: uma cópia de teste
  --auth-check         confere conta e escopos, e sai
```

O resumo sai em texto no **stdout**; avisos e logs vão para o **stderr**. Em erro, a mensagem diz o que fazer e o código de saída é diferente de zero — o Claude relata e para, sem tentar contornar.

## Relatório

```
.\.venv\Scripts\python.exe scripts\relatorio.py
```

Imprime em texto, para ler no terminal ou colar numa conversa. Lê a planilha e **nunca escreve nela** — quem escreve é só o `processa_fatura.py`. Não usa nenhuma dependência além da stdlib e do cliente Google que o pipeline já precisa.

Cinco seções:

- **Total por ciclo** — total, variação vs. ciclo anterior (em R$ e %), contagem.
- **Parcelamentos em aberto** — a partir de `Parcela n/m` do ciclo mais recente, calcula quanto falta pagar, até quando, e o compromisso já assumido em cada ciclo futuro. **Supõe que as parcelas restantes têm o mesmo valor da atual**, o que vale para parcelamento fixo mas não para compra internacional, onde o câmbio muda cada parcela.
- **Gastos recorrentes** — títulos sem parcelamento que aparecem em 2 ou mais ciclos, separados em dois grupos: os que aparecem em **todos** os ciclos (assinaturas de verdade) e os que aparecem em parte deles (comerciantes repetidos). Essa separação é o que torna o indicador útil; sem ela, supermercado e Netflix ficam na mesma lista.
- **Consumo, Pix no crédito e exterior** — por ciclo. Lê os CSVs de `out/`, não a planilha, porque depende de linhas que o filtro remove (ver abaixo).
- **Assinaturas** — custo médio por ciclo e em quantos ciclos cada serviço apareceu, classificado pelas palavras-chave de `references/regras.md`.

### Por que as duas últimas seções leem `out/` e não a planilha

**Pix no crédito** não é consumo no cartão: é uso do limite para transferir dinheiro. Em `2026-10` foram R$ 15.547 de R$ 25.722 — 60% da fatura, dos quais R$ 12.614 num Pix do usuário para ele mesmo. Somar isso ao consumo inverte a leitura do mês: parece alta de 28%, quando o consumo caiu 47%.

**Internacional** é deduzido da cobrança de IOF, que só incide em compra fora do país. Mas o IOF é um dos padrões filtrados, então essa informação **não existe na planilha** — só nos CSVs originais. Se `out/` estiver vazio, a seção avisa e se cala, em vez de inventar número.

Houve um dashboard Streamlit aqui, removido em 18/10/2026: o relatório em texto cobria a mesma necessidade sem servidor local nem as ~27 dependências transitivas que o Streamlit arrasta.

## Relatório visual (o padrão quando o usuário pede "um report")

Decidido em 19/10/2026: **toda vez que o usuário pedir um relatório, entregue os gráficos**, não a tabela em texto. O texto do `relatorio.py` vira a fonte dos números; o que vai para a conversa é um widget único com estes quatro gráficos, nesta ordem, e **pouco texto**.

1. **Três cartões no topo:** valor final da fatura do ciclo mais recente · parcelas a vencer · assinaturas por ciclo.
2. **Consumo e Pix no crédito por ciclo** — barras empilhadas, azul `#2a78d6` e laranja `#eb6834`, legenda em HTML acima do gráfico.
3. **Consumo feito no exterior** — linha, 0 a 100%, um ponto por ciclo.
4. **Parcelas já comprometidas** — barras, um mês futuro por barra.
5. **Assinaturas, custo médio por ciclo** — barras horizontais, série única, com o número de ciclos no tooltip.

Regras de apresentação que o usuário pediu explicitamente: **sem gráfico de categorias** (testado e descartado em 19/10), e o texto fora do widget limitado a três ou quatro linhas de leitura — nunca parágrafos.

Convenções de visualização herdadas da paleta validada: série única usa só o azul; duas séries usam azul e laranja nessa ordem; eixo e grade recessivos (`#898781` e `#e1e0d9`); barras de no máximo 24px na horizontal e 44px na vertical; nunca dois eixos y.

## Convenções

- Nunca alterar o CSV original — ler e gerar saída normalizada em arquivo separado (ex.: `out/<ciclo>_rows.json`).
- Ao adicionar um novo padrão de lançamento a ignorar, editar `references/regras.md`, não o script — mantém a lógica de negócio separada do código.
- Valores monetários sempre arredondados a 2 casas decimais na saída. **`Decimal` com `ROUND_HALF_UP` ponta a ponta, nunca `float`.**
- Datas no formato `AAAA-MM-DD`.
- Na planilha, valores usam o formato `R$ 1,234.56` dela, que **não** é o formato BR do CSV de entrada.
- Segredos nunca entram na árvore do projeto nem no git.

## Ambiente

Python 3.11.9 (build da Microsoft Store), pip 25.3. Dependências: `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`, `pytest`.

Criar o venv com `python -m venv .venv` e usar `.\.venv\Scripts\python.exe` **diretamente** — a política de execução do PowerShell pode bloquear o `Activate.ps1`.

O git CLI **não está instalado** nesta máquina, e `financial_agent/` está *untracked* dentro do monorepo em `C:\Users\Leal\AI\Projects\.git`. O `.gitignore` do projeto é seguro contra um cliente gráfico ou uma instalação futura de git; vale também acrescentar `financial_agent/out/` e `financial_agent/backups/` ao `.git\info\exclude` do repo pai, que não é versionado.

## Verificação

Pontos de checagem, em ordem. Cada um é barato de corrigir onde está e caro de descobrir depois.

1. **Testes offline** — `python -m pytest tests -q`. São 85, rodam em 0,1s e validam o parser contra o CSV real de agosto (`tests/fixtures/Nubank_2026-08-25.csv`, 4405 bytes, idêntico ao do Drive): 54 lançamentos, R$ 5.072,63, os 47 descartes distribuídos pelas 4 regras.
2. **Auth** — `--auth-check` deve imprimir a conta `gabrielrangelleal@gmail.com` e os três escopos.
3. **Parse do ciclo de setembro — a checagem que mais importa.** Rodar com `--csv` sobre `Nubank_2026-09-25.csv` e conferir contra o que você já validou à mão na planilha: **211 lançamentos** e total **R$ 20.075,50**. Se as regras de filtro reproduzem esses dois números, o parser está provado contra dado real; se não, as regras estão erradas — e isso aparece antes de qualquer escrita.
4. **Planilha** — dry-run primeiro. `apagaria 0` significa que nenhum ciclo existente será tocado; `apagaria N, escritas N` significa que o ciclo está sendo substituído por si mesmo, o que é a prova de idempotência. Para testar sem risco, fazer uma cópia de `life` no Drive e apontar `--spreadsheet-id` para ela.
5. **Execução real** — conferir a olho a fronteira entre ciclos: cada bloco agrupado, datas ascendentes dentro do ciclo, valores renderizando como `R$ 1,234.56` igual aos antigos.

## O que este agente NÃO faz

- Não roda sozinho em segundo plano — cada execução depende de uma mensagem sua numa sessão.
- Não pede o CSV no app do Nubank por você: esse passo é manual e o pipeline aborta sem ele.
- Não lida com o PDF da fatura de forma nenhuma: não baixa, não arquiva, não extrai dados dele.
- Não apaga linhas fora do ciclo alvo, nunca.
- Não remove nem sobrescreve arquivo no Drive.
- Não versiona segredos.
- Não dá conselho financeiro — apenas organiza e resume os dados.

## Pontos abertos

- **A pasta `faturas` do Drive ainda tem os PDFs de `2026-08` e `2026-10`**, enviados antes da decisão de abandonar o PDF. Ficaram lá de propósito: nada neste pipeline apaga arquivo do Drive.
- **Há ~12 ciclos antigos** (fev/2025 em diante) com e-mail de fatura fechada no Gmail, mas sem CSV. Para carregá-los seria preciso pedir cada extrato no app do Nubank.
- **Valores são texto na planilha**, logo a coluna D não soma via `SUM` e todo total é recalculado pelo script. Com o bloco de resumo removido, não há nenhum total dentro da planilha — se você quiser um, vale migrar a coluna D para célula numérica com formato de moeda e usar `SUMIF` por ciclo.
- **A conta é presumidamente `@gmail.com` pessoal.** Se for Workspace, o consent screen pode ser *Internal* e a expiração de 7 dias desaparece — vale checar antes do setup.
