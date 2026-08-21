"""
tpl-omie-sync — Serviço FastAPI no Render
Sincroniza estoque TPL → Omie a cada 30min via cron-job.org

Endpoints:
  GET  /health       → status do serviço
  POST /sincronizar  → executa o sync (protegido por token)
"""
import os
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
)
log = logging.getLogger(__name__)

TOKEN_SYNC = os.getenv("TOKEN_SYNC", "")
TZ_SP = ZoneInfo("America/Sao_Paulo")

# Estado em memória — evita execuções simultâneas
_executando = False
_ultimo_sync = None
_ultimo_resultado = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Serviço TPL→Omie iniciado.")
    yield
    log.info("Serviço encerrado.")

app = FastAPI(title="TPL→Omie Sync", lifespan=lifespan)


# ============================================================
# HEALTH
# ============================================================
@app.get("/health")
def health():
    return {
        "status": "ok",
        "executando": _executando,
        "ultimo_sync": str(_ultimo_sync) if _ultimo_sync else None,
        "ultimo_resultado": _ultimo_resultado,
        "hora_sp": datetime.now(TZ_SP).strftime("%d/%m/%Y %H:%M:%S"),
    }


# ============================================================
# SINCRONIZAR
# ============================================================
@app.post("/sincronizar")
async def sincronizar(
    background_tasks: BackgroundTasks,
    authorization: str = Header(default="")
):
    global _executando

    # Verifica token
    token = authorization.replace("Bearer ", "").strip()
    if TOKEN_SYNC and token != TOKEN_SYNC:
        raise HTTPException(status_code=401, detail="Token inválido")

    # Evita execução simultânea
    if _executando:
        log.info("Sync já em andamento. Ignorando requisição.")
        return JSONResponse({"status": "ignorado", "motivo": "sync em andamento"})

    background_tasks.add_task(_executar_sync)
    log.info("Sync agendado em background.")
    return JSONResponse({"status": "agendado"})


async def _executar_sync():
    global _executando, _ultimo_sync, _ultimo_resultado
    _executando = True
    inicio = datetime.now(TZ_SP)
    log.info(f"Iniciando sync TPL→Omie às {inicio.strftime('%H:%M:%S')}...")

    try:
        # Importa e executa o sync
        from utils.ConsultaTPL import rodarAPITPL
        from utils.AtualizaOmie import (
            consultar_produto_omie,
            atualizar_estoque_omie_com_bloqueado,
            atualizar_estoque_kit,
            carregar_locais_estoque,
            SKUS_KITS,
        )
        import time

        locais = carregar_locais_estoque()
        log.info(f"Locais: {locais}")

        skus = rodarAPITPL()
        total = len(skus)
        log.info(f"Recebidos da TPL: {total}")

        ok = falhas = nao_encontrados = 0

        for produto in skus:
            sku       = produto["sku"]
            available = produto["available"]
            bloqueado = produto.get("blocked", 0)

            codigo_produto = consultar_produto_omie(sku)
            if not codigo_produto:
                nao_encontrados += 1
                continue

            if sku in SKUS_KITS:
                sucesso = atualizar_estoque_kit(codigo_produto, available, sku)
            else:
                sucesso = atualizar_estoque_omie_com_bloqueado(
                    codigo_produto, available, bloqueado, sku
                )

            if sucesso: ok += 1
            else: falhas += 1

            time.sleep(1)

        fim = datetime.now(TZ_SP)
        duracao = (fim - inicio).seconds

        _ultimo_resultado = {
            "total": total, "ok": ok,
            "falhas": falhas, "nao_encontrados": nao_encontrados,
            "duracao_segundos": duracao,
        }
        _ultimo_sync = fim
        log.info(f"Sync concluído em {duracao}s — OK={ok} | Falhas={falhas} | NFound={nao_encontrados}")

    except Exception as e:
        log.error(f"Erro no sync: {e}", exc_info=True)
        _ultimo_resultado = {"erro": str(e)}
    finally:
        _executando = False
