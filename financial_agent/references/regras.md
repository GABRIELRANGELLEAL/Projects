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
- `^IOF de "` — cobrança de IOF sobre compra internacional. Devolvida pelo par acima, então não é gasto real. O título vem com o comerciante entre aspas escapadas, ex.: `IOF de ""Tfl Travel Ch""`.
- `^Estorno de "` — estorno de uma compra que já aparece na própria fatura. Valor negativo, ex.: `Estorno de ""Apple.Com/Bill"" (Apple)`.
- `^Crédito de "` — mesma família do estorno, com outro nome. Visto uma vez, em `Nubank_2026-04-01.csv`: `Crédito de ""Google One""` com `-35.71`. Ficou de fora da lista original e só apareceu quando o relatório começou a classificar assinaturas e mostrou uma assinatura de valor negativo.
- `^Pagamento recebido` — crédito do pagamento da fatura anterior, não um gasto deste ciclo. Costuma ser o maior valor negativo do arquivo, ex.: `- 10.616,67`.

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
