import requests

# ─── Configuração ────────────────────────────────────────────────────────────
WHATSAPP_URL    = "http://localhost:3000/send-message"
TELEFONE_JUNIOR = "554333770003"   # <-- número do Junior
TELEFONE_FISCAL = "554333770040"   # <-- número do fiscal


def enviar_whatsapp(telefone: str, mensagem: str) -> dict:
    """
    Envia mensagem via WhatsApp Bridge (Node.js local, whatsapp-service/index.js).

    Sempre devolve "sucesso": bool — quem chama (nf_processor.py) precisa
    conferir antes de marcar a notificação como enviada/logar sucesso. Antes,
    essa função só devolvia r.json() cru (ou {"erro":...} na exceção) e quem
    chamava nunca olhava o retorno — se o bridge estivesse fora do ar, preso
    reconectando (ver o "conflict/replaced" que aparece nos logs do Baileys)
    ou respondesse HTTP 500, a mensagem falhava calada, mas o sistema
    marcava `notificado=1` e gravava "WhatsApp enviado" no log do mesmo
    jeito — a próxima rodada nunca tentava reenviar, porque achava que já
    tinha dado certo.
    """
    try:
        r = requests.post(
            WHATSAPP_URL,
            json={"number": telefone, "message": mensagem},
            timeout=10
        )
        corpo = r.json()
        if r.status_code != 200 or corpo.get("status") != "ok":
            return {"sucesso": False, "erro": corpo.get("error") or corpo.get("erro") or f"HTTP {r.status_code}", "raw": corpo}
        return {"sucesso": True, **corpo}
    except Exception as e:
        return {"sucesso": False, "erro": str(e)}


def _listar_produtos(produtos: list[dict] | None) -> str:
    """Formata uma lista de itens (profor/codpro/despro) pra exibir na mensagem do WhatsApp."""
    if not produtos:
        return "—"
    return "\n".join(
        f"• {p.get('despro') or p.get('profor') or p.get('codpro') or '?'}"
        f" ({p.get('profor') or p.get('codpro') or '—'})"
        for p in produtos
    )


# Etapa "Verificação Cadastral" — produto da NF sem ligação com o portfólio
# do fornecedor (E403FPR). Chamada na entrada da coluna "Aguardando Lig.
# Produto x Fornecedor" e, se a reconferência de 1min mostrar que ainda não
# foi resolvido, no reenvio (uma vez só por etapa — ver nf_processor.py).
def notificar_fiscal_cadastro_pendente(numnfc: str, codfor: str, nomfor: str, produtos: list[dict] | None = None) -> dict:
    """Avisa o fiscal que o produto da NF precisa ser cadastrado no portfólio do fornecedor."""
    mensagem = (
        f"🏷️ *Ligação Produto x Fornecedor pendente*\n\n"
        f"NF: *{numnfc}*\n"
        f"Fornecedor: {nomfor} ({codfor})\n\n"
        f"Produto(s) sem ligação no portfólio deste fornecedor:\n{_listar_produtos(produtos)}\n\n"
        f"Por favor, cadastre no Sapiens antes de prosseguir."
    )
    return enviar_whatsapp(TELEFONE_FISCAL, mensagem)


def _listar_produtos_cadastro_incompleto(produtos: list[dict] | None) -> str:
    """Formata itens sem derivação/depósito, dizendo exatamente o que falta
    em cada um — igual ao espírito de _listar_produtos, mas com o motivo."""
    if not produtos:
        return "—"
    linhas = []
    for p in produtos:
        nome = p.get("despro") or p.get("profor") or p.get("codpro") or "?"
        cod  = p.get("profor") or p.get("codpro") or "—"
        faltas = [f for cond, f in (
            (p.get("sem_derivacao"), "derivação"),
            (p.get("sem_deposito"), "vínculo de depósito/estoque"),
        ) if cond]
        motivo = " e sem ".join(faltas) if faltas else "cadastro incompleto"
        linhas.append(f"• {nome} ({cod}) — sem {motivo}")
    return "\n".join(linhas)


# Etapa "Verificação Cadastral" — produto já ligado ao portfólio do
# fornecedor (E403FPR), mas sem derivação (E075DER) e/ou vínculo de
# depósito/estoque (E210EST) cadastrado no Sapiens. Mesma coluna que a
# ligação de portfólio (AGUARDANDO_LIGACAO) — é outro tipo de pendência
# cadastral que bloqueia o mesmo responsável (fiscal) antes de a NF seguir.
def notificar_fiscal_produto_sem_cadastro(numnfc: str, codfor: str, nomfor: str, produtos: list[dict] | None = None) -> dict:
    """Avisa o fiscal que produto(s) da NF estão sem derivação e/ou vínculo de depósito/estoque cadastrado."""
    mensagem = (
        f"🏷️ *Cadastro de produto incompleto*\n\n"
        f"NF: *{numnfc}*\n"
        f"Fornecedor: {nomfor} ({codfor})\n\n"
        f"Produto(s) com cadastro incompleto no Sapiens:\n{_listar_produtos_cadastro_incompleto(produtos)}\n\n"
        f"Por favor, complete o cadastro antes de prosseguir."
    )
    return enviar_whatsapp(TELEFONE_FISCAL, mensagem)

def notificar_fiscal_pendencias(numnfc: str, codfor: str, nomfor: str,
                                 sem_ligacao: list[dict] | None = None,
                                 cadastro_incompleto: list[dict] | None = None) -> dict:
    """Avisa o fiscal de TODAS as pendências cadastrais da NF numa única
    mensagem (ligação de portfólio + derivação/depósito), em vez de uma
    mensagem por tipo de pendência."""
    partes = [
        f"🏷️ *Pendência(s) cadastral(is) — NF {numnfc}*",
        f"Fornecedor: {nomfor} ({codfor})",
    ]
    if sem_ligacao:
        partes.append(f"\n*Sem ligação no portfólio deste fornecedor:*\n{_listar_produtos(sem_ligacao)}")
    if cadastro_incompleto:
        partes.append(f"\n*Cadastro incompleto (derivação/depósito):*\n{_listar_produtos_cadastro_incompleto(cadastro_incompleto)}")
    partes.append("\nPor favor, cadastre/complete no Sapiens antes de prosseguir.")
    return enviar_whatsapp(TELEFONE_FISCAL, "\n".join(partes))


def _fmt_produto_pen(p: dict) -> str:
    """Item que precisa de OC nova (OC_Pendente) — mostra precisa/tem/falta."""
    nome = p.get("despro") or p.get("profor") or p.get("codpro") or "?"
    cod  = p.get("profor") or p.get("codpro") or "—"
    qtd  = p.get("qtd", 0)
    tem  = p.get("qtd_oc", 0)
    falta = p.get("falta", qtd)
    return f"• {nome} ({cod}) — precisa {qtd:g}, tem {tem:g}, faltam {falta:g}"


def _fmt_produto_apr(p: dict) -> str:
    """Item que será consolidado (Juntar_OC) — só a quantidade."""
    nome = p.get("despro") or p.get("profor") or p.get("codpro") or "?"
    cod  = p.get("profor") or p.get("codpro") or "—"
    qtd  = p.get("qtd", 0)
    return f"• {nome} ({cod}) — {qtd:g} un"


# Etapa "Verificação OC" — produtos sem OC suficiente para a NF. Lista os
# produtos separados em "criar OC nova" (OC_Pendente — falta saldo) e
# "consolidar OC" (Juntar_OC — saldo espalhado em várias OCs), com
# quantidade, empresa/filial. Chamada na entrada da coluna "Aguard.
# Aprovação/Geração de OC" e no reenvio (uma vez só por etapa — ver
# nf_processor.py).
def notificar_junior_criar_oc(numnfc: str, codfor: str, nomfor: str,
                              consolidar: list[dict] | None = None,
                              criar_nova: list[dict] | None = None,
                              codemp: str = "", codfil: str = "",
                              numocp_apr: str | None = None,
                              numocp_pen: str | None = None) -> dict:
    """
    Avisa o Junior que é necessária a criação/aprovação de OC pra esta NF.
    `numocp_apr`/`numocp_pen`: NUMOCP da OC já gerada (webservice) pra cada
    caminho, quando já existir — ajuda o Junior a achar a OC certa no
    Sapiens direto, sem precisar procurar pelo produto/fornecedor.
    """
    consolidar = consolidar or []
    criar_nova = criar_nova or []

    empfil = f" · Emp {codemp}/Fil {codfil}" if (codemp or codfil) else ""
    partes = [
        f"📋 *OC necessária — NF {numnfc}*",
        f"Fornecedor: {nomfor} ({codfor}){empfil}",
    ]
    if criar_nova:
        oc_txt = f" (OC {numocp_pen})" if numocp_pen else ""
        partes.append(f"\n🔴 *Criar OC nova (falta saldo){oc_txt}:*\n" +
                      "\n".join(_fmt_produto_pen(p) for p in criar_nova))
    if consolidar:
        oc_txt = f" (OC {numocp_apr})" if numocp_apr else ""
        partes.append(f"\n🟡 *Consolidar OC (saldo espalhado){oc_txt}:*\n" +
                      "\n".join(_fmt_produto_apr(p) for p in consolidar))
    if not criar_nova and not consolidar:
        # Reenvio sem detalhe (registro antigo) — mensagem genérica.
        partes.append("\nProdutos aguardando OC. Confira no Sapiens.")

    partes.append("\nPor favor, crie/aprove a OC no Sapiens.")
    return enviar_whatsapp(TELEFONE_JUNIOR, "\n".join(partes))


# Etapa "Verificação OC" — o Sapiens recusou cancelar uma OC antiga (ex: "OC já
# foi fechada e possui Aprovação Multinível"), então a NF não pôde ser
# consolidada/gerada. Enviada UMA vez por bloqueio (ver nf_processor.py); a NF
# só é retomada sozinha quando a OC mudar no Sapiens.
def notificar_junior_cancelamento_bloqueado(numnfc: str, codfor: str, nomfor: str,
                                            codemp: str, codfil: str, bloqueio: dict) -> dict:
    empfil = f" · Emp {codemp}/Fil {codfil}" if (codemp or codfil) else ""
    partes = [
        f"⛔ *Cancelamento de OC recusado — NF {numnfc}*",
        f"Fornecedor: {nomfor} ({codfor}){empfil}",
        f"\nO Sapiens não deixou cancelar a OC *{bloqueio.get('numocp')}* (linha {bloqueio.get('seqipo')}), "
        f"usada pelo item {bloqueio.get('seqipc')} da NF.",
        f"Motivo: {bloqueio.get('erro')}",
        "\nNenhuma OC nova foi gerada. Por favor, ajuste/cancele essa OC no Sapiens — "
        "o sistema retoma a NF sozinho quando ela mudar.",
    ]
    return enviar_whatsapp(TELEFONE_JUNIOR, "\n".join(partes))