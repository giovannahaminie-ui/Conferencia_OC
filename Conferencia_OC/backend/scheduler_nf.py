"""
scheduler_nf.py
─────────────────────────────────────────────────────────────────────────────
Scheduler unificado — roda a cada 1 minuto e faz, numa rodada só:
  1. Busca NFs novas via /api/nf/dashboard e entra elas no fluxo
     (nf_processor.processar_nf_nova).
  2. Reconfere toda NF em AGUARDANDO_LIGACAO (reconferir_ligacao_pendente).
  3. Reconfere toda NF em AGUARDANDO_APROVACAO_OC (reconferir_oc_pendente).
  4. Reprocessa toda NF presa num estado de passagem
     (VERIFICACAO_CADASTRAL / VERIFICACAO_OC) — garante que nenhuma NF fique
     travada se uma etapa deu exceção numa rodada anterior.

Cada NF é tocada NO MÁXIMO 1 VEZ por rodada (ver `tocadas` abaixo): os passos
2/3/4 buscam a lista de NFs de cada estágio "ao vivo" (listar_nfs_no_estagio),
e um passo anterior pode ter acabado de mudar o estágio de uma NF pra um dos
estágios que o próximo passo também busca — sem esse controle, essa NF seria
reprocessada de novo NA MESMA RODADA (inclusive reexecutando webservice de
verdade), em vez de esperar a rodada seguinte, como os comentários de cada
etapa prometem ("reprocessa na próxima rodada").
─────────────────────────────────────────────────────────────────────────────

"""

from apscheduler.schedulers.background import BackgroundScheduler
import requests

from nf_processor import (
    processar_nf_nova,
    reconferir_ligacao_pendente,
    reconferir_oc_pendente,
    reprocessar_travada,
    reconferir_integracao,
    reconferir_integrada,
    listar_nfs_integradas_recentes,
    listar_nfs_no_estagio,
    listar_nfs_ativas,
    AGUARDANDO_LIGACAO,
    AGUARDANDO_APROVACAO_OC,
    VERIFICACAO_CADASTRAL,
    VERIFICACAO_OC,
)

FLASK_URL = "http://localhost:5005"


# ─────────────────────────────────────────────────────────────────────────────

def conferir_nfs():
    """
    Job principal — roda a cada 1 minuto.
    """
    print("\n🕐 [Scheduler] Iniciando rodada de conferência...")

    resultados: dict[str, int] = {}
    # NFs já tocadas nesta rodada (por numnfc) — nenhum passo abaixo
    # reprocessa uma NF que outro passo já tenha processado agora mesmo,
    # mesmo que ela apareça na lista de outro estágio no meio do caminho.
    tocadas: set[str] = set()

    # ── 1. NFs novas ─────────────────────────────────────────────────────
    try:
        resp = requests.post(f"{FLASK_URL}/api/nf/dashboard", timeout=60)
        resp.raise_for_status()
        data = resp.json()
        nfs = data.get("nfs", data.get("divergentes", []))
    except Exception as e:
        print(f"❌ [Scheduler] Erro ao buscar dashboard: {e}")
        nfs = []

    print(f"   NFs candidatas (novas ou já vistas): {len(nfs)}")
    for nf in nfs:
        nf_normalizada = {k.lower(): v for k, v in nf.items()}
        numnfc = str(nf_normalizada.get("numnfc") or "")
        if numnfc in tocadas:
            continue
        resultado = processar_nf_nova(nf_normalizada)
        s = resultado.get("resultado", "DESCONHECIDO")
        # "JA_PROCESSADA" é um no-op (a NF já tinha registro, nada foi feito
        # de verdade) — NÃO marca como tocada, senão uma NF que o dashboard
        # sempre devolve (ex: filtro fixo de teste) ficaria bloqueada pra
        # sempre nos passos 2/3/4, mesmo sem nunca ter sido reprocessada de
        # fato. Só marca quando processar_nf_nova realmente executou algo.
        if s != "JA_PROCESSADA":
            tocadas.add(numnfc)
        resultados[s] = resultados.get(s, 0) + 1

    # ── 2. Reconfere ligação pendente (coluna "Aguardando Lig. Produto x Fornecedor") ──
    pendentes_ligacao = [r for r in listar_nfs_no_estagio(AGUARDANDO_LIGACAO) if str(r["numnfc"]) not in tocadas]
    print(f"   Reconferindo {len(pendentes_ligacao)} NF(s) em AGUARDANDO_LIGACAO...")
    for registro in pendentes_ligacao:
        resultado = reconferir_ligacao_pendente(registro)
        tocadas.add(str(registro["numnfc"]))
        s = resultado.get("resultado", "DESCONHECIDO")
        resultados[s] = resultados.get(s, 0) + 1

    # ── 3. Reconfere OC pendente (coluna "Aguard. aprovação de OC") ─────
    pendentes_oc = [r for r in listar_nfs_no_estagio(AGUARDANDO_APROVACAO_OC) if str(r["numnfc"]) not in tocadas]
    print(f"   Reconferindo {len(pendentes_oc)} NF(s) em AGUARDANDO_APROVACAO_OC...")
    for registro in pendentes_oc:
        resultado = reconferir_oc_pendente(registro)
        tocadas.add(str(registro["numnfc"]))
        s = resultado.get("resultado", "DESCONHECIDO")
        resultados[s] = resultados.get(s, 0) + 1

    # ── 4. Reprocessa NFs presas em estado de passagem ──────────────────
    travadas_brutas = listar_nfs_no_estagio(VERIFICACAO_CADASTRAL) + listar_nfs_no_estagio(VERIFICACAO_OC)
    travadas = [r for r in travadas_brutas if str(r["numnfc"]) not in tocadas]
    if travadas:
        print(f"   Reprocessando {len(travadas)} NF(s) presa(s) em VERIFICACAO_CADASTRAL/VERIFICACAO_OC...")
    for registro in travadas:
        resultado = reprocessar_travada(registro)
        tocadas.add(str(registro["numnfc"]))
        s = resultado.get("resultado", "DESCONHECIDO")
        resultados[s] = resultados.get(s, 0) + 1

    print(f"\n📊 [Scheduler] Resumo da rodada:")
    for status, qtd in resultados.items():
        print(f"   {status}: {qtd}")
    print("─" * 60)

def conferir_nfs_integrada():
    """ 2ª verificação — job próprio, a cada 2 minutos. Passa por TODA NF ainda no
    fluxo e checa se ela já apareceu na E440NFC (já foi integrada de verdade).
    Se sim, manda pro estágio terminal INTEGRADA e ela some do board.
    """

    print("\n🔁 [Scheduler-2ªverif] Conferindo NFs já integradas (E440NFC)...")
    ativas = listar_nfs_ativas()
    print(f" {len(ativas)} NF(s) no fluxo para checar")
    saíram = 0
    for registro in ativas:
        resultado = reconferir_integracao(registro)
        if resultado.get("resultado") == "INTEGRADA":
            saíram += 1
    print(f" {saíram} NF(s) marcada(s) como INTEGRADA e removida(s) do board")

    # INTEGRADA é terminal, mas a NF pode ter sido excluída/estornada no
    # Sapiens depois (some da E440NFC) — devolve ao fluxo as INTEGRADA
    # recentes que não constam mais lá. Roda DEPOIS do loop acima de propósito:
    # uma NF que acabou de virar INTEGRADA nesta rodada já consta na E440NFC.
    integradas = listar_nfs_integradas_recentes()
    voltaram = 0
    for registro in integradas:
        resultado = reconferir_integrada(registro)
        if resultado.get("resultado") == VERIFICACAO_OC:
            voltaram += 1
    print(f" {len(integradas)} NF(s) INTEGRADA recente(s) reconferida(s), {voltaram} voltou(aram) pro fluxo")
    print("-" * 60)

# ─────────────────────────────────────────────────────────────────────────────

def iniciar_scheduler_nf():
    scheduler = BackgroundScheduler()
    scheduler.add_job(conferir_nfs, "interval", minutes=1, id="conferir_nfs",
                      max_instances=1, coalesce=True)
    scheduler.add_job(conferir_nfs_integrada, "interval", minutes=2, id="conferir_nfs_integradas",
                      max_instances=1, coalesce=True)
    scheduler.start()
    print("🕐 Scheduler NF iniciado — rodada 1 min + 2ª verificação (E440NFC) a cada 2 min")
