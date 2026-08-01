# Bot de simulação — copy trading no Polymarket

Bot de **simulação** (dry-run, dinheiro fictício) que descobre carteiras
automaticamente, filtra pelas que têm histórico comprovado, e copia os
trades delas com capital fictício de **$500** — sem restrição de categoria.

Nenhuma ordem real é enviada. Nenhuma chave privada é necessária. Ele só lê
dados públicos do Polymarket e mantém a contabilidade em arquivos locais
(`data/`).

## Como funciona (agora 100% automático)

1. **`python main.py discover`** escaneia o feed público e global de trades
   recentes do Polymarket (todas as carteiras, não uma específica), procura
   endereços que aparecem repetidamente com trades grandes, e escreve os
   candidatos em `data/watchlist.csv` sozinho. **Você não precisa mais saber
   ou procurar endereços manualmente.**

2. **`python main.py rank`** busca o histórico real de cada carteira candidata
   (trades e posições resolvidas) e aplica os filtros definidos em
   `config.py`: mínimo de 100 trades históricos e win rate mínimo de 55%. Só
   as aprovadas entram na "watchlist qualificada"
   (`data/qualified_wallets.json`).

3. **`python main.py cycle`** roda uma única passada: verifica se alguma
   carteira qualificada abriu um trade novo, decide se copia (5% do capital
   fictício por trade, máximo 8 posições simultâneas, para de seguir
   automaticamente uma carteira após 3 perdas seguidas), e fecha posições
   quando o mercado resolve. Pensado para ser chamado por um agendador
   externo (veja "Rodando sem o PC ligado" abaixo).

   Se preferir deixar um processo local rodando continuamente em vez de
   agendar externamente, `python main.py run` faz a mesma coisa em loop
   (a cada 5 minutos), mas exige o computador ligado o tempo todo.

4. **`python report.py`** lê o log de trades e gera um relatório em
   `reports/relatorio_<data>.md` com retorno do período, win rate, drawdown,
   desempenho por carteira copiada e sugestões automáticas (ex.: "pare de
   seguir a carteira X, ela só deu prejuízo esta semana").

## Rodando sem o PC ligado (recomendado): GitHub Actions

O jeito mais simples e **gratuito** de deixar o bot rodando 24/7 sem depender
do seu computador é usar o GitHub Actions — já vem configurado em
`.github/workflows/bot.yml`.

Passo a passo:

1. Crie uma conta gratuita no GitHub (se ainda não tiver) e crie um novo
   repositório (pode ser público — não há chaves privadas nem dinheiro real
   aqui, só simulação).
2. Suba a pasta `polymarket_sim_bot/` inteira para esse repositório.
3. No repositório, vá em **Settings → Actions → General → Workflow
   permissions** e marque **"Read and write permissions"** (isso permite o
   bot salvar o progresso automaticamente a cada execução).
4. Pronto. O workflow já está agendado para: rodar um ciclo de monitoramento
   a cada 30 minutos, e rodar descoberta + ranking + relatório toda
   segunda-feira às 3h (UTC).
5. Para testar imediatamente sem esperar o agendamento, vá na aba **Actions**
   do repositório, clique no workflow "polymarket-sim-bot" e depois em
   **"Run workflow"**.

Repositórios **públicos** têm minutos de execução ilimitados no GitHub
Actions. Se preferir um repositório privado, o plano gratuito inclui ~2.000
minutos/mês, o que também é suficiente para essa cadência de 30 em 30
minutos.

### Alternativa: VPS (mais controle, custo baixo)

Se preferir um servidor próprio em vez do GitHub Actions (por exemplo, para
rodar o loop contínuo com `python main.py run` em vez de ciclos agendados):
contrate um VPS simples (~$5-6/mês), instale Python, copie os arquivos, e
rode dentro de um `tmux`/`screen` ou como serviço systemd, para continuar
rodando mesmo com você desconectado.

## Rodando o teste de 1 mês (visão geral)

```bash
pip install -r requirements.txt

python main.py discover   # descobre carteiras automaticamente
python main.py rank       # filtra pelas que têm bom histórico
python main.py cycle      # roda um ciclo (ou configure o GitHub Actions acima)

# Toda semana:
python report.py --days 7

# No final do mês:
python report.py --days 30
```

## Importante: isto foi construído sem acesso direto à API do Polymarket

O ambiente onde este bot foi escrito bloqueia conexões diretas aos domínios
`gamma-api.polymarket.com` e `data-api.polymarket.com` (rede restrita por
allowlist). Por isso:

- **Toda a lógica foi validada com dados de exemplo** (veja
  `_offline_test.py`), não com chamadas reais. Os testes confirmam que a
  descoberta automática, o filtro de carteiras, o motor de simulação
  (abrir/fechar posições, cálculo de PnL) e o gerador de relatório
  funcionam corretamente com os formatos de dado documentados
  publicamente.
- **A primeira execução real (via GitHub Actions ou no seu próprio
  computador) é o teste de verdade** — é ali que a conexão com a API
  acontece de fato. Se algo vier vazio ou com erro, confira os logs da
  execução (na aba Actions, se estiver usando GitHub Actions).
- **Os nomes exatos de alguns campos da API podem precisar de ajuste.** Em
  especial, em `wallet_ranking.py`, a função `_compute_metrics()` espera
  encontrar em `/positions` campos como `realizedPnl` (ou `cashPnl`/`pnl`) e
  `redeemable` (ou `resolved`/`closed`). Na primeira execução real, confira
  o arquivo salvo automaticamente em `data/_debug_sample_position.json` —
  ele mostra a resposta de verdade da API. Se os nomes de campo forem
  diferentes, ajuste a lista em `_first_present(p, [...], ...)` dentro de
  `wallet_ranking.py`. Você pode colar o conteúdo desse arquivo de volta
  para mim a qualquer momento e eu ajusto o código para você — não precisa
  decifrar isso sozinho.

## O que ajustar em `config.py`

| Parâmetro | Padrão | O que controla |
|---|---|---|
| `SIMULATED_CAPITAL_USD` | 500 | Capital fictício inicial |
| `POSITION_SIZE_PCT` | 0.05 (5%) | Quanto do capital vai em cada trade copiado |
| `MAX_OPEN_POSITIONS` | 8 | Limite de exposição simultânea |
| `MIN_TRADES_HISTORY` | 100 | Trades mínimos para validar uma carteira |
| `MIN_WIN_RATE` | 0.55 | Win rate mínimo para qualificar |
| `CATEGORY_FILTER` | `None` (todas) | Restringir a uma categoria (ex.: `"Sports"`) |
| `MAX_CONSECUTIVE_LOSSES_PER_WALLET` | 3 | Perdas seguidas até pausar automaticamente uma carteira |

Parâmetros da descoberta automática ficam em `wallet_discovery.py`
(`min_trade_size_usd`, `min_appearances`, `top_n`) — não precisa mexer neles
para começar, os padrões já são razoáveis.

## Migrando para dinheiro real (depois do teste de 1 mês)

Não implementado aqui de propósito — é um passo separado e deve vir só
depois de revisar os relatórios semanais e confirmar que os resultados são
consistentes (não um pico de sorte). Quando for a hora:

1. O único lugar que muda de verdade é a função que "copia" o trade
   (`maybe_copy_trade` em `simulator.py`): em vez de só escrever no CSV, ela
   passaria a chamar a API de execução (CLOB) do Polymarket para enviar uma
   ordem real, assinada pela sua carteira.
2. Isso exige configurar uma carteira dedicada (nunca a principal), com
   fundos limitados, e gerenciar a chave privada com segurança (nunca no
   código-fonte, e nunca num repositório GitHub público).
3. Comece com uma fração pequena do capital (10-20% do que pretende usar no
   final), mantendo os mesmos filtros e limites validados na simulação.
4. Configure um "kill switch" manual e alertas em tempo real antes de deixar
   rodando sem supervisão constante.

## Estrutura de arquivos

```
polymarket_sim_bot/
├── .github/workflows/bot.yml  # agendamento automático (GitHub Actions)
├── config.py                  # todos os parâmetros ajustáveis
├── polymarket_client.py       # chamadas às APIs públicas do Polymarket
├── wallet_discovery.py        # descoberta automática de carteiras candidatas
├── wallet_ranking.py          # filtro/ranking de carteiras
├── simulator.py               # motor de simulação (abrir/fechar posições)
├── main.py                    # orquestração (discover / rank / cycle / run)
├── report.py                  # gerador de relatório semanal
├── _offline_test.py           # teste com dados de exemplo (não é o bot em si)
├── requirements.txt
├── data/
│   ├── watchlist.csv          # preenchido automaticamente por 'discover'
│   ├── qualified_wallets.json
│   ├── trade_log.csv          # histórico de trades simulados
│   └── ...
└── reports/
    └── relatorio_<data>.md
```
