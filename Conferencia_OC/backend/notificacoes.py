from db_local import get_local_connection


def notificar_admins(mensagem: str, chvnel: str = "", numnfc: str = "") -> dict:
    """Grava a notificação no sino de todos os admins (Jéssica)."""
    try:
        conn = get_local_connection()
        try:
            admins = [r["codusu"] for r in conn.execute(
                "SELECT codusu FROM nf_usuarios WHERE is_admin = 1")]
            for cod in admins:
                conn.execute(
                    "INSERT INTO nf_notificacoes (destinatario, chvnel, numnfc, mensagem) "
                    "VALUES (?, ?, ?, ?)", (cod, chvnel, numnfc, mensagem))
            conn.commit()
        finally:
            conn.close()
        if not admins:
            return {"sucesso": False, "erro": "Nenhum admin cadastrado"}
        return {"sucesso": True}
    except Exception as e:
        return {"sucesso": False, "erro": str(e)}


def avisar_aguardando_ligacao(numnfc, codfor, nomfor, sem_ligacao=None,
                              cadastro_incompleto=None, chvnel=""):
    qtd = len(sem_ligacao or []) + len(cadastro_incompleto or [])
    return notificar_admins(
        f"NF {numnfc} – {nomfor}: aguardando ligação/cadastro de produto ({qtd} item(ns)).",
        chvnel, numnfc)


def avisar_aguardando_aprovacao_oc(numnfc, codfor, nomfor, consolidar=None, criar_nova=None,
                                   codemp="", codfil="", numocp_apr=None, numocp_pen=None, chvnel=""):
    qtd = len(consolidar or []) + len(criar_nova or [])
    return notificar_admins(
        f"NF {numnfc} – {nomfor}: aguardando aprovação/geração de OC ({qtd} item(ns)).",
        chvnel, numnfc)


def avisar_cancelamento_bloqueado(numnfc, codfor, nomfor, codemp, codfil, bloqueio, chvnel=""):
    return notificar_admins(
        f"NF {numnfc} – {nomfor}: Sapiens recusou cancelar a OC {bloqueio.get('numocp')}.",
        chvnel, numnfc)