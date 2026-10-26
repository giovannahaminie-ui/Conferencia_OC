"""
nf_processor.py
─────────────────────────────────────────────────────────────────────────────
Orquestrador do fluxo de conferência de NF de entrada — modelo de 5 estágios,
com reconferência periódica (a cada 1min, ver scheduler_nf.py) e "dupla
checagem" ao resolver uma pendência (volta um estágio antes de seguir):

  Nova NF
  │
  ├─ VERIFICACAO_CADASTRAL — marca E000NFC.SITNFC=6 + confere, nesta ordem:
  │     1) portfólio do fornecedor (E403FPR)
  │     2) derivação (E075DER) e vínculo de depósito/estoque (E210EST) dos
  │        produtos já ligados ao portfólio
  │   ├─ Pendência (1 ou 2) → AGUARDANDO_LIGACAO (WhatsApp fiscal)
  │   └─ OK        → salva estágio VERIFICACAO_OC e PARA (uma etapa por
  │       rodada — a Verificação OC de fato só roda na próxima rodada do
  │       scheduler, via reprocessar_travada)
  │
  ├─ AGUARDANDO_LIGACAO — reconfere chamando VERIFICACAO_CADASTRAL do zero
  │   (as duas checagens, 1 e 2, de novo — mais simples que uma reconferência
  │   estreita por tipo de pendência, e o custo é o mesmo, ambas já filtradas
  │   por CHVNEL)
  │   ├─ Ainda pendente → reenvia WhatsApp fiscal (uma vez por pendência —
  │       ver _registrar_estagio), permanece
  │   └─ Resolvido      → segue direto: salva VERIFICACAO_OC e PARA (idem
  │       acima — roda na rodada seguinte)
  │
  ├─ VERIFICACAO_OC — verifica saldo de OC ITEM A ITEM, em ordem, e PARA no
  │   primeiro item sem cobertura (não olha os demais itens ainda — um erro
  │   por vez). Itens já vinculados (NUMOCP na E000IPC) numa passada anterior
  │   são pulados sem reprocessar.
  │   ├─ Chegou ao fim sem achar pendência → AGUARDANDO_INTEGRACAO
  │   └─ Achou item sem OC                 → AGUARDANDO_APROVACAO_OC (WhatsApp
  │       Junior — mensagem só com ESSE produto; TEMPORÁRIO até o webservice
  │       de geração/cancelamento de OC existir)
  │
  ├─ AGUARDANDO_APROVACAO_OC — NUMOCP fica em `referencia` (por enquanto,
  │   preenchido manualmente por alguém direto no banco quando a OC é criada
  │   no Sapiens — depois do webservice, isso vira automático)
  │   ├─ NUMOCP vazio               → reenvia WhatsApp Junior, permanece
  │   ├─ NUMOCP preenchido, OC ainda aberta (SITOCP IN 1,2) → reenvia, permanece
  │   └─ NUMOCP preenchido, OC fechada (SITOCP NOT IN 1,2)  → volta pra
  │       VERIFICACAO_OC (reconfere o item, dupla checagem)
  │
  └─ AGUARDANDO_INTEGRACAO — confere E440NFC (idempotente) e marca pronta
      pra importação se ainda não estiver.

Se alguma etapa falhar (exceção, não divergência de negócio), a NF
simplesmente permanece no estágio atual — não existe status de erro
separado; a próxima rodada do scheduler tenta de novo.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import traceback

from db_local   import get_local_connection, registrar_log_acao
from oc_service import (
    verificar_portfolio_fornecedor,
    verificar_derivacao_deposito,
    marcar_nf_aguardando_wms,
    marcar_nf_pronta_importacao,
    verificar_pronto_integracao,
    listar_seqipc_nf,
    buscar_item_nf,
    contar_e_somar_ocs,
    numocp_vinculo_valido,
    reservar_item_oc,
    vincular_item_oc,
    limpar_vinculo_item_oc,
    cancelar_item_oc_via_webservice,
    gerar_oc_aprovada_via_webservice,
    gerar_oc_para_aprovacao,
    verificar_oc_aprovada,
    oc_situacao,
    descricao_sitocp,
    nf_esta_cancelada,
    nf_ja_integrada,
    nf_consta_e440nfc,
    codpro_do_portfolio,
    buscar_seqipos_da_oc,
    listar_ocs_da_nf,
    casar_item_com_ocs_da_nf,
    assinatura_linhas_oc,
)
from whatsapp import (
    notificar_fiscal_pendencias,
    notificar_junior_criar_oc,
    notificar_junior_cancelamento_bloqueado,
)

# Chave de `referencia` (nf_processamento) do estágio AGUARDANDO_APROVACAO_OC
# quando o Sapiens recusou um cancelamento — NÃO é um NUMOCP (os leitores de
# `referencia` que tratam valores como NUMOCP precisam ignorá-la).
CANCELAMENTO_BLOQUEADO = "cancelamento_bloqueado"

# ─────────────────────────────────────────────────────────────────────────────
# Estágios do fluxo (nomes usados em nf_processamento.divergencias[0].tipo)
# ─────────────────────────────────────────────────────────────────────────────

VERIFICACAO_CADASTRAL   = "VERIFICACAO_CADASTRAL"
AGUARDANDO_LIGACAO      = "AGUARDANDO_LIGACAO"
VERIFICACAO_OC          = "VERIFICACAO_OC"
AGUARDANDO_APROVACAO_OC = "AGUARDANDO_APROVACAO_OC"
AGUARDANDO_INTEGRACAO   = "AGUARDANDO_INTEGRACAO"

# Estágio terminal — NF cancelada no Sapiens (E000NFC.INDCAN='S'), detectada
# na trava final antes de gerar OC. Diferente de VERIFICACAO_OC (que é
# reprocessado a cada rodada pelo scheduler), CANCELADA não entra em nenhuma
# lista de reconferência nem em nenhum "case" do dashboard — uma vez marcada,
# some da tela e não é mais tocada. O histórico continua em nf_processamento/
# nf_log_acoes pra auditoria, só não aparece mais como card ativo.
CANCELADA = "CANCELADA"
#Estágio da Nota que já foi integrada sem passar pelo fluxo de conferência detectada 
#na segunda verificação nf_ja_integrada.
INTEGRADA = "INTEGRADA"
# Estágios que ainda estão "no fluxo" (aparecem em alguma coluna do board) —
# a 2ª verificação (reconferir_integracao) roda em cima de todos eles.
ESTAGIOS_ATIVOS = (
    VERIFICACAO_CADASTRAL,
    AGUARDANDO_LIGACAO,
    VERIFICACAO_OC,
    AGUARDANDO_APROVACAO_OC,
    AGUARDANDO_INTEGRACAO,
)

# INTEGRADA é terminal, mas uma NF integrada pode ser excluída/estornada no
# Sapiens depois (some da E440NFC) — sem reconferir, ficaria órfã pra sempre
# (sem coluna no board e sem ser tocada pelo scheduler). Só reconfere as
# INTEGRADA mais recentes que esta janela (em dias), pra não consultar o Oracle
# por todo o histórico a cada rodada.
INTEGRADA_RECONFERIR_DIAS = 3

# ─────────────────────────────────────────────────────────────────────────────
# SQLite — estado atual da NF (nf_processamento)
# ─────────────────────────────────────────────────────────────────────────────

def _linha_com_info(row: dict) -> dict:
    """Extrai tipo/detalhe/referencia do JSON divergencias e anexa na linha."""
    try:
        divs = json.loads(row.get("divergencias") or "[]")
        info = divs[0] if divs else {}
    except Exception:
        info = {}
    row["status_interno"] = info.get("tipo", "")
    row["detalhe"]        = info.get("detalhe", "")
    row["referencia"]     = info.get("referencia")
    return row


def _buscar_estagio_atual(numnfc: str) -> dict | None:
    """Última linha de nf_processamento pra essa NF, ou None se nunca vista."""
    conn = get_local_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT * FROM nf_processamento
        WHERE numnfc = ?
        ORDER BY id DESC
        LIMIT 1
    """, (numnfc,))
    row = cur.fetchone()
    conn.close()
    return _linha_com_info(dict(row)) if row else None


def listar_nfs_no_estagio(status_interno: str) -> list[dict]:
    """
    Retorna o registro (última linha) de toda NF cujo estágio ATUAL seja o
    informado — usado pelo scheduler pra saber quais NFs reconferir em
    AGUARDANDO_LIGACAO / AGUARDANDO_APROVACAO_OC.
    """
    conn = get_local_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT t.* FROM nf_processamento t
        INNER JOIN (
            SELECT numnfc, MAX(id) AS max_id FROM nf_processamento GROUP BY numnfc
        ) u ON u.numnfc = t.numnfc AND u.max_id = t.id
    """)
    rows = [_linha_com_info(dict(r)) for r in cur.fetchall()]
    conn.close()
    return [r for r in rows if r["status_interno"] == status_interno]

def listar_nfs_ativas() -> list[dict]:
    "Retorna ultima linha de toda NF, cujo está atual ainda esteja no fluxo mesmo sendo feito a 2° verificação."
    conn = get_local_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.* FROM nf_processamento t
        INNER JOIN (
            SELECT numnfc, MAX(id) as MAX_id FROM nf_processamento 
            GROUP BY numnfc
            ) u ON u.numnfc = t.numnfc AND u.max_id = t.id 
    """)
    rows = [_linha_com_info(dict(r)) for r in cur.fetchall()]
    conn.close()
    return [r for r in rows if r["status_interno"] in ESTAGIOS_ATIVOS]

def listar_nfs_integradas_recentes(dias: int = INTEGRADA_RECONFERIR_DIAS) -> list[dict]:
    "Última linha das NFs em INTEGRADA marcadas nos últimos `dias` dias — candidatas à reconferência."
    conn = get_local_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.* FROM nf_processamento t
        INNER JOIN (
            SELECT numnfc, MAX(id) AS max_id FROM nf_processamento GROUP BY numnfc
        ) u ON u.numnfc = t.numnfc AND u.max_id = t.id
        WHERE t.criado_em >= strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime', ?)
    """, (f"-{int(dias)} days",))
    rows = [_linha_com_info(dict(r)) for r in cur.fetchall()]
    conn.close()
    return [r for r in rows if r["status_interno"] == INTEGRADA]

def _dados_nf_do_registro(registro: dict) -> dict:
    """Reconstrói o dict de dados da NF a partir de uma linha de nf_processamento."""
    return {
        "numnfc": registro["numnfc"], "chvnel": registro["chvnel"],
        "codfor": registro["codfor"], "codemp": registro["codemp"],
        "codfil": registro["codfil"], "cgcfor": registro["cgcfor"],
        "cgcfil": registro["cgcfil"], "nomfor": registro["nomfor"],
        "datemi": registro["datemi"], "vlrfin": registro["vlrfin"],
        "qtdite": registro["qtdite"],
        # NUMOCPs que o sistema já conhece pra esta NF — desempata a busca da
        # OC da NF quando há mais de uma candidata (ver _rodar_verificacao_oc).
        "referencia": registro.get("referencia"),
    }


def _registrar_estagio(dados_nf: dict, status_interno: str, detalhe: str = "", referencia=None,
                       ocp_apr: str = "", ocp_pen: str = "", ocp_can: str = "") -> None:
    """
    Grava o estágio atual da NF. Se a ÚLTIMA linha já tiver o MESMO
    status_interno, faz UPDATE em vez de INSERT — só grava linha nova quando
    o estágio realmente muda (evita empilhar uma linha a cada reconferência
    de 1min que ainda não resolveu, e sustenta a notificação única por etapa
    — ver `_marcar_notificado`/`_ja_notificado`).

    Os campos de exibição (nomfor/datemi/vlrfin/qtdite/...) são gravados
    junto — é o que permite ao dashboard continuar montando o card mesmo
    depois que a NF sumir do SELECT de /api/nf/dashboard (a coluna 1 do
    front usa esses registros locais, via `procMap`, pra saber quais NFs já
    têm estágio e não repetir o card ali).
    """
    numnfc = str(dados_nf["numnfc"])
    # status grava o nome exato do estágio atual (VERIFICACAO_CADASTRAL,
    # AGUARDANDO_LIGACAO, VERIFICACAO_OC, AGUARDANDO_APROVACAO_OC,
    # AGUARDANDO_INTEGRACAO ou CANCELADA) — dá pra consultar direto por SQL
    # sem abrir o JSON de divergencias.
    status_banco = status_interno
    divergencias = json.dumps(
        [{"tipo": status_interno, "detalhe": detalhe, "referencia": referencia}],
        ensure_ascii=False,
    )

    conn = get_local_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, divergencias FROM nf_processamento
        WHERE numnfc = ? ORDER BY id DESC LIMIT 1
    """, (numnfc,))
    ultima = cur.fetchone()

    mesmo_estagio = False
    detalhe_mudou = True
    if ultima:
        try:
            divs = json.loads(ultima[1] or "[]")
            mesmo_estagio = bool(divs) and divs[0].get("tipo") == status_interno
            if mesmo_estagio:
                detalhe_mudou = divs[0].get("detalhe") != detalhe
        except Exception:
            mesmo_estagio = False

    if mesmo_estagio:
        # Mesmo estágio, mas o DETALHE mudou (ex: NF em AGUARDANDO_LIGACAO
        # por portfólio pendente; portfólio resolve, mas aí a checagem de
        # derivação/depósito acha um problema novo — mesmo estágio, pendência
        # diferente) — zera `notificado` pra essa pendência nova ser avisada,
        # em vez de ficar presa pelo "já notificado" da pendência anterior.
        # Se o detalhe é idêntico ao da última vez, preserva `notificado`
        # (comportamento original: não reenvia WhatsApp pra pendência que não
        # mudou, mesmo reconferindo a cada 1min).
        if detalhe_mudou:
            cur.execute("""
                UPDATE nf_processamento
                   SET status = ?, divergencias = ?, ocp_apr = ?, ocp_pen = ?, ocp_can = ?,
                       notificado = 0,
                       criado_em = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
                 WHERE id = ?
            """, (status_banco, divergencias, ocp_apr, ocp_pen, ocp_can, ultima[0]))
        else:
            cur.execute("""
                UPDATE nf_processamento
                   SET status = ?, divergencias = ?, ocp_apr = ?, ocp_pen = ?, ocp_can = ?,
                       criado_em = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
                 WHERE id = ?
            """, (status_banco, divergencias, ocp_apr, ocp_pen, ocp_can, ultima[0]))
    else:
        cur.execute("""
            INSERT INTO nf_processamento
                (chvnel, numnfc, codfor, status, divergencias,
                 codemp, codfil, cgcfor, cgcfil, nomfor, datemi, vlrfin, qtdite,
                 ocp_apr, ocp_pen, ocp_can)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            dados_nf.get("chvnel", ""), numnfc, dados_nf.get("codfor", ""),
            status_banco, divergencias,
            dados_nf.get("codemp", ""), dados_nf.get("codfil", ""),
            dados_nf.get("cgcfor", ""), dados_nf.get("cgcfil", ""),
            dados_nf.get("nomfor", ""), str(dados_nf.get("datemi", "") or ""),
            dados_nf.get("vlrfin", 0) or 0, dados_nf.get("qtdite", 0) or 0,
            ocp_apr, ocp_pen, ocp_can,
        ))
    conn.commit()
    conn.close()


def _marcar_notificado(numnfc: str) -> None:
    """Marca a etapa atual da NF como já notificada — chamado logo após
    enviar o WhatsApp, pra bloquear reenvio nas reconferências seguintes
    enquanto ela ficar parada na mesma etapa."""
    conn = get_local_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE nf_processamento SET notificado = 1
         WHERE id = (SELECT id FROM nf_processamento WHERE numnfc = ? ORDER BY id DESC LIMIT 1)
    """, (str(numnfc),))
    conn.commit()
    conn.close()


def _ja_notificado(numnfc: str) -> bool:
    """Confere se a etapa ATUAL da NF já foi notificada (WhatsApp enviado
    uma vez pra esse estágio) — usado pra decidir se reenvia ou não."""
    atual = _buscar_estagio_atual(numnfc)
    return bool(atual and atual.get("notificado"))


# ─────────────────────────────────────────────────────────────────────────────
# Entrada — NF nova (nunca vista)
# ─────────────────────────────────────────────────────────────────────────────

def processar_nf_nova(nf: dict) -> dict:
    """
    Executa a entrada de uma NF nova, retornada pelo SELECT de
    /api/nf/dashboard (que só traz NF com SITNFC ainda não marcado).

    Parâmetros esperados em `nf`:
      numnfc, chvnel, cgcfor, cgcfil, codfor, codfil, codemp, nomfor,
      datemi, vlrfin, qtdite
    """
    dados_nf = {
        "numnfc": str(nf.get("numnfc") or nf.get("NUMNFC") or ""),
        "chvnel": str(nf.get("chvnel") or nf.get("CHVNEL") or ""),
        "cgcfor": str(nf.get("cgcfor") or nf.get("CGCFOR") or ""),
        "cgcfil": str(nf.get("cgcfil") or nf.get("CGCFIL") or ""),
        "codfor": str(nf.get("codfor") or nf.get("CODFOR") or ""),
        "codfil": str(nf.get("codfil") or nf.get("CODFIL") or ""),
        "codemp": str(nf.get("codemp") or nf.get("CODEMP") or ""),
        "nomfor": str(nf.get("nomfor") or nf.get("NOMFOR") or ""),
        "datemi": nf.get("datemi") or nf.get("DATEMI") or "",
        "vlrfin": nf.get("vlrfin") or nf.get("VLRFIN") or 0,
        "qtdite": nf.get("qtdite") or nf.get("QTDITE") or 0,
        "inddev": str(nf.get("inddev") or nf.get("INDDEV") or "N"),
    }
    numnfc, chvnel = dados_nf["numnfc"], dados_nf["chvnel"]

    if _buscar_estagio_atual(numnfc):
        # Segunda camada de segurança — não deveria acontecer, já que
        # /api/nf/dashboard exclui SITNFC IN (6,3).
        return {"numnfc": numnfc, "resultado": "JA_PROCESSADA", "detalhe": ""}

    print(f"\n{'─'*60}")
    print(f"🔍 Processando NF nova {numnfc} | Fornecedor: {dados_nf['nomfor']}")

    try:
        marcar_nf_aguardando_wms(chvnel, dados_nf["cgcfor"], dados_nf["cgcfil"])
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_CADASTRAL, "NF aguardando WMS", "Sit. NF = 6")
        print(f"  1️⃣  NF {numnfc} marcada (E000NFC.SITNFC=6)")

        if dados_nf["inddev"] == "S":
            # NF de devolução (IndDev='S', item com natureza 411/202) — vai
            # direto pra Aguardando Integração, sem passar por Verificação
            # Cadastral nem Verificação OC (decisão de negócio: devolução
            # não precisa de conferência de portfólio/OC).
            return _marcar_pronta_integracao(dados_nf, "NF de devolução — importação direta.")

        return _rodar_verificacao_cadastral(dados_nf)
    except Exception as e:
        traceback.print_exc()
        registrar_log_acao(numnfc, chvnel, "ERRO", "Exceção na entrada da NF nova", str(e))
        print(f"  ⚠️  NF {numnfc} — erro ao processar entrada: {e}")
        return {"numnfc": numnfc, "resultado": "ERRO", "detalhe": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Estágio 1 — Verificação Cadastral (E403FPR)
# ─────────────────────────────────────────────────────────────────────────────

def _rodar_verificacao_cadastral(dados_nf: dict) -> dict:
    numnfc, chvnel = dados_nf["numnfc"], dados_nf["chvnel"]
    codemp, codfil, codfor = dados_nf["codemp"], dados_nf["codfil"], dados_nf["codfor"]

    itens_fora_portfolio = verificar_portfolio_fornecedor(codemp, codfil, codfor, chvnel)
    # Roda as duas checagens sempre (não para na primeira) pra poder mandar
    # UMA mensagem só ao fiscal com todas as pendências cadastrais da NF —
    # verificar_derivacao_deposito só olha item que JÁ tem portfólio (JOIN em
    # E403FPR), então não duplica item já reportado em itens_fora_portfolio.
    itens_sem_cadastro = verificar_derivacao_deposito(codemp, codfor, chvnel)

    if itens_fora_portfolio or itens_sem_cadastro:
        partes_detalhe = []
        referencia = None

        if itens_fora_portfolio:
            profors = sorted({str(it["profor"]) for it in itens_fora_portfolio if it.get("profor")})
            produtos_txt = ", ".join(str(it.get("profor") or it.get("codpro") or "?") for it in itens_fora_portfolio)
            partes_detalhe.append(f"Produto(s) sem ligação no portfólio deste fornecedor: {produtos_txt}.")
            referencia = profors
            registrar_log_acao(numnfc, chvnel, VERIFICACAO_CADASTRAL, "Ligação pendente", partes_detalhe[-1])

        if itens_sem_cadastro:
            partes = []
            for it in itens_sem_cadastro:
                nome = it.get("despro") or it.get("profor") or it.get("codpro") or "?"
                faltas = [f for cond, f in (
                    (it.get("sem_derivacao"), "derivação"),
                    (it.get("sem_deposito"), "vínculo de depósito/estoque"),
                ) if cond]
                partes.append(f"{nome} ({it.get('profor') or it.get('codpro') or '—'}): sem {' e sem '.join(faltas)}")
            partes_detalhe.append("Produto(s) com cadastro incompleto: " + "; ".join(partes) + ".")
            registrar_log_acao(numnfc, chvnel, VERIFICACAO_CADASTRAL, "Cadastro de produto incompleto (derivação/depósito)", partes_detalhe[-1])

        detalhe = " ".join(partes_detalhe)
        _registrar_estagio(dados_nf, AGUARDANDO_LIGACAO, detalhe, referencia=referencia)
        if not _ja_notificado(numnfc):
            envio = notificar_fiscal_pendencias(numnfc, codfor, dados_nf["nomfor"],
                                                 sem_ligacao=itens_fora_portfolio,
                                                 cadastro_incompleto=itens_sem_cadastro)
            if envio.get("sucesso"):
                _marcar_notificado(numnfc)
                registrar_log_acao(numnfc, chvnel, "NOTIFICACAO", "WhatsApp enviado ao fiscal — pendência(s) cadastral(is)")
            else:
                registrar_log_acao(numnfc, chvnel, "ERRO", "Falha ao enviar WhatsApp ao fiscal — pendência(s) cadastral(is)", str(envio.get("erro")))
        print(f"  🏷️  {detalhe}")
        return {"numnfc": numnfc, "resultado": AGUARDANDO_LIGACAO, "detalhe": detalhe}

    # Ligação OK: decisão de negócio confirmada — uma etapa por vez, sem
    # cascatear direto pra Verificação OC na MESMA rodada. Salva o estágio
    # como VERIFICACAO_OC (não como VERIFICACAO_CADASTRAL — se salvasse o
    # próprio nome, a rodada seguinte reconferiria o cadastro de novo, veria
    # "OK" de novo, salvaria o mesmo estágio de novo e nunca chegaria na
    # Verificação OC: loop infinito). VERIFICACAO_OC sinaliza "cadastro já
    # confirmado, aguardando a Verificação OC rodar" — a próxima rodada do
    # scheduler (reprocessar_travada) já sabe ir direto pra
    # _rodar_verificacao_oc sem reconferir o cadastro.
    detalhe = "Ligação OK — todos os produtos com portfólio cadastrado. Verificação OC roda na próxima rodada."
    registrar_log_acao(numnfc, chvnel, VERIFICACAO_CADASTRAL, "Ligação OK — todos os produtos com portfólio cadastrado")
    _registrar_estagio(dados_nf, VERIFICACAO_OC, detalhe)
    print(f"  1️⃣  {detalhe}")
    return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}

def reprocessar_travada(registro: dict) -> dict:
    """
    Reprocessa uma NF que ficou presa num estado de passagem
    (VERIFICACAO_CADASTRAL / VERIFICACAO_OC) — seja porque a rodada anterior
    parou ali de propósito (uma etapa por vez, sem cascatear pra próxima na
    mesma rodada), seja por uma exceção no meio do caminho.

    Roteia pelo estágio ATUAL da NF em vez de sempre refazer tudo do zero:
      - Presa em VERIFICACAO_OC → o cadastro já foi confirmado numa rodada
        anterior (é o que `_rodar_verificacao_cadastral` grava quando a
        ligação está OK) — não precisa reconferir, vai direto pra
        `_rodar_verificacao_oc`.
      - Qualquer outro caso (ex: VERIFICACAO_CADASTRAL, se algum dia voltar
        a existir) → refaz a Verificação Cadastral do zero.
    Chamar `_rodar_verificacao_cadastral` pra uma NF presa em VERIFICACAO_OC
    entraria em loop: ela sempre para depois de confirmar o cadastro, nunca
    chamaria `_rodar_verificacao_oc` na mesma execução.
    """
    numnfc, chvnel = registro["numnfc"], registro["chvnel"]
    try:
        print(f"  ♻️  NF {numnfc} — reprocessando (estava presa em {registro.get('status_interno')})...")
        if registro.get("status_interno") == VERIFICACAO_OC:
            return _rodar_verificacao_oc(_dados_nf_do_registro(registro))
        return _rodar_verificacao_cadastral(_dados_nf_do_registro(registro))
    except Exception as e:
        traceback.print_exc()
        registrar_log_acao(numnfc, chvnel, "ERRO", "Exceção ao reprocessar NF travada", str(e))
        print(f"  ⚠️  NF {numnfc} — erro ao reprocessar: {e}")
        return {"numnfc": numnfc, "resultado": "ERRO", "detalhe": str(e)}


def reconferir_ligacao_pendente(registro: dict) -> dict:
    """
    Roda a cada 1min pra NF em AGUARDANDO_LIGACAO. Reconfere chamando
    _rodar_verificacao_cadastral() de novo do zero — refaz as DUAS checagens
    (portfólio E403FPR, depois derivação/depósito) em vez de uma
    reconferência estreita só do que ficou pendente da primeira vez: mais
    simples de manter (uma função só decide "cadastral está OK?", chamada
    tanto na entrada quanto na reconferência) e cobre o caso de a pendência
    ter MUDADO de tipo entre rodadas (ex: portfólio foi resolvido, mas só aí
    a checagem de derivação/depósito roda e acha um problema novo) — nesse
    caso o WhatsApp É reenviado, porque _registrar_estagio zera o flag de
    notificado quando o detalhe muda, mesmo ainda no mesmo estágio.
    """
    numnfc, chvnel = registro["numnfc"], registro["chvnel"]
    try:
        return _rodar_verificacao_cadastral(_dados_nf_do_registro(registro))
    except Exception as e:
        traceback.print_exc()
        registrar_log_acao(numnfc, chvnel, "ERRO", "Exceção reconferindo ligação pendente", str(e))
        print(f"  ⚠️  NF {numnfc} — erro reconferindo ligação: {e}")
        return {"numnfc": numnfc, "resultado": "ERRO", "detalhe": str(e)}


def _marcar_pronta_integracao(dados_nf: dict, detalhe: str) -> dict:
    """
    Marca a NF como pronta pra integração (mesmo mecanismo usado ao final da
    Verificação OC, quando todos os itens têm OC) e grava o estágio
    AGUARDANDO_INTEGRACAO. Usado tanto pelo fim normal da Verificação OC
    quanto pelo desvio de NF de devolução (IndDev='S'), que pula direto pra
    cá sem checar portfólio/OC.
    """
    numnfc, chvnel = dados_nf["numnfc"], dados_nf["chvnel"]
    codemp, codfil, codfor = dados_nf["codemp"], dados_nf["codfil"], dados_nf["codfor"]

    # Placeholder por enquanto (ver nota original em _rodar_verificacao_oc) —
    # falha aqui não impede o estágio AGUARDANDO_INTEGRACAO, só fica no log.
    try:
        if not verificar_pronto_integracao(codemp, codfil, codfor, numnfc):
            marcar_nf_pronta_importacao(codemp, codfil, codfor, numnfc)
    except Exception as e:
        registrar_log_acao(numnfc, chvnel, AGUARDANDO_INTEGRACAO, "Falha na integração (webservice) — segue aguardando", str(e))
        print(f"  ⚠️  NF {numnfc} — falha ao integrar (segue em AGUARDANDO_INTEGRACAO): {e}")

    registrar_log_acao(numnfc, chvnel, AGUARDANDO_INTEGRACAO, "NF liberada para integração", detalhe)
    _registrar_estagio(dados_nf, AGUARDANDO_INTEGRACAO, detalhe)
    print(f"  ✅ {detalhe}")
    return {"numnfc": numnfc, "resultado": AGUARDANDO_INTEGRACAO, "detalhe": detalhe}


# ─────────────────────────────────────────────────────────────────────────────
# Estágio 2 — Verificação OC (processa a NF inteira, em 2 fases)
# ─────────────────────────────────────────────────────────────────────────────
#
# FASE 1 (classificar) — pra cada item da NF, busca as linhas candidatas
# (contar_e_somar_ocs) e classifica, SEM cancelar nem gerar nada ainda:
#
#   OC_OK       — a linha MAIS ANTIGA (candidatas[0], em ordem cronológica)
#                 sozinha cobre QtdRec
#                 -> (fase 2) reserva (USU_CODANA) + vincula (E000IPC) direto.
#                    Nada de webservice. Nunca pula pra uma linha mais nova
#                    só porque ela cobre sozinha — sempre começa pela mais
#                    antiga.
#   Juntar_OC   — a mais antiga sozinha não cobre, mas a SOMA das linhas (em
#                 ordem cronológica) cobre o QtdRec de verdade — a
#                 quantidade já estava toda coberta, só espalhada
#                 -> (fase 2) cancela (Alteração, webservice) cada linha
#                    (mais antigas primeiro, limitado ao QtdRec) e acumula o
#                    produto em Ocp_Apr -> no fim gera 1 OC nova consolidada
#                    pelo QtdRec total (1 linha nova por produto).
#   OC_Pendente — nem somando cobre o QtdRec — sobra uma diferença que é
#                 demanda nova de verdade (candidatas pode ter 0 ou mais
#                 linhas, nunca fecha o total)
#                 -> (fase 2) cancela o que existir (mesmo mecanismo do
#                    Juntar_OC) e acumula em Ocp_Pen -> no fim gera 1 OC nova
#                    pelo QtdRec TOTAL (o que já tinha + a diferença nova),
#                    pendente de aprovação manual do Junior (notificado uma
#                    vez) — porque parte da quantidade é demanda nova.
#
# Nenhuma OC nova nasce aprovada de verdade no Sapiens (confirmado via
# SITAPR — não existe campo no webservice pra pular a aprovação); a
# diferença entre Juntar_OC e OC_Pendente é só se existia saldo suficiente
# antes (Juntar_OC) ou se parte é demanda genuinamente nova (OC_Pendente) —
# pra fins de mensagem ao Junior, não de mecânica (as duas cancelam o que
# existir e vinculam do mesmo jeito).
#
# Entre a Fase 1 e a Fase 2 ficam as DUAS TRAVAS (acumulado incompleto e NF
# cancelada) — protegendo TUDO que vem depois, inclusive os cancelamentos
# (antes, as travas só rodavam depois dos cancelamentos já terem acontecido).
#
# FASE 2 (executar) — só roda se as duas travas passarem. Cada cancelamento
# via webservice tem o retorno CAPTURADO e checado: se falhar, o ITEM inteiro
# é abortado (não entra no acumulador) em vez de seguir consolidando às
# cegas — o que pode deixar cancelamento parcial já aplicado no Sapiens fica
# registrado em nf_log_acoes pra a próxima rodada reconferir com o saldo
# real. Depois de gerar a OC nova com sucesso, os itens que entraram nela são
# vinculados (NUMOCP/SEQIPO) de volta na E000IPC — sem isso o scheduler
# reprocessava a mesma NF pra sempre (pior: uma vez que a OC nova fechava,
# ela sumia da busca de candidatas abertas e o sistema gerava OUTRA OC nova
# a cada rodada).

# Formatos dos acumuladores (conforme desenhado):
#   Ocp_Apr = "CodPro;QtdRec/CodPro;QtdRec/..."         (Juntar_OC)
#   Ocp_Pen = "CodPro;QtdRec;QtdOcp/CodPro;QtdRec;QtdOcp/..."  (OC_Pendente)

def _vincular_produtos_a_oc_nova(numnfc, chvnel, codemp, codfil, numocp, seqipcs_e_qtds) -> None:
    """
    Depois de uma OC nova ser gerada com sucesso (Juntar_OC ou OC_Pendente),
    vincula cada item da NF que entrou nela (`seqipcs_e_qtds`: lista de
    (seqipc, qtd_rec), na MESMA ordem em que os produtos foram enviados ao
    webservice) ao SEQIPO real que o Sapiens criou.

    A resposta do GravarOrdensCompra_8 não devolve SEQIPO por produto — só
    dá pra descobrir consultando a OC recém-criada de volta
    (oc_service.buscar_seqipos_da_oc), casando pela ORDEM de envio. Se a
    contagem não bater (ex: Sapiens mesclou linhas de mesmo produto), não
    vincula nada — fica pra conferência manual em vez de arriscar vínculo
    errado.

    Também reserva (USU_CODANA) o saldo da linha nova pra essa NF: a OC nasce
    com QTDABE cheio no Sapiens (só é abatido quando a integração fiscal REAL
    acontecer — hoje ainda placeholder), então sem reservar, outra NF
    processada antes disso enxergaria esse saldo como livre.
    """
    seqipos = buscar_seqipos_da_oc(codemp, codfil, numocp)
    if len(seqipos) != len(seqipcs_e_qtds):
        registrar_log_acao(
            numnfc, chvnel, "ERRO",
            f"OC {numocp} gerada mas contagem de linhas nao bate "
            f"({len(seqipos)} na OC vs {len(seqipcs_e_qtds)} item(ns) enviado(s)) — "
            f"vinculo NAO realizado, requer conferencia manual",
        )
        return
    for (seqipc, qtd_rec), seqipo in zip(seqipcs_e_qtds, seqipos):
        reservar_item_oc(codemp, codfil, numocp, seqipo, qtd_rec)
        vincular_item_oc(chvnel, seqipc, numocp, seqipo)
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC,
                           f"Item {seqipc} vinculado a OC nova {numocp} (seq {seqipo})", seqipc=seqipc)

def _dividir_candidatas(candidatas, necessario):
    """
    Decide gulosamente quanto cancelar de cada linha candidata (já vem
    ordenada, mais antiga primeiro) até cobrir `necessario`. Chamada na Fase
    1, no momento da classificação de cada item — o resultado alimenta o
    livro-caixa (reservas_locais) e é guardado no item; a Fase 2 só executa
    esta mesma lista, sem recalcular (recalcular na Fase 2 rodaria sobre um
    saldo que já não reflete os itens classificados depois deste).
    """
    restante = necessario
    a_cancelar = []
    for oc in candidatas:
        if restante <= 0:
            break
        usar = min(oc["disponivel"], restante)
        if usar <= 0:
            continue
        a_cancelar.append((oc, usar))
        restante -= usar
    return a_cancelar


def _rodar_verificacao_oc(dados_nf: dict) -> dict:
    numnfc, chvnel = dados_nf["numnfc"], dados_nf["chvnel"]
    codemp, codfil, codfor = dados_nf["codemp"], dados_nf["codfil"], dados_nf["codfor"]
    nomfor = dados_nf["nomfor"]

    seqipcs = listar_seqipc_nf(chvnel)
    if not seqipcs:
        detalhe = "NF sem itens encontrados na E000IPC."
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, detalhe)
        _registrar_estagio(dados_nf, VERIFICACAO_OC, detalhe)
        print(f"  ❌ {detalhe}")
        return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}

    # ── FASE 1: classificar todos os itens — nenhum webservice/gravação ────
    itens_ja_vinculados = 0
    itens_ok = []        # [{seqipc, qtd_rec, oc}]
    itens_juntar = []    # [{seqipc, qtd_rec, cod_prod, codpro_ws, profor, despro, preuni, candidatas, qtd_ocp, num_linhas, num_ocp, a_cancelar}]
    itens_pendente = []  # idem, sem "oc"
    itens_aguardando_aprovacao = []  # [{seqipc, numocp}] — vínculo válido, mas OC ainda 'ANA'

    # Livro-caixa local: (numocp, seqipo) -> quantidade já "usada" por itens
    # já classificados NESTA MESMA RODADA, antes de qualquer gravação real no
    # Oracle (reservar_item_oc/cancelamento só acontecem na Fase 2). Sem
    # isso, dois itens da mesma NF que disputam a mesma linha de OC
    # enxergam, cada um, o saldo "ao vivo" do banco — nenhum sabe do outro.
    # Bug real observado: 3 itens da mesma NF usando a linha 24416/7 sem
    # nenhum enxergar o que os outros dois já tinham reservado/cancelado.
    # contar_e_somar_ocs desconta este dicionário do `disponivel` de cada
    # candidata, então o item seguinte já vê o saldo remanescente de
    # verdade, não a foto original do banco.
    reservas_locais = {}

    # OC que o PRÓPRIO sistema já gerou pra esta NF (observação "p/ NF <n>:"),
    # recuperada quando o vínculo na E000IPC se perdeu/foi sobrescrito (ex:
    # NUMOCP trocado por um número de pedido do fornecedor). Sem isso o item
    # era tratado como novo e uma OC duplicada era gerada a cada vez (a
    # reserva da própria NF zera o saldo das candidatas). Carregada só quando
    # algum item precisa; `usadas` evita dois itens ocuparem a mesma linha.
    ocs_da_nf = None
    linhas_usadas = set()
    itens_recuperados = []   # [{seqipc, numocp, seqipo, qtd_rec, reservado}] — vínculo gravado na Fase 2
    itens_ambiguos = []      # [(seqipc, [numocps])] — NF para sem gerar nada
    preferidas = [v for k, v in (dados_nf.get("referencia") or {}).items() if k != "erros_webservice"]

    for seqipc in seqipcs:
        item = buscar_item_nf(chvnel, seqipc, codfor)
        if not item:
            continue

        codpro  = item["codpro"]
        # Identificador do produto pros acumuladores (Ocp_Apr/Ocp_Pen): usa
        # CODPRO, mas cai pra PROFOR quando CODPRO vem vazio — comum, muita
        # NF só traz PROFOR preenchido. Sem isso o acumulador fica sem
        # identificação do produto (ex: " ;1.0;0/").
        cod_prod = (str(codpro or "").strip() or str(item.get("profor") or "").strip())
        # codPro real pro WEBSERVICE de OC e pra BUSCA de OC: diferente do
        # identificador acima (que é só pra log/acumulador), o webservice
        # exige o CODPRO interno do Sapiens — mandar o PROFOR direto dá
        # "Produto não encontrado", mesmo com a NF sem CODPRO. Quando falta,
        # busca o de-para em E403FPR (portfólio do fornecedor); só cai pro
        # PROFOR cru se nem lá achar (nesse caso o webservice provavelmente
        # vai rejeitar mesmo). Resolvido ANTES da checagem de vínculo abaixo
        # porque agora também é usado pra validar o vínculo existente.
        codpro_ws = (str(codpro or "").strip()
                    or codpro_do_portfolio(codemp, codfor, item.get("profor"))
                    or str(item.get("profor") or "").strip())

        if item.get("numocp"):
            vinculo_ok = numocp_vinculo_valido(codemp, codfil, item["numocp"], item.get("seqipo"), codfor, codpro_ws)
            # SITOCP da OC vinculada: 4 (liquidado) / 5 (cancelada) = OC morta.
            # numocp_vinculo_valido só confere existência/fornecedor/produto,
            # nunca SITOCP — sem esta checagem, uma OC aprovada e depois
            # cancelada continuava contando como cobertura e a NF ficava
            # "liberada" em cima de nada.
            sitocp_vinc = oc_situacao(codemp, codfil, item["numocp"])[0] if vinculo_ok else None
            if vinculo_ok and sitocp_vinc not in ("4", "5"):
                linhas_usadas.add((item["numocp"], item.get("seqipo")))
                # Vínculo existe, é do fornecedor certo E do produto certo, e a
                # OC ainda está viva — mas só conta como "pronto" se a OC já
                # foi APROVADA (SITAPR='APR'). Toda OC nova nasce 'ANA' (em
                # análise); um item vinculado a uma OC ainda 'ANA' não pode ser
                # tratado como coberto, senão a NF pula direto pra
                # AGUARDANDO_INTEGRACAO sem o Junior ter aprovado nada — bug
                # real: acontecia quando a NF era reprocessada por QUALQUER
                # caminho fora do controlado (reconferir_oc_pendente), ex.
                # se o registro dela sumir do banco local e
                # processar_nf_nova tratar como nova de novo.
                #
                # Importante: NÃO apaga o vínculo aqui (isso é só pra vínculo
                # genuinamente inválido / OC morta, abaixo). O vínculo está
                # correto, só falta aprovação — apagar geraria/cancelaria outra
                # OC à toa, duplicando a que já está esperando o Junior.
                if verificar_oc_aprovada(codemp, codfil, item["numocp"]):
                    itens_ja_vinculados += 1
                else:
                    itens_aguardando_aprovacao.append({"seqipc": seqipc, "numocp": item["numocp"]})
                    registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, f"Item {seqipc} aguardando aprovação da OC",
                                       f"NUMOCP {item['numocp']} vinculado e correto, mas SITAPR ainda não é 'APR' — NF segue esperando", seqipc=seqipc)
                continue
            # Cai aqui quando: NUMOCP é lixo (OC não existe pra esse fornecedor
            # — número da OC do fornecedor, ou uma data como 20260722), aponta
            # pra um produto ERRADO (dado velho/travado de outro teste), OU a
            # OC vinculada foi cancelada/liquidada (SITOCP 4/5) depois do
            # vínculo. Apaga o vínculo via UPDATE na E000IPC e segue abaixo pra
            # conferir o item do zero (vai achar OC real ou gerar OC nova).
            limpar_vinculo_item_oc(chvnel, seqipc)
            if vinculo_ok:
                motivo = f"OC {item['numocp']} está {descricao_sitocp(sitocp_vinc)} — não cobre mais o item"
            else:
                motivo = (f"NUMOCP {item['numocp']} não existe pra fornecedor {codfor} "
                          f"ou é de outro produto (esperado codpro={codpro_ws})")
            registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, f"Item {seqipc} vínculo apagado",
                               f"{motivo} — vínculo limpo, reconferindo", seqipc=seqipc)

        qtd_rec = item["qtd_recebida"]

        # Antes de buscar saldo/gerar OC: esta NF já tem OC gerada pelo sistema?
        if ocs_da_nf is None:
            ocs_da_nf = listar_ocs_da_nf(codemp, codfil, codfor, numnfc)
        veredito, oc_nf, linha_nf = casar_item_com_ocs_da_nf(
            ocs_da_nf, codpro_ws, qtd_rec, linhas_usadas, preferidas)
        if veredito == "ambigua":
            itens_ambiguos.append((seqipc, oc_nf))
            continue
        if veredito == "achou":
            itens_recuperados.append({"seqipc": seqipc, "numocp": oc_nf["numocp"], "seqipo": linha_nf["seqipo"],
                                      "qtd_rec": qtd_rec, "reservado": linha_nf["reservado"]})
            if oc_nf["sitapr"] == "APR":
                itens_ja_vinculados += 1
            else:
                itens_aguardando_aprovacao.append({"seqipc": seqipc, "numocp": oc_nf["numocp"]})
            continue

        # Busca de OC usa codpro_ws (já resolvido via portfólio quando a NF
        # vem sem CODPRO), não o `codpro` cru da E000IPC — sem isso, item
        # sem CODPRO na NF (comum) nunca achava OC nenhuma depois que a
        # busca parou de cair pro PROFOR cru (ver ponto 1): o produto está
        # cadastrado de verdade (E403FPR/E075PRO), só a linha da NF que não
        # carrega o código interno.
        agg = contar_e_somar_ocs(codemp, codfil, codfor, codpro_ws, reservas_locais=reservas_locais)
        candidatas = agg["candidatas"]

        # OC_OK: a linha MAIS ANTIGA (candidatas[0] — já vem ordenada por
        # data) cobre sozinha o QtdRec. Decisão de negócio confirmada: a
        # busca é sempre em ordem cronológica, começando pela mais antiga —
        # não vale pular direto pra uma linha mais nova só porque ela cobre
        # sozinha (isso "furava a fila", deixando a mais antiga intocada
        # enquanto uma mais nova era consumida à toa).
        linha_mais_antiga = candidatas[0] if candidatas else None

        base = {
            "seqipc": seqipc, "qtd_rec": qtd_rec, "cod_prod": cod_prod,
            "codpro_ws": codpro_ws, "profor": item.get("profor"),
            "despro": item.get("cplipc"), "preuni": item.get("preuni"),
            "candidatas": candidatas,
        }

        if linha_mais_antiga and linha_mais_antiga["disponivel"] >= qtd_rec:
            base["oc"] = linha_mais_antiga
            itens_ok.append(base)
            chave = (linha_mais_antiga["numocp"], linha_mais_antiga["seqipo"])
            reservas_locais[chave] = reservas_locais.get(chave, 0) + qtd_rec
        elif agg["qtdocp"] >= qtd_rec:
            # Juntar_OC: a mais antiga sozinha não cobre, mas a SOMA das
            # linhas em ordem cronológica cobre o total de verdade — a
            # quantidade já estava toda coberta, só espalhada em mais de uma
            # linha. Cancela (limitado ao QtdRec) e consolida numa OC nova.
            base["qtd_ocp"] = agg["qtdocp"]
            base["num_linhas"] = agg["num_linhas"]
            base["num_ocp"] = agg["numocp"]
            # Decide AQUI (Fase 1) quanto cancelar de cada linha — não na
            # Fase 2 — pra poder alimentar o livro-caixa antes do próximo
            # item do laço ser classificado. A Fase 2 só executa esta lista.
            base["a_cancelar"] = _dividir_candidatas(candidatas, qtd_rec)
            for oc, usar in base["a_cancelar"]:
                chave = (oc["numocp"], oc["seqipo"])
                reservas_locais[chave] = reservas_locais.get(chave, 0) + usar
            itens_juntar.append(base)
        else:
            # OC_Pendente: nem somando cobre (0 ou mais candidatas, mas
            # sempre falta pelo menos parte de demanda nova). Decisão de
            # negócio confirmada: cancela o que existir (limitado ao QtdRec,
            # mesmo mecanismo do Juntar_OC — ex: cancela os 20 que tinha) e
            # gera 1 OC nova pelo QtdRec TOTAL (não só a diferença) —
            # pendente de aprovação do Junior, porque parte da quantidade
            # (a diferença que faltava) é demanda nova de verdade.
            base["qtd_ocp"] = agg["qtdocp"]
            base["num_linhas"] = agg["num_linhas"]
            base["num_ocp"] = agg["numocp"]
            base["falta"] = qtd_rec - agg["qtdocp"]
            base["a_cancelar"] = _dividir_candidatas(candidatas, qtd_rec)
            for oc, usar in base["a_cancelar"]:
                chave = (oc["numocp"], oc["seqipo"])
                reservas_locais[chave] = reservas_locais.get(chave, 0) + usar
            itens_pendente.append(base)

    # ── TRAVA 0: OC da NF ambígua ───────────────────────────────────────────
    # Há mais de uma OC viva gerada pra esta NF cobrindo o mesmo item (ex:
    # duplicatas de rodadas anteriores). Não dá pra saber qual é a certa, e
    # gerar mais uma só pioraria — para sem cancelar/gerar/vincular nada até
    # alguém deixar uma OC só (cancelando as sobras no Sapiens).
    if itens_ambiguos:
        numocps_amb = sorted({n for _, lista in itens_ambiguos for n in lista})
        detalhe = (f"{len(itens_ambiguos)} item(ns) com mais de uma OC gerada pra esta NF "
                   f"({', '.join(map(str, numocps_amb))}) — nenhuma OC nova gerada, "
                   f"requer decisão manual (deixar só uma OC no Sapiens).")
        anterior = _buscar_estagio_atual(numnfc)
        if not anterior or anterior.get("status_interno") != VERIFICACAO_OC or anterior.get("detalhe") != detalhe:
            registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "OC da NF ambígua — geração de OC abortada", detalhe)
        _registrar_estagio(dados_nf, VERIFICACAO_OC, detalhe)
        print(f"  ⛔ {detalhe}")
        return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}

    # ── TRAVA 1: confere a classificação contra a NF ────────────────────────
    # Antes de cancelar/gerar qualquer OC, valida que TODOS os itens da NF
    # foram contemplados (OC_OK / já vinculado / Juntar_OC / OC_Pendente). Se
    # algum item ficou de fora (item não encontrado na E000IPC), o acumulado
    # está INCOMPLETO e NÃO se pode agir (geraria uma OC parcial, faltando
    # produto). Fica em VERIFICACAO_OC pra reprocessar do zero na próxima
    # rodada, SEM ter cancelado nada ainda.
    total_classificado = (len(itens_ok) + itens_ja_vinculados + len(itens_juntar)
                          + len(itens_pendente) + len(itens_aguardando_aprovacao))
    if total_classificado != len(seqipcs):
        detalhe = (
            f"Acumulado incompleto: {total_classificado} de {len(seqipcs)} item(ns) "
            f"contemplados — NÃO gerando OC (reprocessa na próxima rodada)."
        )
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "Acumulado incompleto — geração de OC abortada", detalhe)
        _registrar_estagio(dados_nf, VERIFICACAO_OC, detalhe)
        print(f"  ⛔ {detalhe}")
        return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}

    # ── TRAVA 2: NF cancelada (E000NFC.INDCAN='S') ──────────────────────────
    # Antes de QUALQUER cancelamento/geração — não só antes da geração, como
    # era antes (a trava protegia só a geração; os cancelamentos já tinham
    # acontecido no laço). A NF pode ter sido cancelada no Sapiens enquanto
    # esperava resolução (dias em VERIFICACAO_OC/AGUARDANDO_APROVACAO_OC).
    if nf_esta_cancelada(chvnel):
        detalhe = "NF cancelada no Sapiens (INDCAN='S') — geração de OC abortada."
        registrar_log_acao(numnfc, chvnel, CANCELADA, "NF cancelada — geração de OC abortada, saindo da tela", detalhe)
        _registrar_estagio(dados_nf, CANCELADA, detalhe)
        print(f"  🚫 {detalhe}")
        return {"numnfc": numnfc, "resultado": CANCELADA, "detalhe": detalhe}

    # ── FASE 2: executar — só chega aqui se as duas travas passaram ────────

    for it in itens_ok:
        oc = it["oc"]
        reservar_item_oc(codemp, codfil, oc["numocp"], oc["seqipo"], it["qtd_rec"])
        vincular_item_oc(chvnel, it["seqipc"], oc["numocp"], oc["seqipo"])
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, f"Item {it['seqipc']} OK",
                           f"OC {oc['numocp']} (seq {oc['seqipo']}) qtd={it['qtd_rec']}", seqipc=it["seqipc"])

    # Religa os itens à OC que o sistema já tinha gerado pra esta NF (vínculo
    # perdido). A reserva (USU_CODANA) normalmente já está feita — só completa
    # se faltar, pra outra NF não enxergar a linha como livre.
    for it in itens_recuperados:
        falta_reservar = it["qtd_rec"] - it["reservado"]
        if falta_reservar > 0:
            reservar_item_oc(codemp, codfil, it["numocp"], it["seqipo"], falta_reservar)
        vincular_item_oc(chvnel, it["seqipc"], it["numocp"], it["seqipo"])
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, f"Item {it['seqipc']} religado à OC da NF {it['numocp']} (seq {it['seqipo']})",
                           "Vínculo perdido na E000IPC — OC já gerada pra esta NF reaproveitada, nenhuma OC nova", seqipc=it["seqipc"])

    ocp_apr = ""              # acumulador Juntar_OC  — "CodPro;QtdRec/"
    ocp_pen = ""              # acumulador OC_Pendente — "CodPro;QtdRec;QtdOcp/"
    ocp_can = ""              # acumulador cancelamento — "NumOcp;SeqIpo;Qtd/"
    produtos_apr = []         # detalhe pros webservices/log
    produtos_pen = []
    seqipcs_apr = []          # (seqipc, qtd_rec) que sobreviveram — pra vincular à OC nova
    seqipcs_pen = []          # idem, pro caminho OC_Pendente

    # Progresso da Fase 2 (cancelamentos via webservice): cada chamada pode
    # demorar/travar, então cada uma é registrada ANTES de ser enviada — se o
    # processo ficar preso numa chamada, o log mostra qual (item/OC/linha) e
    # quantas faltavam.
    prog = {"n": 0, "total": sum(len(it["a_cancelar"]) for it in itens_juntar + itens_pendente)}
    falha_cancelamento = None   # {"seqipc", "numocp", "seqipo", "erro"} da 1ª falha — para a NF inteira
    if prog["total"]:
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "Fase 2 iniciada — cancelamentos via webservice",
                           f"{prog['total']} cancelamento(s) a enviar em {len(itens_juntar) + len(itens_pendente)} item(ns)")
        print(f"  ▶️  Fase 2: {prog['total']} cancelamento(s) a enviar")

    def _cancelar_candidatas(seqipc, candidatas_a_cancelar):
        """
        Executa os cancelamentos (webservice, Alteração) de uma lista de
        (oc, usar) pra um item, checando sucesso um a um e gravando cada
        tentativa (envelope + raw, sucesso ou falha) em nf_log_acoes.
        Retorna (ok, cancelados) — ok=False se QUALQUER tentativa falhar; a
        função PARA na primeira falha (não tenta as seguintes) e quem chama
        interrompe a NF inteira SEM gerar OC (ver falha_cancelamento). Os
        cancelamentos que já deram certo antes dela já foram aplicados de
        verdade no Sapiens — ficam registrados em log pra a reconferência da
        próxima rodada recalcular o saldo real, em vez de tentar desfazer.
        """
        nonlocal falha_cancelamento
        cancelados = []
        for oc, usar in candidatas_a_cancelar:
            prog["n"] += 1
            registrar_log_acao(
                numnfc, chvnel, VERIFICACAO_OC,
                f"Cancelamento {prog['n']}/{prog['total']} — enviando (item {seqipc}, OC {oc['numocp']}/{oc['seqipo']}, qtd={usar})",
                seqipc=seqipc,
            )
            print(f"  ⏳ Cancelamento {prog['n']}/{prog['total']} — item {seqipc}, OC {oc['numocp']}/{oc['seqipo']}")
            # qtdCan no Senior é ABSOLUTO (a quantidade cancelada TOTAL da
            # linha), não um delta — confirmado num teste real: mandar só
            # `usar` sobrescrevia um QTDCAN anterior em vez de somar,
            # deixando saldo errado. Soma o que a linha já tinha cancelado
            # (oc["qtdcan"]) com o que estamos cancelando agora.
            qtdcan_total = oc.get("qtdcan", 0) + usar
            resultado = cancelar_item_oc_via_webservice(
                codemp, codfil, oc["numocp"], oc["seqipo"], qtdcan_total, oc.get("qtdori"),
            )
            registrar_log_acao(
                numnfc, chvnel, VERIFICACAO_OC,
                f"Cancelamento item {seqipc} — OC {oc['numocp']}/{oc['seqipo']} "
                f"qtd_agora={usar} (qtdCan enviado={qtdcan_total}, já tinha {oc.get('qtdcan', 0)})",
                f"sucesso={resultado.get('sucesso')} erro={resultado.get('erro')} "
                f"envelope={resultado.get('envelope')} raw={resultado.get('raw')}",
                seqipc=seqipc,
            )
            if not resultado.get("cancelado"):
                falha_cancelamento = {"seqipc": seqipc, "numocp": oc["numocp"], "seqipo": oc["seqipo"],
                                      "erro": resultado.get("erro") or "falha desconhecida no webservice",
                                      # "raw" só existe quando o Sapiens RESPONDEU (recusa de negócio, ex:
                                      # "OC fechada / Aprovação Multinível"); erro de rede/timeout não tem.
                                      "recusado": bool(resultado.get("raw"))}
                registrar_log_acao(
                    numnfc, chvnel, "ERRO",
                    f"Falha ao cancelar item {seqipc} na OC {oc['numocp']}/{oc['seqipo']} — NF interrompida, nenhuma OC será gerada",
                    f"{prog['n'] - 1} cancelamento(s) anterior(es) desta NF já aplicado(s) de verdade no Sapiens. "
                    f"erro={falha_cancelamento['erro']}",
                    seqipc=seqipc,
                )
                return False, cancelados
            cancelados.append((oc, usar))
        return True, cancelados

    # ── Juntar_OC: a soma das candidatas (ordem cronológica) já cobre o
    # QtdRec de verdade — cancela cada linha (mais antigas primeiro),
    # limitado ao QtdRec, e gera 1 OC nova consolidada pelo QtdRec total.
    for it in itens_juntar:
        # a_cancelar já foi decidido na Fase 1 (sobre o saldo com o
        # livro-caixa aplicado) — só executa aqui, não recalcula.
        ok, cancelados = _cancelar_candidatas(it["seqipc"], it["a_cancelar"])
        if not ok:
            break  # 1ª falha — para a NF inteira (ver falha_cancelamento abaixo)
        total_cancelado = sum(usar for _, usar in cancelados)
        for oc, usar in cancelados:
            ocp_can += f"{oc['numocp']};{oc['seqipo']};{usar}/"
        ocp_apr +=f"{it['codpro_ws']};{it['qtd_rec']}/"
        produtos_apr.append({"seqipc": it["seqipc"], "codpro": it["codpro_ws"],
                             "profor": it["profor"], "despro": it["despro"],
                             "preuni": it["preuni"], "qtd": it["qtd_rec"]})
        seqipcs_apr.append((it["seqipc"], it["qtd_rec"]))
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, f"Item {it['seqipc']} consolidar OC",
                           f"{it['num_linhas']} linha(s) em {it['num_ocp']} OC(s) somam {it['qtd_ocp']} >= "
                           f"{it['qtd_rec']} necessário(s) — canceladas {total_cancelado} — OC nova de "
                           f"{it['qtd_rec']} gerada, pendente de aprovação do Junior", seqipc=it["seqipc"])

    # ── OC_Pendente: nem somando cobre o QtdRec (falta pelo menos uma parte
    # de demanda nova de verdade). Decisão de negócio confirmada: cancela o
    # que existir (mesmo mecanismo do Juntar_OC — ex: cancela os 20 que
    # tinha) e gera 1 OC nova pelo QtdRec TOTAL (os 20 que já tinha + a
    # diferença nova), pendente de aprovação do Junior — vincula assim que a
    # OC é gerada com sucesso, igual ao Juntar_OC (a aprovação em si,
    # SITAPR='APR', é conferida à parte, no nível da NF, não aqui).
    for it in itens_pendente:
        if falha_cancelamento:
            break  # já falhou no Juntar_OC — não envia mais nada
        # a_cancelar já foi decidido na Fase 1 (sobre o saldo com o
        # livro-caixa aplicado) — só executa aqui, não recalcula.
        ok, cancelados = _cancelar_candidatas(it["seqipc"], it["a_cancelar"])
        if not ok:
            break  # 1ª falha — para a NF inteira (ver falha_cancelamento abaixo)
        total_cancelado = sum(usar for _, usar in cancelados)
        for oc, usar in cancelados:
            ocp_can += f"{oc['numocp']};{oc['seqipo']};{usar}/"
        ocp_pen += f"{it['codpro_ws']};{it['qtd_rec']};{it['qtd_ocp']}/"
        produtos_pen.append({"seqipc": it["seqipc"], "codpro": it["codpro_ws"],
                             "profor": it["profor"], "despro": it["despro"],
                             "preuni": it["preuni"],
                             "qtd": it["qtd_rec"], "qtd_oc": it["qtd_ocp"], "falta": it["falta"]})
        seqipcs_pen.append((it["seqipc"], it["qtd_rec"]))
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, f"Item {it['seqipc']} sem cobertura",
                           f"{it['num_linhas']} linha(s) em {it['num_ocp']} OC(s) somam {it['qtd_ocp']} < {it['qtd_rec']} "
                           f"— canceladas {total_cancelado} — OC nova de {it['qtd_rec']} gerada, pendente de aprovação do Junior",
                           seqipc=it["seqipc"])

    # ── Cancelamento falhou: PARA aqui, sem gerar nenhuma OC nova ───────────
    # Gerar a OC com o saldo antigo ainda ativo duplicaria a quantidade (o
    # cancelamento é o que libera a demanda antiga). A NF fica em
    # VERIFICACAO_OC e o scheduler tenta de novo na próxima rodada — a
    # reconferência recalcula o saldo real, então os cancelamentos que já
    # deram certo não são repetidos.
    if falha_cancelamento and falha_cancelamento["recusado"]:
        # O Sapiens RESPONDEU recusando (OC fechada / Aprovação Multinível...):
        # repetir a cada minuto não adianta — só o comprador resolve. A NF vai
        # pra AGUARDANDO_APROVACAO_OC (coluna do Junior), guarda uma "foto" das
        # linhas de OC que ia cancelar e avisa o Junior UMA vez. A cada rodada
        # só compara a foto (ver reconferir_oc_pendente) e retoma sozinha se
        # alguma OC mudar.
        f = falha_cancelamento
        linhas_alvo = sorted({(oc["numocp"], oc["seqipo"]) for it in itens_juntar + itens_pendente for oc, _ in it["a_cancelar"]})
        bloqueio = {
            "seqipc": f["seqipc"], "numocp": f["numocp"], "seqipo": f["seqipo"], "erro": f["erro"],
            "linhas": [list(x) for x in linhas_alvo],
            "assinatura": assinatura_linhas_oc(codemp, codfil, linhas_alvo),
        }
        detalhe = (f"Cancelamento recusado pelo Sapiens — item {f['seqipc']}, OC {f['numocp']}/{f['seqipo']}: {f['erro']}. "
                   f"Nenhuma OC gerada; aguardando o comprador ajustar a OC no Sapiens (a NF retoma sozinha quando ela mudar).")
        registrar_log_acao(numnfc, chvnel, AGUARDANDO_APROVACAO_OC, "Cancelamento recusado — NF parada até a OC mudar", detalhe)
        _registrar_estagio(dados_nf, AGUARDANDO_APROVACAO_OC, detalhe, referencia={CANCELAMENTO_BLOQUEADO: bloqueio})
        if not _ja_notificado(numnfc):
            envio = notificar_junior_cancelamento_bloqueado(numnfc, codfor, nomfor, codemp, codfil, bloqueio)
            if envio.get("sucesso"):
                _marcar_notificado(numnfc)
                registrar_log_acao(numnfc, chvnel, "NOTIFICACAO", "WhatsApp enviado ao Junior — cancelamento de OC recusado", detalhe)
            else:
                registrar_log_acao(numnfc, chvnel, "ERRO", "Falha ao enviar WhatsApp ao Junior — cancelamento recusado", str(envio.get("erro")))
        print(f"  ⛔ {detalhe}")
        return {"numnfc": numnfc, "resultado": AGUARDANDO_APROVACAO_OC, "detalhe": detalhe}

    if falha_cancelamento:
        # Falha SEM resposta do Sapiens (rede/timeout) — transitória: fica em
        # VERIFICACAO_OC e tenta de novo na próxima rodada.
        f = falha_cancelamento
        detalhe = (f"Falha ao cancelar item {f['seqipc']} na OC {f['numocp']}/{f['seqipo']} "
                   f"({prog['n']}/{prog['total']} cancelamentos tentados) — nenhuma OC gerada, "
                   f"tenta de novo na próxima rodada. Erro: {f['erro']}")
        anterior = _buscar_estagio_atual(numnfc)
        if not anterior or anterior.get("status_interno") != VERIFICACAO_OC or anterior.get("detalhe") != detalhe:
            registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "Cancelamento falhou — geração de OC abortada", detalhe)
        _registrar_estagio(dados_nf, VERIFICACAO_OC, detalhe)
        print(f"  ⛔ {detalhe}")
        return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}

    # ── TRAVA 1 (reconferida): algum cancelamento pode ter falhado na Fase 2
    # e abortado o item sem entrar no acumulador — se isso aconteceu, o total
    # não bate mais com len(seqipcs). Mesma regra: nunca gera OC parcial
    # faltando produto, fica pra reprocessar na próxima rodada.
    total_executado = (len(itens_ok) + itens_ja_vinculados + len(produtos_apr)
                       + len(produtos_pen) + len(itens_aguardando_aprovacao))
    if total_executado != len(seqipcs):
        detalhe = (
            f"Acumulado incompleto após cancelamentos: {total_executado} de {len(seqipcs)} "
            f"item(ns) contemplados (algum cancelamento falhou) — NÃO gerando OC (reprocessa na próxima rodada)."
        )
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "Acumulado incompleto pós-cancelamento — geração de OC abortada", detalhe)
        _registrar_estagio(dados_nf, VERIFICACAO_OC, detalhe)
        print(f"  ⛔ {detalhe}")
        return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}

    # ── Age sobre os acumuladores: gera as OCs e vincula os itens à OC nova ─
    # referencia_oc guarda o NUMOCP que o webservice de fato gerou (chave
    # "apr" pro consolidado do Juntar_OC, "pen" pro pendente do OC_Pendente —
    # uma NF pode disparar os dois ao mesmo tempo, se tiver itens nos dois
    # casos). Sem capturar isso, reconferir_oc_pendente nunca saberia que a
    # OC já foi gerada e chamaria o webservice de novo a cada 1min, criando
    # uma OC duplicada por rodada.
    referencia_oc = {}
    if ocp_apr:
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "Enviando OC consolidada ao webservice", f"{len(produtos_apr)} produto(s)")
        oc_apr = gerar_oc_aprovada_via_webservice(
            codemp, codfil, codfor, produtos_apr,
            observacao=f"OC consolidada (aprovada) p/ NF {numnfc}: {ocp_apr}"[:1000],
        )
        if oc_apr.get("sucesso"):
            referencia_oc["apr"] = oc_apr.get("numero_oc")
            registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "OC aprovada consolidada gerada",
                               f"Ocp_Apr={ocp_apr} numocp={oc_apr.get('numero_oc')}")
            _vincular_produtos_a_oc_nova(numnfc, chvnel, codemp, codfil, oc_apr.get("numero_oc"), seqipcs_apr)
        else:
            # raw = resposta crua do webservice — essencial pra diagnosticar
            # quando sucesso vem None/False sem "erro" (formato de resposta
            # inesperado, não confirmado ainda pra GravarOrdensCompra_8).
            registrar_log_acao(numnfc, chvnel, "ERRO", "Falha ao gerar OC aprovada consolidada",
                               f"Ocp_Apr={ocp_apr} sucesso={oc_apr.get('sucesso')} erro={oc_apr.get('erro')} "
                               f"raw={oc_apr.get('raw')}")
            # Registrado em referencia_oc["erros_webservice"] pra
            # reconferir_oc_pendente PARAR de tentar gerar essa OC de novo a
            # cada 1min (o erro já foi acusado no log acima) — sem isso, um
            # erro permanente do webservice (ex: Sapiens fora do ar,
            # credencial inválida) faria a mesma chamada repetir indefinidamente.
            cods_apr = ", ".join(str(p["codpro"]) for p in produtos_apr)
            referencia_oc.setdefault("erros_webservice", []).append(
                f"OC aprovada consolidada (produtos: {cods_apr}): {oc_apr.get('erro') or 'falha desconhecida no webservice'}"
            )

    if ocp_pen:
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "Enviando OC pendente ao webservice", f"{len(produtos_pen)} produto(s)")
        oc_pen = gerar_oc_para_aprovacao(
            codemp, codfil, codfor, produtos_pen,
            observacao=f"OC pendente p/ NF {numnfc}: {ocp_pen}"[:1000],
        )
        if oc_pen.get("sucesso"):
            referencia_oc["pen"] = oc_pen.get("numero_oc")
            registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "OC pendente gerada",
                               f"Ocp_Pen={ocp_pen} numocp={oc_pen.get('numero_oc')}")
            # Vincula direto (igual ao Juntar_OC) — a OC cobre o QtdRec
            # inteiro do item (não existia cobertura nenhuma antes). A NF só
            # avança pra AGUARDANDO_INTEGRACAO depois que reconferir_oc_pendente
            # confirmar SITAPR='APR' — o vínculo aqui não pula essa espera.
            _vincular_produtos_a_oc_nova(numnfc, chvnel, codemp, codfil, oc_pen.get("numero_oc"), seqipcs_pen)
        else:
            registrar_log_acao(numnfc, chvnel, "ERRO", "Falha ao gerar OC pendente",
                               f"Ocp_Pen={ocp_pen} sucesso={oc_pen.get('sucesso')} erro={oc_pen.get('erro')} "
                               f"raw={oc_pen.get('raw')}")
            # Idem ao ramo "apr" acima — marca o erro pra reconferir_oc_pendente
            # parar de tentar gerar essa OC de novo automaticamente.
            cods_pen = ", ".join(str(p["codpro"]) for p in produtos_pen)
            referencia_oc.setdefault("erros_webservice", []).append(
                f"OC pendente (produtos: {cods_pen}): {oc_pen.get('erro') or 'falha desconhecida no webservice'}"
            )

    # itens_aguardando_aprovacao: vínculo já existia e é correto, só falta a
    # OC ser aprovada (SITAPR='APR') — soma os NUMOCPs desses itens à
    # referencia_oc (chaves "existente_N") pra reconferir_oc_pendente saber
    # quais OCs conferir na próxima rodada, sem chamar o webservice de novo
    # (numocps já não está vazio) e sem a NF pular pra AGUARDANDO_INTEGRACAO.
    numocps_aguardando = sorted({it["numocp"] for it in itens_aguardando_aprovacao})
    for idx, n in enumerate(numocps_aguardando):
        referencia_oc[f"existente_{idx}"] = n

    # ──────────────────────────────────────────────
    # Só integra se NENHUM item ficou pendente/consolidando/aguardando
    # aprovação. Qualquer item em Juntar_OC, OC_Pendente OU vinculado a uma
    # OC ainda não aprovada deixa a NF em AGUARDANDO_APROVACAO_OC até a(s)
    # OC(s) fecharem (ver reconferir_oc_pendente).
    if not ocp_apr and not ocp_pen and not numocps_aguardando:
        detalhe = (
            f"Todos os {len(seqipcs)} item(ns) da NF com OC "
            f"({len(itens_ok)} OK, {itens_ja_vinculados} já vinculado(s), "
            f"{len(itens_recuperados)} religado(s) à OC existente da NF) — "
            f"NF aguardando integração."
        )
        return _marcar_pronta_integracao(dados_nf, detalhe)

    detalhe = (
        f"{len(produtos_apr)} item(ns) p/ consolidar (Juntar_OC), "
        f"{len(produtos_pen)} sem cobertura (OC_Pendente)"
        + (f", {len(itens_aguardando_aprovacao)} aguardando aprovação de OC já vinculada"
           if itens_aguardando_aprovacao else "")
        + "."
    )
    # Persiste os acumuladores no nf_processamento (ocp_apr/ocp_pen) — ficam
    # recuperáveis/auditáveis mesmo que a geração via webservice tenha
    # falhado. referencia_oc (o NUMOCP real de cada OC gerada com sucesso,
    # mais os NUMOCPs de itens já vinculados esperando aprovação) é o que
    # reconferir_oc_pendente usa pra saber se já pode parar de gerar e passar
    # a só conferir se a(s) OC(s) fecharam.
    _registrar_estagio(dados_nf, AGUARDANDO_APROVACAO_OC, detalhe, referencia=(referencia_oc or None),
                       ocp_apr=ocp_apr, ocp_pen=ocp_pen, ocp_can=ocp_can)
    # Notifica o Junior só na primeira vez que a NF entra/permanece nesta
    # etapa (_ja_notificado trava o reenvio nas reconferências seguintes),
    # com os produtos separados em "consolidar" (Juntar_OC) e "criar OC nova"
    # (OC_Pendente), + empresa/filial pra ele ter tudo que precisa pra
    # criar/aprovar a OC no Sapiens. Se o único motivo de estar aqui for
    # itens_aguardando_aprovacao (vínculo já existia, OC já era conhecida do
    # Junior de uma rodada anterior), não há nada NOVO a notificar.
    if not _ja_notificado(numnfc) and (produtos_apr or produtos_pen):
        envio = notificar_junior_criar_oc(numnfc, codfor, nomfor,
                                  consolidar=produtos_apr, criar_nova=produtos_pen,
                                  codemp=codemp, codfil=codfil,
                                  numocp_apr=referencia_oc.get("apr"), numocp_pen=referencia_oc.get("pen"))
        if envio.get("sucesso"):
            _marcar_notificado(numnfc)
            registrar_log_acao(numnfc, chvnel, "NOTIFICACAO", "WhatsApp enviado ao Junior — OC pendente/consolidar", detalhe)
        else:
            registrar_log_acao(numnfc, chvnel, "ERRO", "Falha ao enviar WhatsApp ao Junior — OC pendente/consolidar", str(envio.get("erro")))
    print(f"  ⚠️  {detalhe}")
    return {"numnfc": numnfc, "resultado": AGUARDANDO_APROVACAO_OC, "detalhe": detalhe}


def _produtos_dos_acumuladores(registro: dict) -> tuple[list, list]:
    """
    Remonta as listas (consolidar, criar_nova) a partir dos acumuladores
    persistidos (ocp_apr/ocp_pen) — pra os reenvios do WhatsApp usarem a
    mesma mensagem rica da notificação inicial.
      ocp_apr = "CodPro;QtdRec/..."          → consolidar
      ocp_pen = "CodPro;QtdRec;QtdOcp/..."    → criar_nova (com falta)
      
    """
    def _parse(s, com_oc):
        itens = []
        for pedaco in (s or "").split("/"):
            if not pedaco.strip():
                continue
            campos = pedaco.split(";")
            codpro = campos[0]
            qtd = float(campos[1]) if len(campos) > 1 else 0
            d = {"codpro": codpro, "despro": codpro, "profor": codpro, "qtd": qtd}
            if com_oc:
                qtd_oc = float(campos[2]) if len(campos) > 2 else 0
                d["qtd_oc"] = qtd_oc
                d["falta"] = qtd - qtd_oc
            itens.append(d)
        return itens
    consolidar = _parse(registro.get("ocp_apr"), com_oc=False)
    criar_nova = _parse(registro.get("ocp_pen"), com_oc=True)
    return consolidar, criar_nova


def reconferir_oc_pendente(registro: dict) -> dict:
    """Roda a cada 1min pra NF em AGUARDANDO_APROVACAO_OC."""
    numnfc, chvnel = registro["numnfc"], registro["chvnel"]
    codemp, codfil, codfor = registro["codemp"], registro["codfil"], registro["codfor"]
    # referencia_oc: {"apr": numocp, "pen": numocp} — só entram as chaves cuja
    # geração via webservice deu certo (ver _rodar_verificacao_oc). Uma NF
    # pode ter as duas ao mesmo tempo (parte dos itens em Juntar_OC, parte em
    # OC_Pendente) — só avança quando TODAS as presentes estiverem aprovadas
    # (SITAPR='APR') — nunca existe "já aprovada" de fato no Sapiens no
    # momento da criação (ver verificar_oc_aprovada): TODA OC nova, seja do
    # caminho Juntar_OC ou OC_Pendente, nasce em análise (SITAPR='ANA') e
    # precisa de aprovação manual antes de poder ser usada de verdade.
    referencia_oc = registro.get("referencia") or {}
    erros_webservice = referencia_oc.get("erros_webservice") or []
    bloqueio = referencia_oc.get(CANCELAMENTO_BLOQUEADO)
    numocps = [v for k, v in referencia_oc.items() if v and k not in ("erros_webservice", CANCELAMENTO_BLOQUEADO)]

    try:
        if bloqueio:
            # NF parada porque o Sapiens recusou cancelar uma OC (ver
            # _rodar_verificacao_oc). Nada de webservice aqui: só compara a
            # "foto" guardada das linhas de OC com o estado atual (consulta
            # leve). Igual → continua parada, sem log nem reenvio. Diferente →
            # o comprador mexeu na OC: reconfere do zero.
            atual = assinatura_linhas_oc(codemp, codfil, [tuple(x) for x in bloqueio.get("linhas", [])])
            if atual == bloqueio.get("assinatura"):
                return {"numnfc": numnfc, "resultado": AGUARDANDO_APROVACAO_OC,
                        "detalhe": "Cancelamento de OC recusado — aguardando o comprador ajustar a OC no Sapiens"}
            registrar_log_acao(numnfc, chvnel, AGUARDANDO_APROVACAO_OC,
                               "OC alterada no Sapiens — reconferindo Verificação OC",
                               f"antes={bloqueio.get('assinatura')} | agora={atual}")
            print(f"  🔁 NF {numnfc} — OC bloqueada mudou no Sapiens, reconferindo Verificação OC...")
            return _rodar_verificacao_oc(_dados_nf_do_registro(registro))

        if not numocps:
            if erros_webservice:
                # A geração da OC já falhou no webservice numa rodada
                # anterior (erro já foi acusado no log na hora — ver
                # _rodar_verificacao_oc) — NÃO tenta de novo automaticamente
                # a cada 1min. Fica parada aqui até ação manual (ex: corrigir
                # o motivo do erro e reprocessar), em vez de bater no mesmo
                # erro indefinidamente.
                detalhe = "Erro no webservice ao gerar OC — parado, sem nova tentativa automática: " + "; ".join(erros_webservice)
                print(f"  🛑 NF {numnfc} — {detalhe}")
                return {"numnfc": numnfc, "resultado": AGUARDANDO_APROVACAO_OC, "detalhe": detalhe}
            # Nenhuma OC foi gerada com sucesso ainda (é a primeira passada)
            # — RECONFERE a NF do zero (_rodar_verificacao_oc), em vez de
            # reenviar os acumuladores persistidos (que podem estar
            # DESATUALIZADOS: produtos cadastrados, OC criada por outro
            # caminho, etc). Assim a mensagem ao Junior sai sempre com os
            # dados ATUAIS.
            print(f"  🔁 NF {numnfc} — nenhuma OC gerada ainda, reconferindo com dados atuais...")
            return _rodar_verificacao_oc(_dados_nf_do_registro(registro))

        # Alguma OC referenciada foi cancelada/liquidada (SITOCP 4/5) enquanto
        # esperava aprovação? Não adianta seguir notificando o Junior pra
        # aprovar uma OC morta — reconfere a NF do zero (_rodar_verificacao_oc
        # limpa o vínculo morto e gera OC nova / notifica de novo com dados
        # atuais).
        oc_mortas = [n for n in numocps if oc_situacao(codemp, codfil, n)[0] in ("4", "5")]
        if oc_mortas:
            registrar_log_acao(numnfc, chvnel, AGUARDANDO_APROVACAO_OC,
                               f"OC(s) {oc_mortas} cancelada(s)/liquidada(s) — reconferindo Verificação OC do zero")
            print(f"  ↩️  NF {numnfc} — OC(s) {oc_mortas} morta(s), reconferindo Verificação OC...")
            return _rodar_verificacao_oc(_dados_nf_do_registro(registro))

        nao_aprovadas = [n for n in numocps if not verificar_oc_aprovada(codemp, codfil, n)]
        if nao_aprovadas:
            # Uma ou mais OCs informadas ainda não foram aprovadas (SITAPR != 'APR').
            registrar_log_acao(numnfc, chvnel, AGUARDANDO_APROVACAO_OC,
                               f"OC(s) {', '.join(map(str, nao_aprovadas))} ainda não aprovada(s) na reconferência")
            if not registro.get("notificado"):
                # Reenvia o aviso com os produtos remontados dos acumuladores
                # persistidos — só a primeira vez; reconferências seguintes
                # não repetem enquanto notificado=1.
                consolidar, criar_nova = _produtos_dos_acumuladores(registro)
                envio = notificar_junior_criar_oc(numnfc, codfor, registro["nomfor"],
                                          consolidar=consolidar, criar_nova=criar_nova,
                                          codemp=codemp, codfil=codfil,
                                          numocp_apr=referencia_oc.get("apr"), numocp_pen=referencia_oc.get("pen"))
                if envio.get("sucesso"):
                    _marcar_notificado(numnfc)
                    registrar_log_acao(numnfc, chvnel, "NOTIFICACAO", "WhatsApp enviado ao Junior — OC ainda não aprovada")
                    print(f"  ⏳ NF {numnfc} — OC(s) {nao_aprovadas} ainda não aprovada(s), notificando Junior")
                else:
                    registrar_log_acao(numnfc, chvnel, "ERRO", "Falha ao enviar WhatsApp ao Junior — OC ainda não aprovada", str(envio.get("erro")))
                    print(f"  ⚠️  NF {numnfc} — falha ao notificar Junior: {envio.get('erro')}")
            else:
                print(f"  ⏳ NF {numnfc} — OC(s) {nao_aprovadas} ainda não aprovada(s), já notificado (sem reenvio)")
            return {"numnfc": numnfc, "resultado": AGUARDANDO_APROVACAO_OC, "detalhe": f"OC(s) {nao_aprovadas} ainda não aprovada(s)"}

        registrar_log_acao(
            numnfc, chvnel, AGUARDANDO_APROVACAO_OC,
            f"OC(s) {numocps} aprovada(s) (SITAPR='APR') — voltando pra Verificação OC",
        )
        print(f"  🔁 NF {numnfc} — OC(s) {numocps} aprovada(s), reconferindo Verificação OC...")
        return _rodar_verificacao_oc(_dados_nf_do_registro(registro))
    except Exception as e:
        traceback.print_exc()
        registrar_log_acao(numnfc, chvnel, "ERRO", "Exceção reconferindo OC pendente", str(e))
        print(f"  ⚠️  NF {numnfc} — erro reconferindo OC: {e}")
        return {"numnfc": numnfc, "resultado": "ERRO", "detalhe": str(e)}

def reconferir_integracao(registro: dict) -> dict:
    """ 2° Verificação roda de 2 em 2 min para toda NF que ainda está no fluxo."""

    numnfc, chvnel = registro["numnfc"], registro["chvnel"] 
    codemp, codfil, codfor = registro["codemp"], registro["codfil"], registro["codfor"]

    try:
        # AGUARDANDO_INTEGRACAO é um beco sem saída no scheduler de 1min
        # (passos 2/3/4 não olham esse estágio). Se a NF só chegou aqui porque
        # um item estava vinculado a uma OC aprovada, e essa OC foi
        # cancelada/liquidada (SITOCP 4/5) depois, a NF ficou "liberada" em
        # cima de nada — devolve pra VERIFICACAO_OC pra o job de 1min
        # reconferir do zero (vai limpar o vínculo morto e gerar OC nova /
        # notificar o Junior).
        if registro.get("status_interno") == AGUARDANDO_INTEGRACAO:
            for seqipc in listar_seqipc_nf(chvnel):
                it = buscar_item_nf(chvnel, seqipc)
                if not it or not it.get("numocp"):
                    continue
                sitocp_it = oc_situacao(it["codemp"], it["codfil"], it["numocp"])[0]
                if sitocp_it in ("4", "5"):
                    detalhe = (f"OC {it['numocp']} do item {seqipc} está "
                               f"{descricao_sitocp(sitocp_it)} — NF volta pra Verificação OC")
                    registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC,
                                       "OC vinculada cancelada/liquidada — voltando pra Verificação OC", detalhe)
                    _registrar_estagio(_dados_nf_do_registro(registro), VERIFICACAO_OC, detalhe)
                    print(f"  ↩️  NF {numnfc} - {detalhe}")
                    return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}

        if not nf_ja_integrada(codemp, codfil, codfor, numnfc, chvnel):
            return {"numnfc": numnfc, "resultado": registro["status_interno"],
                    "detalhe": "Ainda não consta em E440NFC - permanece"}

        detalhe = "NF ja consta na E440NFC (processada/importada) - saindo do fluxo"
        registrar_log_acao(numnfc, chvnel, INTEGRADA, "NF integrada (E440NFC) - 2 verificação, saindo da tela", detalhe)
        _registrar_estagio(_dados_nf_do_registro(registro), INTEGRADA, detalhe)
        print(f" ✅ NF {numnfc} - {detalhe}")
        return{"numnfc":numnfc, "resultado": INTEGRADA, "detalhe":detalhe}
    except Exception as e:
        traceback.print_exc()
        registrar_log_acao(numnfc, chvnel, "ERRO", "Exceção na 2ª verificação (integração E440NFC)", str(e))
        print(f" ⚠️ NF {numnfc} - erro na 2ª verificação: {e}")
        return {"numnfc": numnfc, "resultado":"ERRO", "detalhe": str(e)}


def reconferir_integrada(registro: dict) -> dict:
    """
    Reconferência de uma NF em INTEGRADA (estágio terminal): se ela deixou de
    constar na E440NFC (foi excluída/estornada no Sapiens depois de integrada),
    devolve pra VERIFICACAO_OC — o scheduler a reprocessa na rodada seguinte
    (reprocessar_travada). Os vínculos NUMOCP/SEQIPO da E000IPC continuam
    valendo, então se as OCs seguem aprovadas e abertas ela volta direto pra
    AGUARDANDO_INTEGRACAO sem gerar OC nova; se alguma OC morreu, o fluxo
    normal limpa o vínculo e regenera. NF cancelada nesse meio-tempo cai na
    trava de INDCAN dentro de _rodar_verificacao_oc (vira CANCELADA).

    Falha de consulta (exceção) não muda nada — a NF permanece INTEGRADA e a
    próxima rodada tenta de novo.
    """
    numnfc, chvnel = registro["numnfc"], registro["chvnel"]
    codemp, codfil, codfor = registro["codemp"], registro["codfil"], registro["codfor"]
    try:
        if nf_consta_e440nfc(codemp, codfil, codfor, numnfc):
            return {"numnfc": numnfc, "resultado": INTEGRADA, "detalhe": "Continua na E440NFC - permanece"}

        detalhe = "NF estava INTEGRADA mas não consta mais na E440NFC (excluída/estornada no Sapiens) - voltando pra Verificação OC"
        registrar_log_acao(numnfc, chvnel, VERIFICACAO_OC, "NF integrada sumiu da E440NFC - voltando pro fluxo", detalhe)
        _registrar_estagio(_dados_nf_do_registro(registro), VERIFICACAO_OC, detalhe)
        print(f"  ↩️  NF {numnfc} - {detalhe}")
        return {"numnfc": numnfc, "resultado": VERIFICACAO_OC, "detalhe": detalhe}
    except Exception as e:
        traceback.print_exc()
        registrar_log_acao(numnfc, chvnel, "ERRO", "Exceção reconferindo NF INTEGRADA (E440NFC)", str(e))
        print(f"  ⚠️  NF {numnfc} - erro reconferindo INTEGRADA: {e}")
        return {"numnfc": numnfc, "resultado": "ERRO", "detalhe": str(e)}

