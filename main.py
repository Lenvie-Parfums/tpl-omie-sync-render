"""
tpl-omie-sync — Serviço FastAPI no Render
Sincroniza estoque TPL → Omie via chamada externa/cron.

Endpoints:
  GET  /health       → status do serviço + métricas da última execução
  POST /sincronizar  → executa o sync (protegido por token)
"""
import os
import logging
import time
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

# Mantém o comportamento atual. Pode ser alterado no Render sem mudar código.
DELAY_ENTRE_SKUS = float(os.getenv("DELAY_ENTRE_SKUS", "1"))

# Quantos SKUs lentos ficam disponíveis no /health.
QTD_SKUS_LENTOS = int(os.getenv("QTD_SKUS_LENTOS", "10"))

# Estado em memória — evita execuções simultâneas
_executando = False
_inicio_sync_atual = None
_progresso_atual = {}
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
    agora = datetime.now(TZ_SP)
    duracao_atual = None
    if _executando and _inicio_sync_atual:
        duracao_atual = round((agora - _inicio_sync_atual).total_seconds(), 1)

    return {
        "status": "ok",
        "executando": _executando,
        "inicio_sync_atual": (
            _inicio_sync_atual.strftime("%d/%m/%Y %H:%M:%S")
            if _inicio_sync_atual else None
        ),
        "duracao_sync_atual_segundos": duracao_atual,
        "progresso_atual": _progresso_atual,
        "ultimo_sync": (
            _ultimo_sync.strftime("%d/%m/%Y %H:%M:%S")
            if _ultimo_sync else None
        ),
        "ultimo_resultado": _ultimo_resultado,
        "hora_sp": agora.strftime("%d/%m/%Y %H:%M:%S"),
    }


# ============================================================
# SINCRONIZAR
# ============================================================
@app.post("/sincronizar")
async def sincronizar(
    background_tasks: BackgroundTasks,
    authorization: str = Header(default="", alias="Authorization"),
    x_sync_source: str = Header(default="manual", alias="X-Sync-Source"),
):
    global _executando

    token = authorization.replace("Bearer ", "").strip()
    if TOKEN_SYNC and token != TOKEN_SYNC:
        raise HTTPException(status_code=401, detail="Token inválido")

    if _executando:
        log.info(
            "Sync já em andamento. Ignorando requisição. origem=%s",
            x_sync_source,
        )
        return JSONResponse({
            "status": "ignorado",
            "motivo": "sync em andamento",
            "origem": x_sync_source,
            "progresso": _progresso_atual,
        })

    # Reserva imediatamente para impedir duas requisições de entrarem
    # antes de a BackgroundTask efetivamente começar.
    _executando = True
    background_tasks.add_task(_executar_sync)
    log.info("Sync agendado em background. origem=%s", x_sync_source)
    return JSONResponse({
        "status": "agendado",
        "origem": x_sync_source,
    })


async def _executar_sync():
    global _executando, _inicio_sync_atual, _progresso_atual
    global _ultimo_sync, _ultimo_resultado

    inicio = datetime.now(TZ_SP)
    inicio_perf = time.perf_counter()
    _inicio_sync_atual = inicio
    _progresso_atual = {
        "fase": "iniciando",
        "processados": 0,
        "total": None,
        "percentual": 0,
        "sku_atual": None,
    }

    log.info("=" * 70)
    log.info(f"INÍCIO SYNC TPL→OMIE | {inicio.strftime('%d/%m/%Y %H:%M:%S')}")
    log.info("=" * 70)

    try:
        from utils.ConsultaTPL import rodarAPITPL
        from utils.AtualizaOmie import (
            consultar_produto_omie,
            atualizar_estoque_omie_com_bloqueado,
            atualizar_estoque_kit,
            carregar_locais_estoque,
            SKUS_KITS,
        )

        locais = carregar_locais_estoque()
        log.info(f"Locais de estoque: {locais}")

        # --------------------------------------------------------
        # Mede separadamente o tempo gasto para buscar a TPL
        # --------------------------------------------------------
        _progresso_atual["fase"] = "consultando_tpl"
        tpl_inicio = time.perf_counter()
        skus = rodarAPITPL()
        tempo_tpl = time.perf_counter() - tpl_inicio

        total = len(skus)
        _progresso_atual.update({
            "fase": "sincronizando_omie",
            "total": total,
            "processados": 0,
            "percentual": 0,
        })

        log.info(
            f"TPL concluída | SKUs={total} | tempo={tempo_tpl:.2f}s"
        )

        ok = 0
        falhas = 0
        nao_encontrados = 0
        tempos_skus = []
        falhas_detalhadas = []
        nao_encontrados_lista = []

        for indice, produto in enumerate(skus, start=1):
            sku = str(produto.get("sku", "")).strip()
            available = produto.get("available", 0)
            bloqueado = produto.get("blocked", 0)

            sku_inicio = time.perf_counter()
            horario_sku = datetime.now(TZ_SP).strftime("%H:%M:%S")

            _progresso_atual.update({
                "sku_atual": sku,
                "processados": indice - 1,
                "percentual": round(((indice - 1) / total) * 100, 1) if total else 100,
            })

            log.info(
                f"[{indice}/{total}] [{horario_sku}] SKU={sku} | "
                f"TPL disponível={available} | bloqueado={bloqueado} | INÍCIO"
            )

            status = "falha"
            etapa = "consultar_produto"

            try:
                consulta_inicio = time.perf_counter()
                codigo_produto = consultar_produto_omie(sku)
                tempo_consulta = time.perf_counter() - consulta_inicio

                if not codigo_produto:
                    nao_encontrados += 1
                    status = "nao_encontrado"
                    nao_encontrados_lista.append(sku)
                    log.warning(
                        f"[{indice}/{total}] SKU={sku} NÃO ENCONTRADO NO OMIE | "
                        f"consulta={tempo_consulta:.2f}s"
                    )
                else:
                    etapa = "atualizar_estoque"
                    atualizacao_inicio = time.perf_counter()

                    if sku in SKUS_KITS:
                        sucesso = atualizar_estoque_kit(
                            codigo_produto, available, bloqueado, sku
                        )
                    else:
                        sucesso = atualizar_estoque_omie_com_bloqueado(
                            codigo_produto, available, bloqueado, sku
                        )

                    tempo_atualizacao = time.perf_counter() - atualizacao_inicio

                    if sucesso:
                        ok += 1
                        status = "ok"
                    else:
                        falhas += 1
                        status = "falha"
                        falhas_detalhadas.append({
                            "sku": sku,
                            "etapa": etapa,
                            "motivo": "função de atualização retornou False",
                        })

                    log.info(
                        f"[{indice}/{total}] SKU={sku} | status={status.upper()} | "
                        f"consulta={tempo_consulta:.2f}s | "
                        f"atualização={tempo_atualizacao:.2f}s"
                    )

            except Exception as erro_sku:
                falhas += 1
                status = "erro"
                falhas_detalhadas.append({
                    "sku": sku,
                    "etapa": etapa,
                    "motivo": str(erro_sku)[:300],
                })
                log.error(
                    f"[{indice}/{total}] SKU={sku} | ERRO na etapa {etapa}: {erro_sku}",
                    exc_info=True,
                )

            # Mantém o delay atual entre produtos.
            if DELAY_ENTRE_SKUS > 0 and indice < total:
                time.sleep(DELAY_ENTRE_SKUS)

            tempo_sku = time.perf_counter() - sku_inicio
            tempos_skus.append({
                "sku": sku,
                "segundos": round(tempo_sku, 2),
                "status": status,
            })

            _progresso_atual.update({
                "processados": indice,
                "percentual": round((indice / total) * 100, 1) if total else 100,
            })

            log.info(
                f"[{indice}/{total}] SKU={sku} | FIM | "
                f"tempo_total_sku={tempo_sku:.2f}s | "
                f"progresso={_progresso_atual['percentual']}%"
            )

        # --------------------------------------------------------
        # Métricas finais
        # --------------------------------------------------------
        fim = datetime.now(TZ_SP)
        duracao = time.perf_counter() - inicio_perf
        tempo_processamento_omie = max(0, duracao - tempo_tpl)
        media_por_sku = (
            sum(item["segundos"] for item in tempos_skus) / len(tempos_skus)
            if tempos_skus else 0
        )

        mais_lentos = sorted(
            tempos_skus,
            key=lambda item: item["segundos"],
            reverse=True,
        )[:QTD_SKUS_LENTOS]

        _ultimo_resultado = {
            "inicio": inicio.strftime("%d/%m/%Y %H:%M:%S"),
            "fim": fim.strftime("%d/%m/%Y %H:%M:%S"),
            "total": total,
            "ok": ok,
            "falhas": falhas,
            "nao_encontrados": nao_encontrados,
            "duracao_segundos": round(duracao, 2),
            "duracao_formatada": _formatar_duracao(duracao),
            "tempo_consulta_tpl_segundos": round(tempo_tpl, 2),
            "tempo_processamento_omie_segundos": round(tempo_processamento_omie, 2),
            "media_segundos_por_sku": round(media_por_sku, 2),
            "skus_por_minuto": round((total / duracao) * 60, 2) if duracao > 0 else 0,
            "delay_entre_skus_segundos": DELAY_ENTRE_SKUS,
            "skus_mais_lentos": mais_lentos,
            "skus_nao_encontrados": nao_encontrados_lista,
            "falhas_detalhadas": falhas_detalhadas,
        }
        _ultimo_sync = fim
        _progresso_atual.update({
            "fase": "concluido",
            "sku_atual": None,
            "processados": total,
            "percentual": 100,
        })

        log.info("=" * 70)
        log.info("SYNC CONCLUÍDO")
        log.info(
            f"Total={total} | OK={ok} | Falhas={falhas} | "
            f"Não encontrados={nao_encontrados}"
        )
        log.info(
            f"Duração={_formatar_duracao(duracao)} ({duracao:.2f}s) | "
            f"TPL={tempo_tpl:.2f}s | Omie/processamento={tempo_processamento_omie:.2f}s"
        )
        log.info(
            f"Média={media_por_sku:.2f}s/SKU | "
            f"Velocidade={_ultimo_resultado['skus_por_minuto']} SKUs/min"
        )
        if mais_lentos:
            log.info(f"SKUs mais lentos: {mais_lentos}")
        if nao_encontrados_lista:
            log.warning(f"SKUs não encontrados: {nao_encontrados_lista}")
        if falhas_detalhadas:
            log.warning(f"Falhas detalhadas: {falhas_detalhadas}")
        log.info("=" * 70)

    except Exception as e:
        fim = datetime.now(TZ_SP)
        duracao = time.perf_counter() - inicio_perf
        log.error(f"Erro geral no sync: {e}", exc_info=True)
        _ultimo_resultado = {
            "status": "erro",
            "erro": str(e),
            "inicio": inicio.strftime("%d/%m/%Y %H:%M:%S"),
            "fim": fim.strftime("%d/%m/%Y %H:%M:%S"),
            "duracao_segundos": round(duracao, 2),
            "duracao_formatada": _formatar_duracao(duracao),
        }
        _ultimo_sync = fim
        _progresso_atual["fase"] = "erro"

    finally:
        _executando = False
        _inicio_sync_atual = None


def _formatar_duracao(segundos):
    segundos = int(round(segundos))
    horas, resto = divmod(segundos, 3600)
    minutos, segundos = divmod(resto, 60)
    if horas:
        return f"{horas}h {minutos}m {segundos}s"
    if minutos:
        return f"{minutos}m {segundos}s"
    return f"{segundos}s"
