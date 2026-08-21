import requests
import json
import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

OMIE_PRODUTO_URL = os.getenv("OMIE_PRODUTO_URL")
OMIE_ESTOQUE_URL = os.getenv("OMIE_ESTOQUE_URL")
OMIE_LOCAL_URL   = "https://app.omie.com.br/api/v1/estoque/local/"
OMIE_POSICAO_URL = "https://app.omie.com.br/api/v1/estoque/consulta/"
APP_KEY    = os.getenv("APP_KEY_OMIE")
APP_SECRET = os.getenv("APP_SECRET")

TZ_SP = ZoneInfo("America/Sao_Paulo")

def data_hoje_sp():
    return datetime.now(TZ_SP).strftime("%d/%m/%Y")

# Codigos dos locais de estoque (obtidos via ListarLocaisEstoque em 07/07/2026)
COD_LOCAL_PADRAO   = 8385256868
COD_LOCAL_AVARIAS  = 8980234760  # 0002_AVARIA (codigo: 99CTRL000201)

_cache_locais = {
    "PADRAO":  COD_LOCAL_PADRAO,
    "AVARIAS": COD_LOCAL_AVARIAS,
}

def carregar_locais_estoque():
    """Retorna o cache de locais (já pré-carregado com os codigos fixos)."""
    return _cache_locais

def obter_codigo_local(nome_local):
    """Retorna o codigo numerico do local pelo nome."""
    return _cache_locais.get(nome_local.strip().upper())

SKUS_KITS = {
    "101002022","101002021","102022380","102022150","90100001","14093020",
    "14099020","10131874","14093320","14090220","14090020","14097920",
    "14098020","14012020","10408470","10137474","10139274","10134074",
    "102026380","102059380",
    "KIT44","KIT45","KIT46","KIT47","KIT48","KIT49","KIT50",
    "KIT51","KIT52","KIT53","KIT54","KIT55","KIT56","KIT57",
    "KIT58","KIT59","KIT60","KIT61","KIT63","KIT64","KIT65",
    "KIT66","KIT67","KIT68","KIT69","KIT70","KIT05","11800001",
}


def _post_omie(url, payload, sku, max_retries=3, retry_delay=10):
    tentativa = 1
    while tentativa <= max_retries:
        try:
            response = requests.post(
                url, headers={"Content-Type": "application/json"},
                data=json.dumps(payload), timeout=60
            )
            texto = response.text
            if response.status_code == 200 and "faultstring" not in texto:
                return response
            if "Data do Movimento" in texto or "Client-101" in texto:
                return response
            if "REDUNDANT" in texto or "MISUSE_API" in texto or response.status_code in (425, 429):
                log.warning(f"[{sku}] API limitada. Esperando 60s...")
                time.sleep(60)
                tentativa += 1
                continue
            log.warning(f"[{sku}] Erro {response.status_code}: {texto[:200]}")
            time.sleep(retry_delay)
            tentativa += 1
        except requests.exceptions.RequestException as e:
            log.warning(f"[{sku}] Falha: {e}. Tentativa {tentativa}.")
            time.sleep(retry_delay)
            tentativa += 1
    return None


def consultar_produto_omie(codigo, max_retries=3, retry_delay=10, request_delay=1):
    """Retorna codigo_produto ou None."""
    payload = {
        "call": "ConsultarProduto",
        "app_key": APP_KEY, "app_secret": APP_SECRET,
        "param": [{"codigo": codigo}]
    }
    tentativa = 1
    while tentativa <= max_retries:
        try:
            response = requests.post(
                OMIE_PRODUTO_URL,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload), timeout=60
            )
            if response.status_code == 429 or "REDUNDANT" in response.text or "MISUSE_API" in response.text:
                log.warning(f"[{codigo}] Rate limit. Aguardando 60s...")
                time.sleep(60); tentativa += 1; continue
            if response.status_code != 200:
                log.warning(f"[{codigo}] Erro {response.status_code}")
                time.sleep(retry_delay); tentativa += 1; continue
            data = response.json()
            codigo_produto = data.get("codigo_produto")
            if not codigo_produto:
                log.warning(f"[{codigo}] Nao encontrado no Omie.")
                return None
            time.sleep(request_delay)
            return codigo_produto
        except requests.exceptions.RequestException as e:
            log.warning(f"[{codigo}] Falha: {e}.")
            time.sleep(retry_delay); tentativa += 1
    log.error(f"[{codigo}] Falha definitiva na consulta.")
    return None


def consultar_saldo_por_local(codigo_produto, sku):
    """
    Consulta saldo de estoque por local via ListarPosEstoque.
    Retorna dict: {codigo_local: quantidade}
    """
    payload = {
        "call": "ListarPosEstoque",
        "app_key": APP_KEY, "app_secret": APP_SECRET,
        "param": [{
            "nCodProd": codigo_produto,
            "pagina": 1,
            "registros_por_pagina": 50
        }]
    }
    resultado = {}
    try:
        response = requests.post(
            OMIE_POSICAO_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload), timeout=60
        )
        if response.status_code == 200 and "faultstring" not in response.text:
            data = response.json()
            posicoes = data.get("posicaoEstoque", [])
            for p in posicoes:
                cod_local = p.get("nCodLocalEstoque") or p.get("codigo_local_estoque")
                qtde = float(p.get("nQtde", 0) or p.get("quantidade", 0))
                if cod_local:
                    resultado[cod_local] = qtde
                    log.info(f"[{sku}] Local {cod_local}: {qtde} un")
        else:
            log.warning(f"[{sku}] ListarPosEstoque falhou: {response.text[:200]}")
    except Exception as e:
        log.warning(f"[{sku}] Erro ao consultar saldo: {e}")
    return resultado


def _ajustar_local(codigo_produto, sku, quan_alvo, codigo_local, nome_local,
                   saldo_atual, tipo_ajuste_kit=None, max_retries=3, retry_delay=10):
    """
    Ajusta o estoque de um produto num local específico.
    - PA: usa SLD (saldo direto) — sem codigo_local_estoque (vai pro local padrão)
    - Kit no local AVARIAS: usa ENT/SAI calculando diferença
    - Kit no local PADRAO: usa ENT/SAI calculando diferença
    """
    diferenca = float(quan_alvo) - float(saldo_atual)

    if diferenca == 0:
        log.info(f"[{sku}] {nome_local} ja correto ({saldo_atual}). Nada a fazer.")
        return True

    # PA: SLD no local PADRAO (sem especificar local = usa o padrão)
    if tipo_ajuste_kit is None:
        tipo = "SLD"
        ajuste = quan_alvo
        valor = 0
    else:
        # Kit: ENT ou SAI com diferença
        tipo = "ENT" if diferenca > 0 else "SAI"
        ajuste = abs(diferenca)
        valor = 0.01

    log.info(f"[{sku}] {nome_local}: saldo={saldo_atual} | alvo={quan_alvo} | "
             f"tipo={tipo} | ajuste={ajuste:.0f}")

    param = {
        "id_prod": codigo_produto,
        "data": data_hoje_sp(),
        "quan": str(int(ajuste)) if tipo_ajuste_kit else str(quan_alvo),
        "obs": "Ajuste automatico por API",
        "origem": "AJU",
        "tipo": tipo,
        "motivo": "INV",
        "valor": valor
    }
    # Especifica o local de estoque (AVARIAS)
    if codigo_local is not None:
        param["codigo_local_estoque"] = codigo_local

    payload = {
        "call": "IncluirAjusteEstoque",
        "app_key": APP_KEY, "app_secret": APP_SECRET,
        "param": [param]
    }
    response = _post_omie(OMIE_ESTOQUE_URL, payload, sku, max_retries, retry_delay)
    if response is None:
        log.error(f"[{sku}] Falha definitiva ao ajustar {nome_local}.")
        return False
    if response.status_code == 200 and "faultstring" not in response.text:
        log.info(f"[{sku}] {nome_local} atualizado! alvo={quan_alvo}")
        return True
    log.error(f"[{sku}] Falha {nome_local}: {response.text[:200]}")
    return False


def atualizar_estoque_omie(codigo_produto, quan_disponivel, sku, max_retries=3, retry_delay=10):
    """
    Atualiza PA:
    - Disponível Estoca → local PADRAO via SLD
    - Bloqueado Estoca  → local AVARIAS via ENT/SAI
    """
    # SLD no local padrão (não precisa especificar o local)
    return _ajustar_local(
        codigo_produto, sku, quan_disponivel,
        codigo_local=None, nome_local="PADRAO",
        saldo_atual=0,  # SLD substitui, nao precisa calcular diferença
        tipo_ajuste_kit=None,
        max_retries=max_retries, retry_delay=retry_delay
    )


def atualizar_estoque_omie_com_bloqueado(codigo_produto, quan_disponivel,
                                          quan_bloqueado, sku,
                                          max_retries=3, retry_delay=10):
    """
    Atualiza PA com dois locais:
    - Disponível → PADRAO (SLD — sempre grava, independente do saldo atual)
    - Bloqueado  → AVARIAS (SLD direto)
    """
    # 1. Atualiza PADRAO via SLD — sempre grava pra garantir consistência
    log.info(f"[{sku}] PADRAO: gravando saldo {quan_disponivel} via SLD")
    payload_padrao = {
        "call": "IncluirAjusteEstoque",
        "app_key": APP_KEY, "app_secret": APP_SECRET,
        "param": [{
            "id_prod": codigo_produto,
            "data": data_hoje_sp(),
            "quan": str(int(float(quan_disponivel))),
            "obs": "Ajuste automatico por API",
            "origem": "AJU",
            "tipo": "SLD",
            "motivo": "INV",
            "valor": 0
        }]
    }
    response = _post_omie(OMIE_ESTOQUE_URL, payload_padrao, sku, max_retries, retry_delay)
    if response and response.status_code == 200 and "faultstring" not in response.text:
        log.info(f"[{sku}] PADRAO atualizado! alvo={quan_disponivel}")
        ok_padrao = True
    else:
        log.error(f"[{sku}] PADRAO falhou: {response.text[:150] if response else 'sem resposta'}")
        ok_padrao = False

    # 2. Atualiza AVARIAS via SLD
    ok_avarias = True
    cod_avarias = obter_codigo_local("AVARIAS")
    if cod_avarias:
        log.info(f"[{sku}] AVARIAS: gravando saldo {quan_bloqueado} via SLD")
        payload_avarias = {
            "call": "IncluirAjusteEstoque",
            "app_key": APP_KEY, "app_secret": APP_SECRET,
            "param": [{
                "id_prod": codigo_produto,
                "data": data_hoje_sp(),
                "quan": str(int(float(quan_bloqueado))),
                "obs": "Ajuste automatico por API",
                "origem": "AJU",
                "tipo": "SLD",
                "motivo": "INV",
                "valor": 0,
                "codigo_local_estoque": cod_avarias
            }]
        }
        response2 = _post_omie(OMIE_ESTOQUE_URL, payload_avarias, sku, max_retries, retry_delay)
        if response2 and response2.status_code == 200 and "faultstring" not in response2.text:
            log.info(f"[{sku}] AVARIAS atualizada! alvo={quan_bloqueado}")
        else:
            log.warning(f"[{sku}] AVARIAS falhou: {response2.text[:150] if response2 else 'sem resposta'}")
            ok_avarias = False
    else:
        log.warning(f"[{sku}] Codigo do local AVARIAS nao encontrado.")

    return ok_padrao and ok_avarias


def atualizar_estoque_kit(codigo_produto, quan_estoca, sku,
                           max_retries=3, retry_delay=10):
    """
    Atualiza Kit no Omie.
    Tenta SLD primeiro. Se rejeitado (tipo inválido para kit), usa ENT/SAI.
    Evita ListarPosEstoque que está causando bloqueio.
    """
    log.info(f"[{sku}] Kit: tentando SLD com valor {quan_estoca}")

    payload_sld = {
        "call": "IncluirAjusteEstoque",
        "app_key": APP_KEY, "app_secret": APP_SECRET,
        "param": [{
            "id_prod": codigo_produto,
            "data": data_hoje_sp(),
            "quan": str(quan_estoca),
            "obs": "Ajuste automatico por API",
            "origem": "AJU",
            "tipo": "SLD",
            "motivo": "INV",
            "valor": 0.01,
            "codigo_local_estoque": COD_LOCAL_PADRAO
        }]
    }
    response = _post_omie(OMIE_ESTOQUE_URL, payload_sld, sku, max_retries=1, retry_delay=retry_delay)

    if response and response.status_code == 200 and "faultstring" not in response.text:
        log.info(f"[{sku}] Kit atualizado via SLD! quantidade={quan_estoca}")
        return True

    # SLD rejeitado — usa ENT com o valor alvo
    # (sem consulta de saldo pra evitar bloqueio da API)
    log.info(f"[{sku}] SLD falhou — usando ENT com alvo={quan_estoca}")
    payload_ent = {
        "call": "IncluirAjusteEstoque",
        "app_key": APP_KEY, "app_secret": APP_SECRET,
        "param": [{
            "id_prod": codigo_produto,
            "data": data_hoje_sp(),
            "quan": str(int(float(quan_estoca))),
            "obs": "Ajuste automatico por API",
            "origem": "AJU",
            "tipo": "ENT",
            "motivo": "INV",
            "valor": 0.01,
            "codigo_local_estoque": COD_LOCAL_PADRAO
        }]
    }
    response2 = _post_omie(OMIE_ESTOQUE_URL, payload_ent, sku, max_retries, retry_delay)
    if response2 and response2.status_code == 200 and "faultstring" not in response2.text:
        log.info(f"[{sku}] Kit atualizado via ENT! alvo={quan_estoca}")
        return True
    log.error(f"[{sku}] Falha kit: {response2.text[:200] if response2 else 'sem resposta'}")
    return False

