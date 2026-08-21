# TPL → Omie Sync (Render)

Serviço FastAPI que sincroniza estoque da TPL para o Omie.
Chamado a cada 30min pelo cron-job.org.

## Endpoints

- `GET /health` — status do serviço e última execução
- `POST /sincronizar` — executa o sync (requer token)

## Deploy no Render

1. Conecta ao repo
2. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1`
3. Adiciona as variáveis de ambiente do `.env.example`

## cron-job.org

- URL: `https://SEU-SERVICO.onrender.com/sincronizar`
- Método: POST
- Header: `Authorization: Bearer SEU_TOKEN_SYNC`
- Frequência: a cada 30min
