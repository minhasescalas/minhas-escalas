# Minhas Escalas — V5

Versão que separa a **atualização do SEI** da **consulta do usuário**.

## Arquitetura

- `updater.py`: roda no GitHub Actions, acessa o processo público do SEI/PMPE e grava `data/sei_cache.json`.
- `.github/workflows/atualizar-sei.yml`: executa manualmente ou 4 vezes por dia.
- `server.py`: não acessa o SEI; consulta somente o cache local.
- `index.html`: interface pública.

Se o SEI ficar indisponível durante uma atualização, o workflow falha e o cache anterior permanece intacto.

## Fluxo

SEI/PMPE → GitHub Actions → `data/sei_cache.json` → GitHub → Render → usuário

O `GITHUB_TOKEN` recebe somente `contents: write`, necessário para o workflow gravar a atualização no próprio repositório.

## Primeira atualização

Depois de enviar os arquivos ao GitHub:

1. Abra a aba **Actions** do repositório.
2. Abra **Atualizar escalas do SEI**.
3. Toque em **Run workflow**.
4. Aguarde a execução terminar com ✓.
5. O workflow só cria um commit se houver alteração nos dados.
6. O Render então fará o deploy do novo cache, se o auto-deploy estiver habilitado.

## Observação

O processo informado nesta versão é público e corresponde ao processo de escalas de setembro de 2026. Se o processo/URL mudar em outro mês, altere `SEI_PROCESS_URL` no `updater.py` ou configure a variável de ambiente no workflow.
