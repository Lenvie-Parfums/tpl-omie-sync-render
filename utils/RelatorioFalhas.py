"""
RelatorioFalhas.py
Coleta tudo que NÃO sincronizou numa execução e exporta no final.

Saídas (cada uma independente — se uma falhar, as outras seguem e o sync não quebra):
  1. CSV em ./relatorios/  -> vira artifact no GitHub Actions
  2. Resumo da execução    -> aba "Summary" do run no GitHub Actions
  3. Slack                 -> só se houver falha (SLACK_WEBHOOK_URL)
  4. Google Sheets         -> histórico na aba Falhas_Sync (GOOGLE_SA_JSON + SHEET_FALHAS_ID)

Uso no main.py:
    from utils.RelatorioFalhas import RelatorioFalhas
    rel = RelatorioFalhas(origem="tpl-omie-sync")
    ...
    rel.registrar("ERRO_OMIE", sku, detalhe=faultstring, saldo_tpl=amount)
    rel.ok()
    ...
    rel.exportar()
"""

import csv
import json
import os
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Sao_Paulo")

TIPOS = {
    "SEM_DEPARA":      "SKU do TPL sem correspondência no Omie",
    "ERRO_OMIE":       "Omie recusou o ajuste de estoque",
    "ERRO_TPL":        "Falha ao ler saldo no TPL",
    "ZEROU_NA_RODADA": "Tinha saldo no Omie e zerou no TPL — risco de venda sem estoque desde o último sync",
    "ESTOQUE_BAIXO":   "Saldo no TPL abaixo do mínimo de segurança",
    "SO_NO_OMIE":      "SKU com saldo no Omie que não veio do TPL (verificar KIT ou de-para)",
}

COLUNAS = [
    "data_hora", "origem", "tipo", "descricao", "sku",
    "saldo_tpl", "saldo_omie_antes", "detalhe",
]

ABA_PLANILHA = "Falhas_Sync"

# Foto do estoque a cada execução — lida pelo alerta do Slack (Apps Script)
ABA_SNAPSHOT = "Estoque_Sync"
COLUNAS_SNAPSHOT = [
    "sku", "descricao", "saldo_tpl", "saldo_omie_antes",
    "saldo_enviado", "atualizado_em", "origem",
]


class RelatorioFalhas:
    def __init__(self, origem: str):
        self.origem = origem
        self.inicio = datetime.now(TZ)
        self.itens: list[dict] = []
        self.saldos: list[dict] = []
        self.total_ok = 0

    # ------------------------------------------------------------------ coleta
    def registrar(self, tipo: str, sku: str, detalhe: str = "",
                  saldo_tpl=None, saldo_omie_antes=None) -> None:
        self.itens.append({
            "data_hora": datetime.now(TZ).strftime("%d/%m/%Y %H:%M:%S"),
            "origem": self.origem,
            "tipo": tipo,
            "descricao": TIPOS.get(tipo, tipo),
            "sku": str(sku),
            "saldo_tpl": "" if saldo_tpl is None else saldo_tpl,
            "saldo_omie_antes": "" if saldo_omie_antes is None else saldo_omie_antes,
            "detalhe": str(detalhe)[:300],
        })

    def ok(self) -> None:
        self.total_ok += 1

    def registrar_saldo(self, sku: str, saldo_tpl, saldo_omie_antes=None,
                        saldo_enviado=None, descricao: str = "") -> None:
        """Chamar para TODO SKU lido do TPL — alimenta a aba Estoque_Sync."""
        self.saldos.append({
            "sku": str(sku),
            "descricao": str(descricao)[:120],
            "saldo_tpl": saldo_tpl,
            "saldo_omie_antes": "" if saldo_omie_antes is None else saldo_omie_antes,
            "saldo_enviado": saldo_tpl if saldo_enviado is None else saldo_enviado,
            "atualizado_em": datetime.now(TZ).isoformat(timespec="seconds"),
            "origem": self.origem,
        })

    @staticmethod
    def carregar_saldos_anteriores() -> dict:
        """
        Lê a aba Estoque_Sync da execução anterior -> {sku: saldo_tpl}.
        Usado para detectar SKU que zerou entre um sync e outro, sem chamada extra ao Omie.
        Em qualquer falha devolve {} (o sync segue normalmente).
        """
        sa_json = os.getenv("GOOGLE_SA_JSON")
        sheet_id = os.getenv("SHEET_FALHAS_ID")
        if not sa_json or not sheet_id:
            return {}
        try:
            import gspread

            gc = gspread.service_account_from_dict(json.loads(sa_json))
            aba = gc.open_by_key(sheet_id).worksheet(ABA_SNAPSHOT)
            anteriores = {}
            for linha in aba.get_all_records(numericise_ignore=["all"]):
                sku = str(linha.get("sku", "")).strip()
                if not sku:
                    continue
                try:
                    anteriores[sku] = float(str(linha.get("saldo_tpl", "0")).replace(",", ".") or 0)
                except ValueError:
                    anteriores[sku] = 0.0
            print(f"[relatorio] {len(anteriores)} saldos anteriores carregados de {ABA_SNAPSHOT}")
            return anteriores
        except Exception as e:  # noqa: BLE001
            print(f"[relatorio] sem saldos anteriores ({e})")
            return {}

    def contagem(self) -> Counter:
        return Counter(i["tipo"] for i in self.itens)

    def criticos(self) -> list[dict]:
        return [i for i in self.itens if i["tipo"] in ("ERRO_OMIE", "ERRO_TPL", "ZEROU_NA_RODADA", "SEM_DEPARA")]

    # ------------------------------------------------------------------ saídas
    def salvar_csv(self, pasta: str = "relatorios") -> str:
        os.makedirs(pasta, exist_ok=True)
        nome = f"falhas_{self.origem}_{self.inicio.strftime('%Y%m%d_%H%M')}.csv"
        caminho = os.path.join(pasta, nome)
        # utf-8-sig + ; para abrir certo no Excel em PT-BR
        with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLUNAS, delimiter=";")
            w.writeheader()
            w.writerows(self.itens)
        print(f"[relatorio] CSV salvo: {caminho} ({len(self.itens)} linhas)")
        return caminho

    def escrever_resumo_actions(self) -> None:
        destino = os.getenv("GITHUB_STEP_SUMMARY")
        if not destino:
            return
        linhas = [
            f"## Sync {self.origem} — {self.inicio.strftime('%d/%m/%Y %H:%M')}",
            "",
            f"- SKUs sincronizados: **{self.total_ok}**",
            f"- Ocorrências: **{len(self.itens)}**",
            "",
        ]
        if self.itens:
            linhas += ["| Tipo | Qtd |", "| --- | --- |"]
            linhas += [f"| {t} | {q} |" for t, q in self.contagem().most_common()]
            linhas += ["", "| Tipo | SKU | Saldo TPL | Omie antes | Detalhe |", "| --- | --- | --- | --- | --- |"]
            for i in self.itens[:50]:
                det = i["detalhe"].replace("|", "/")[:80]
                linhas.append(f"| {i['tipo']} | {i['sku']} | {i['saldo_tpl']} | {i['saldo_omie_antes']} | {det} |")
            if len(self.itens) > 50:
                linhas.append(f"\n_+{len(self.itens) - 50} linhas no CSV do artifact._")
        with open(destino, "a", encoding="utf-8") as f:
            f.write("\n".join(linhas) + "\n")

    def enviar_slack(self) -> None:
        url = os.getenv("SLACK_WEBHOOK_URL")
        if not url or not self.itens:
            return
        cont = self.contagem()
        resumo = " · ".join(f"{t}: {q}" for t, q in cont.most_common())
        crit = self.criticos()
        detalhe = "\n".join(
            f"• `{i['sku']}` {i['tipo']} — TPL {i['saldo_tpl']} / Omie antes {i['saldo_omie_antes']}"
            for i in crit[:15]
        )
        if len(crit) > 15:
            detalhe += f"\n…+{len(crit) - 15} no relatório"
        link = ""
        if os.getenv("GITHUB_RUN_ID"):
            link = (f"\n<{os.getenv('GITHUB_SERVER_URL')}/{os.getenv('GITHUB_REPOSITORY')}"
                    f"/actions/runs/{os.getenv('GITHUB_RUN_ID')}|Abrir execução e CSV>")
        texto = (f"*Sync {self.origem}* — {self.inicio.strftime('%d/%m %H:%M')}\n"
                 f"{self.total_ok} SKUs ok · {len(self.itens)} ocorrências\n{resumo}\n{detalhe}{link}")
        r = requests.post(url, json={"text": texto}, timeout=15)
        r.raise_for_status()

    def gravar_planilha(self) -> None:
        sa_json = os.getenv("GOOGLE_SA_JSON")
        sheet_id = os.getenv("SHEET_FALHAS_ID")
        if not sa_json or not sheet_id or not self.itens:
            return
        import gspread  # só importa se for usar

        gc = gspread.service_account_from_dict(json.loads(sa_json))
        planilha = gc.open_by_key(sheet_id)
        try:
            aba = planilha.worksheet(ABA_PLANILHA)
        except gspread.WorksheetNotFound:
            aba = planilha.add_worksheet(ABA_PLANILHA, rows=1000, cols=len(COLUNAS))
            aba.append_row(COLUNAS)
        # append em lote — 1 chamada só
        aba.append_rows([[i[c] for c in COLUNAS] for i in self.itens],
                        value_input_option="RAW")
        print(f"[relatorio] {len(self.itens)} linhas gravadas em {ABA_PLANILHA}")

    def gravar_snapshot(self) -> None:
        """Sobrescreve a aba Estoque_Sync com a foto desta execução (sempre, mesmo sem falha)."""
        sa_json = os.getenv("GOOGLE_SA_JSON")
        sheet_id = os.getenv("SHEET_FALHAS_ID")
        if not sa_json or not sheet_id or not self.saldos:
            return
        import gspread

        gc = gspread.service_account_from_dict(json.loads(sa_json))
        planilha = gc.open_by_key(sheet_id)
        try:
            aba = planilha.worksheet(ABA_SNAPSHOT)
        except gspread.WorksheetNotFound:
            aba = planilha.add_worksheet(ABA_SNAPSHOT, rows=1000, cols=len(COLUNAS_SNAPSHOT))
        linhas = [COLUNAS_SNAPSHOT] + [[s[c] for c in COLUNAS_SNAPSHOT] for s in self.saldos]
        aba.clear()
        aba.update(range_name="A1", values=linhas, value_input_option="RAW")
        print(f"[relatorio] snapshot com {len(self.saldos)} SKUs gravado em {ABA_SNAPSHOT}")

    def exportar(self) -> dict:
        """Roda todas as saídas. Nunca levanta exceção — relatório não pode derrubar o sync."""
        status = {}
        for nome, fn in [("csv", self.salvar_csv),
                         ("actions", self.escrever_resumo_actions),
                         ("slack", self.enviar_slack),
                         ("planilha", self.gravar_planilha),
                         ("snapshot", self.gravar_snapshot)]:
            try:
                fn()
                status[nome] = "ok"
            except Exception as e:  # noqa: BLE001
                status[nome] = f"erro: {e}"
                print(f"[relatorio] falha ao exportar {nome}: {e}")
        return {
            "origem": self.origem,
            "inicio": self.inicio.isoformat(),
            "total_ok": self.total_ok,
            "ocorrencias": len(self.itens),
            "por_tipo": dict(self.contagem()),
            "itens": self.itens,
            "exportacao": status,
        }
