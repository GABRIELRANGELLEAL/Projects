# Regras de negócio do parsing

Este arquivo é a fonte da verdade dos padrões de lançamento a ignorar. **Para adicionar ou remover um padrão, edite este arquivo — nunca o código.** `scripts/regras.py` lê esta seção e compila os regexes.

## Como o loader lê este arquivo

Dentro da seção `## Padrões ignorados`, cada bullet deve ter exatamente esta forma:

```
- `<regex>` — <motivo em prosa>
```

O loader extrai o **primeiro trecho entre backticks** de cada bullet e compila com `re.compile`. O texto do motivo é ignorado pelo código, mas é obrigatório para quem lê depois. Bullets sem backticks são ignorados; um regex inválido faz o carregamento falhar com erro nomeando a linha.

Os padrões são aplicados ao campo `title` do CSV, com `re.search`, **sem** normalizar caixa. Use `^` para ancorar no início quando o padrão for um prefixo — todos os atuais são.

## Padrões ignorados

- `^IOF de volta de ` — crédito de devolução do IOF. Forma par com a cobrança correspondente abaixo.
- `^IOF de "` — cobrança de IOF sobre compra internacional, **com o comerciante no título**, entre aspas escapadas, ex.: `IOF de ""Tfl Travel Ch""`. Devolvida pelo par acima, então não é gasto real.
- `^IOF de compra internacional` — a **mesma cobrança, sem o nome do comerciante**. É o outro título que o Nubank usa para a mesma coisa; os dois aparecem em dado real e nenhum substituiu o outro (ver a seção abaixo). Faltava na lista, e por isso as cobranças desse formato passavam o filtro e entravam como gasto real enquanto as devoluções continuavam sendo descartadas.
- `^Estorno de "` — estorno de uma compra que já aparece na própria fatura. Valor negativo, ex.: `Estorno de ""Apple.Com/Bill"" (Apple)`.
- `^Crédito de "` — mesma família do estorno, com outro nome. Visto uma vez, em `Nubank_2026-04-01.csv`: `Crédito de ""Google One""` com `-35.71`. Ficou de fora da lista original e só apareceu quando o relatório começou a classificar assinaturas e mostrou uma assinatura de valor negativo.
- `^Pagamento recebido` — crédito do pagamento da fatura anterior, não um gasto deste ciclo. Costuma ser o maior valor negativo do arquivo, ex.: `- 10.616,67`.

### A cobrança de IOF tem dois títulos diferentes, e os dois são reais

O Nubank exporta a cobrança de IOF de duas formas, e **as duas aparecem em dado real** — não é uma que substituiu a outra:

| Título | Ciclos onde foi visto | Comerciante no título? |
|---|---|---|
| `IOF de "<comerciante>"` | `2026-08` (23 linhas), `2026-09` | sim |
| `IOF de compra internacional` | `2026-04` (2 linhas), `2026-11` (81 linhas) | **não** |

A devolução nunca mudou: é sempre `IOF de volta de <comerciante>`, com o nome.

Por isso os dois padrões ficam na lista, e **nunca se troca um pelo outro**: um ciclo antigo precisa do formato com aspas, e um ciclo novo do outro. Como o filtro é por `re.search` sobre o título, manter os dois não tem custo nem risco de sobreposição.

O padrão `^IOF de compra internacional` estava faltando, e o efeito era assimétrico — a devolução era descartada, a cobrança não:

- `2026-11`: 81 cobranças, **R$ 251,97**, entravam como gasto real. Com o padrão no lugar, o resíduo do ciclo cai de -R$ 251,03 para R$ 0,94.
- `2026-04`: 2 cobranças, R$ 5,49, pelo mesmo motivo. O resíduo do ciclo vai a **R$ 0,00** — os pares de abril fecham exatos, o que é a melhor evidência de que o padrão está certo.

Consequência para o relatório: a seção de consumo no exterior deduz o comerciante internacional do título da cobrança (`RE_IOF` em `scripts/relatorio.py`, ancorado no formato com aspas). Num ciclo que usa `IOF de compra internacional` o título não carrega o nome, então essa seção não consegue atribuir valor por comerciante. O nome ainda existe nas **devoluções**, que são a fonte alternativa — mas a devolução costuma cair um ciclo depois da compra, então trocar a fonte muda a semântica da seção e não foi feito aqui.

### Os pares de IOF NÃO se cancelam exatamente dentro de um ciclo

Medido no arquivo real `Nubank_2026-08-25.csv`: 23 cobranças somando `R$ 107,02` contra 22 devoluções somando `-R$ 104,52`, deixando um resíduo de **R$ 2,50**. A órfã é `IOF de "Farmacia Higuerasalvar"` em `2026-07-18`, a linha mais antiga do arquivo — a devolução dela caiu no ciclo anterior.

É efeito de borda da janela do ciclo, não erro de parsing, e acontece sempre que uma compra internacional cai perto do fechamento. Consequências:

- O total dos lançamentos filtrados **não fecha exatamente** com o total da fatura; a diferença é o resíduo.
- Descartar os dois lados continua correto: o IOF é devolvido, só não no mesmo ciclo.
- `ResultadoParse.residuo_iof` expõe o valor para que qualquer reconciliação futura o considere em vez de tratá-lo como erro.

## Padrões que NÃO devem ser filtrados

Registrado aqui para evitar que alguém "limpe" demais no futuro:

- `Pix no Crédito - <nome>` — é gasto real (Pix pago com o limite do cartão).
- `eSIM de Viagem - <N>GB` — gasto real, apesar do formato parecer metadado.
- Títulos com sufixo `- Parcela n/m` — gasto real; a parcela é extraída para coluna separada e o sufixo **permanece** na descrição.

## Assinaturas

Palavras-chave que marcam um comerciante como assinatura digital, usadas pelo relatório. A comparação é por **substring, em minúsculas**, contra o título já sem o sufixo de parcela. Mesmo formato dos bullets acima: o primeiro trecho entre backticks é o que vale.

- `anthropic` — Claude
- `apple.com` — App Store e iCloud
- `google one` — armazenamento
- `coursera` — cursos
- `deeplearning` — cursos
- `udemy` — cursos
- `linkedin` — LinkedIn Premium
- `netflix` — streaming
- `spotify` — streaming
- `disney` — streaming
- `helphbomax` — streaming (o título vem como `Dm*Helphbomaxcom`)
- `amazonprime` — Prime
- `amazon ad free` — Prime sem anúncios
- `clube livelo` — programa de pontos
- `openai` — ChatGPT

**Cuidado conhecido:** `Spiti*Finclass` parece assinatura mas é parcelamento (`Parcela 11/12`) e acaba sozinho, por isso não está na lista. Se um dia virar assinatura de verdade, acrescente aqui.

## Detecção de parcelamento

Regex: `Parcela (\d+/\d+)` aplicado ao título. O valor capturado (ex.: `4/10`) vai para o campo `parcela`. O título é preservado inteiro, **com** o sufixo — é assim que as linhas já existentes na planilha estão formatadas, e a coluna `Parcela` repete o mesmo valor por redundância deliberada.

## Conversão de valores

O CSV usa formato BR e precisa destes cuidados:

| Entrada | Saída (`Decimal`) | Cuidado |
|---|---|---|
| `339,09` | `339.09` | vírgula decimal |
| `10.616,67` | `10616.67` | ponto de milhar |
| `- 8,53` | `-8.53` | **espaço após o sinal** |
| `- 10.616,67` | `-10616.67` | sinal, espaço e milhar juntos |

Sempre `Decimal` com `ROUND_HALF_UP` e 2 casas. Nunca `float`.
