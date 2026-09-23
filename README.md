# Minhas Escalas — V4

Versão preparada para hospedagem em um Web Service Python (ex.: Render).

## Como funciona

1. `index.html` é a interface.
2. `server.py` consulta o processo público do SEI.
3. O servidor lista os documentos acessíveis do processo e pesquisa a matrícula em paralelo.
4. A matrícula pode ser digitada com ou sem hífen.
5. O resultado mostra os documentos onde houve ocorrência e links para abrir o documento original no SEI.
6. O servidor mantém em cache a lista de documentos por 10 minutos para evitar consultas desnecessárias ao processo.

## Rodar no computador

Instale Python 3.13 e execute:

```bash
pip install -r requirements.txt
python server.py
```

Depois abra `http://127.0.0.1:5000`.

## Publicar no Render

O projeto já inclui `render.yaml`.

1. Crie um repositório no GitHub e envie estes arquivos para a raiz do repositório.
2. No Render, crie um **Web Service** e conecte o repositório.
3. Use o plano Free.
4. O Build Command é `pip install -r requirements.txt`.
5. O Start Command é `gunicorn --bind 0.0.0.0:$PORT server:app`.
6. Após o deploy, o Render fornecerá uma URL `onrender.com`.

## Observação importante

O processo público informa 31 registros, mas nem todos os registros necessariamente possuem um link de documento acessível na página externa. A V4 pesquisa todos os documentos que o acesso externo efetivamente disponibiliza e informa no resultado quantos foram acessíveis.

O serviço gratuito do Render pode dormir após período de inatividade e ter um primeiro acesso mais lento.
