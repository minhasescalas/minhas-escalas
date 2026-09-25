# Minhas Escalas — V8 determinístico

Esta versão remove a dependência de IA para a consulta.

## Arquivos

- `server.py` — API e consulta da base local.
- `index.html` — página pública.
- `updater.py` — acessa o processo público do SEI e extrai as tabelas.
- `requirements.txt` — somente Flask, Gunicorn, Requests e BeautifulSoup.
- `.github/workflows/atualizar-sei.yml` — atualiza a base automaticamente.
- `data/sei_cache.json` — cache gerado pelo GitHub Actions.

## Por que esta versão é diferente

Os documentos do SEI têm seções explícitas como:

- DESCRIÇÃO: ESCALA DE FOLGA
- DESCRIÇÃO: ESCALA DE SERVIÇO

A escala principal também traz colunas separadas para Serviço/Função, Equipe,
Graduação, Matrícula, Efetivo, Dias e Horário.

O sistema passa essas informações diretamente para a consulta, sem uma IA
decidir datas, horários ou classificação.

## Deploy

No GitHub, mantenha somente um `server.py`, um `index.html` e um
`requirements.txt` na raiz.

No Render, o comando deve ser:

`gunicorn --bind 0.0.0.0:$PORT server:app`

A variável `GEMINI_API_KEY` deixa de ser necessária.

Depois do primeiro push:

1. GitHub → Actions → Atualizar escalas do SEI → Run workflow.
2. Aguarde concluir com ✓.
3. Confirme que `data/sei_cache.json` foi atualizado.
4. O Render fará o deploy do novo cache se o Auto-Deploy estiver ligado.
