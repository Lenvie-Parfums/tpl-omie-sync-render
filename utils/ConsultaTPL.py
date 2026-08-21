"""
ConsultaTPL.py
Lê o estoque atual do WMS da TPL via API e retorna lista de SKUs com saldo.

Endpoints utilizados:
  POST /api/get/auth  → gera token dinâmico (válido 1h, não pode chamar 2x)
  POST /api/get/stock → retorna saldo de todos os SKUs ativos

Credenciais necessárias (Secrets do GitHub):
  TPL_APIKEY  → fornecido pelo comercial/projetos TPL
  TPL_TOKEN   → fornecido pelo comercial/projetos TPL
  TPL_EMAIL   → e-mail autorizado a utilizar o serviço
  TPL_BASE_URL → ex: https://oms.tpl.com.br/api
"""
import os
import json
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

TPL_BASE_URL = os.getenv("TPL_BASE_URL", "https://oms.tpl.com.br/api")
TPL_APIKEY   = os.getenv("TPL_APIKEY")
TPL_TOKEN    = os.getenv("TPL_TOKEN")
TPL_EMAIL    = os.getenv("TPL_EMAIL")

MAX_RETRIES  = 3
RETRY_DELAY  = 10


def autenticar_tpl():
    """
    Gera token dinâmico via get/auth.
    ATENÇÃO: o token é válido por 1h e não pode ser solicitado mais de uma vez
    nesse período (retorna code 400 na segunda tentativa).
    O token é gerado uma vez no início da execução e reutilizado.
    """
    if not all([TPL_APIKEY, TPL_TOKEN, TPL_EMAIL]):
        raise RuntimeError(
            "Credenciais da TPL ausentes. "
            "Confira TPL_APIKEY, TPL_TOKEN e TPL_EMAIL nos Secrets."
        )

    url = f"{TPL_BASE_URL}/get/auth"
    payload = {
        "apikey": TPL_APIKEY,
        "token":  TPL_TOKEN,
        "email":  TPL_EMAIL,
    }

    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=30
            )
            data = response.json()

            if response.status_code == 200 and ("token" in data or "auth" in data):
                auth_value = data.get("token") or data.get("auth")
                log.info(f"TPL: autenticado com sucesso. id={data.get('id')}")
                return auth_value

            code = data.get("code", response.status_code)

            # code 400 = já há um auth em uso (token ainda válido de execução anterior)
            # Isso não deveria acontecer no GitHub Actions (nova execução = novo ambiente)
            # mas tratamos por segurança.
            if code == 400:
                log.warning("TPL: auth code 400 — token ainda em uso. Aguardando 65s...")
                time.sleep(65)
                continue

            log.error(f"TPL: falha na autenticação (code={code}): {data}")
            raise RuntimeError(f"Falha na autenticação TPL: code={code}")

        except requests.exceptions.RequestException as e:
            log.warning(f"TPL: erro de conexão na autenticação (tentativa {tentativa}): {e}")
            if tentativa < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError("TPL: falha definitiva na autenticação após retries.")


def rodarAPITPL():
    """
    Consulta o estoque atual de todos os SKUs ativos na TPL.
    Retorna lista de dicts: [{"sku": ..., "available": balance}, ...]

    O campo 'balance' da TPL representa o saldo atual do período.
    Usamos start=hoje para garantir que o saldo reflita a posição atual.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    auth = autenticar_tpl()
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y")

    url = f"{TPL_BASE_URL}/get/stock"
    payload = {
        "auth":   auth,   # campo exigido pelo get/stock conforme doc
        "start":  hoje,
        "resume": 1,
        "sku": [{"sku": "*"}]
    }

    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=60
            )
            log.info(f"TPL get/stock status HTTP: {response.status_code}")
            log.info(f"TPL get/stock retorno bruto: '{response.text[:300]}'")

            if not response.text or not response.text.strip():
                log.warning(f"TPL: get/stock retornou resposta vazia (tentativa {tentativa})")
                time.sleep(RETRY_DELAY)
                continue

            data = response.json()
            code = data.get("code", response.status_code)

            if code == 200:
                estoque = data.get("stock", [])
                # Log do primeiro item pra diagnóstico
                if estoque:
                    log.info(f"TPL retorno exemplo (1o item): {json.dumps(estoque[0])}")
                resultados = []
                for item in estoque:
                    if item.get("code") != 200:
                        log.warning(f"TPL: SKU {item.get('sku')} retornou code={item.get('code')}. Pulando.")
                        continue
                    # amount = disponível físico → PADRAO
                    # reserved = reservado/bloqueado → AVARIAS (campo confirmado na doc TPL)
                    saldo    = int(item.get("amount", 0) or 0)
                    reserved = int(item.get("reserved", 0) or 0)

                    # Lote/validade via campo 'more'
                    lotes = []
                    for lote in (item.get("more") or []):
                        if lote.get("lote") or lote.get("valid"):
                            lotes.append({
                                "lote":   lote.get("lote", ""),
                                "valid":  lote.get("valid", ""),
                                "amount": int(lote.get("amount", 0) or 0),
                            })

                    if saldo > 0 or reserved > 0:
                        log.info(f"TPL SKU {item['sku']}: amount={saldo} | reserved={reserved} | lotes={lotes}")

                    resultados.append({
                        "sku":       item["sku"],
                        "available": saldo,
                        "blocked":   reserved,
                        "lotes":     lotes,
                    })

                log.info(f"TPL: {len(resultados)} SKUs recebidos com sucesso.")
                return resultados

            log.warning(f"TPL: get/stock retornou code={code} (tentativa {tentativa})")
            time.sleep(RETRY_DELAY)

        except requests.exceptions.RequestException as e:
            log.warning(f"TPL: erro de conexão em get/stock (tentativa {tentativa}): {e}")
            if tentativa < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError("TPL: falha definitiva em get/stock após retries.")
