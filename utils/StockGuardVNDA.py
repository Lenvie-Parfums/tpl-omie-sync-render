"""Proteção de ruptura VNDA. Não libera estoque positivo.

Sem controle da integração Omie->VNDA, a escrita zero não constitui trava
permanente; reconcilia o zero em cada ciclo quando o modo ativo está ligado.
"""
import logging
import os
import time
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)


def _enabled(name):
    return os.getenv(name, "false").strip().lower() in ("true", "1", "yes")


def proteger_rupturas(produtos):
    """Executa antes do loop Omie; somente SKUs TPL com available == 0.

    Requer STOCK_GUARD_ENABLED=true, VNDA_TOKEN e VNDA_SHOP_HOST.
    Em modo de teste, apenas registra candidatos, sem chamar a VNDA.
    """
    enabled = _enabled("STOCK_GUARD_ENABLED")
    dry_run = _enabled("STOCK_GUARD_DRY_RUN")
    resumo = {"ativo": enabled, "dry_run": dry_run, "rupturas": 0,
              "ja_zerados": 0, "zerados": 0, "falhas": 0}
    if not enabled:
        return resumo

    token = os.getenv("VNDA_TOKEN", "").strip()
    host = os.getenv("VNDA_SHOP_HOST", "").strip()
    base = os.getenv("VNDA_BASE_URL", "https://api.vnda.com.br").rstrip("/")
    if not dry_run and (not token or not host):
        raise RuntimeError("STOCK GUARD: VNDA_TOKEN e VNDA_SHOP_HOST obrigatorios")

    headers = {"Authorization": f"Bearer {token}", "X-Shop-Host": host}
    vistos = set()
    for produto in produtos:
        sku = str(produto.get("sku", "")).strip()
        if not sku or sku in vistos:
            continue
        vistos.add(sku)
        try:
            saldo = float(produto["available"])
        except (KeyError, TypeError, ValueError):
            log.error("[STOCK GUARD] saldo TPL invalido SKU=%s", sku)
            resumo["falhas"] += 1
            continue
        if saldo != 0:
            continue
        resumo["rupturas"] += 1
        if dry_run:
            log.warning("[STOCK GUARD] DRY RUN SKU=%s TPL=0; nenhuma escrita VNDA", sku)
            continue

        url = f"{base}/api/v2/variants/{quote(sku, safe='')}"
        for tentativa in range(1, 4):
            try:
                consulta = requests.get(url, headers=headers, timeout=15)
                consulta.raise_for_status()
                dados = consulta.json()
                if str(dados.get("sku", "")) != sku:
                    raise ValueError("SKU retornado pela VNDA nao confere")
                # stock é a quantidade disponível na API VNDA.
                estoque = dados.get("stock")
                if estoque is not None and float(estoque) <= 0 and dados.get("restocking_enabled") is False:
                    resumo["ja_zerados"] += 1
                    break
                resposta = requests.post(
                    url + "/quantity", headers=headers,
                    params={"quantity": 0, "restocking_enabled": "false"}, timeout=20,
                )
                resposta.raise_for_status()
                corpo = resposta.json()
                if corpo.get("status") != "ok":
                    raise ValueError(f"Resposta VNDA inesperada: {str(corpo)[:180]}")
                log.warning("[STOCK GUARD] SKU=%s TPL=0 VNDA_antes=%s zerado; tentativa=%s",
                            sku, estoque, tentativa)
                resumo["zerados"] += 1
                break
            except (requests.RequestException, ValueError) as exc:
                log.error("[STOCK GUARD] falha SKU=%s tentativa=%s: %s", sku, tentativa, exc)
                if tentativa == 3:
                    resumo["falhas"] += 1
                else:
                    time.sleep(2 ** tentativa)
    log.info("[STOCK GUARD RESUMO] %s", resumo)
    return resumo
