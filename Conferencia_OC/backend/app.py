import os
import re
import sys
from io import BytesIO
from functools import wraps

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

from flask import Flask, jsonify, render_template, request, session, send_file
from db_oracle   import get_connection, rows_to_dict
from db_local    import get_local_connection, registrar_log_acao
from oc_service  import (
    conferir_nf_vs_portfolio, stanfv_atual,
    buscar_conferencia_integracao, confirmar_conferencia_integracao,
)
from scheduler_nf import iniciar_scheduler_nf
from usuarios_service import (
    buscar_usuario, obter_ou_criar_usuario, definir_senha, validar_login,
    listar_usuarios, atribuicao_atual, atribuicoes_ativas, atribuir_nf,
    desatribuir_nf, listar_notificacoes, contar_notificacoes_nao_lidas,
    marcar_notificacoes_lidas, definir_colunas_visiveis, atualizar_cor,
    desempenho_usuarios,
)

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))

def _ler_env(chave: str, default: str = "") -> str:
    caminho = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha.startswith(f"{chave}="):
                    return linha.split("=", 1)[1].strip()
    return os.environ.get(chave, default)

app.secret_key = _ler_env("FLASK_SECRET_KEY") or os.urandom(24)

def admin_required(f):
    """Bloqueia a rota pra quem não é admin (usado nas ações restritas à Jéssica)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"erro": "Ação restrita ao administrador."}), 403
        return f(*args, **kwargs)
    return wrapper

@app.before_request
def _exigir_login_api():
    if request.remote_addr in ("127.0.0.1", "::1"):
        return
    if request.path.startswith("/api/") and not request.path.startswith("/api/auth/"):
        if not session.get("codusu"):
            return jsonify({"erro": "Não autenticado."}), 401

# app.run roda com debug=False, threaded=False (ver fim do arquivo) — sem
# reloader do Werkzeug, então existe só 1 processo mesmo, sem risco da
# duplicação que a checagem de WERKZEUG_RUN_MAIN existia pra evitar (isso só
# seria um problema com debug=True, que sobe DOIS processos e rodaria o
# scheduler duas vezes). Com debug=False essa variável nunca é setada, então
# aquela checagem deixava o scheduler sem iniciar NUNCA — chama direto.
iniciar_scheduler_nf()


# ═══════════════════════════════════════════════════════════════════════════════
# PÁGINAS
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("dashboard_nf.html")

# ═══════════════════════════════════════════════════════════════════════════════
# NF — dashboard
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/nf/dashboard", methods=["POST"])
def dashboard_nf():
    
    sql_base = """
        SELECT
            e070fil.codemp, e070fil.codfil,
            e000nfc.numnfc, e000nfc.chvnel,
            e000nfc.cgcfor, e000nfc.cgcfil,
            e095for.codfor, e095for.nomfor,
            e000nfc.datemi, e000nfc.vlrfin,
            NVL((
                SELECT MIN(e000par.vctpar)
                FROM sapiens.e000par
                WHERE e000par.chvnel = e000nfc.chvnel
                  AND e000par.cgcfor = e000nfc.cgcfor
                  AND e000par.cgcfil = e000nfc.cgcfil
            ), e000nfc.datemi + 7) AS vctpar,
            CASE WHEN e000nfc.codedc = '55' THEN 'NFE' ELSE 'CTE' END AS TipNfe,
            NVL((
                SELECT COUNT(e000ipc.codpro)
                FROM sapiens.e000ipc
                WHERE e000ipc.chvnel = e000nfc.chvnel
                  AND e000ipc.cgcfor = e000nfc.cgcfor
                  AND e000ipc.cgcfil = e000nfc.cgcfil
            ), 1) AS QtdIte,
            NVL((
   		 Select 'S' from sapiens.e000ipc a
   		 where a.chvnel=e000nfc.chvnel
    		 and (a.noppro like ('%411%') or a.noppro like ('%202%'))
   		 and ROWNUM = 1), 'N'
		) AS IndDev
        FROM sapiens.e000nfc, sapiens.e095for, sapiens.e070fil
        WHERE e000nfc.indcan <> 'S'
          AND NOT EXISTS (
              Select 1 from sapiens.e000ipc a
              where a.chvnel=e000nfc.chvnel
                and a.noppro like ('%949%')
          )
          AND NOT EXISTS (
              SELECT 1 FROM sapiens.e440nfc
              WHERE e440nfc.codemp = e070fil.codemp
                AND e440nfc.codfil = e070fil.codfil
                AND e440nfc.codfor = e095for.codfor
                AND e440nfc.numnfc = e000nfc.numnfc
                AND e440nfc.sitnfc IN (1,2)
          )
          AND e070fil.numcgc = e000nfc.cgcfil
          AND e000nfc.cgcfor = e095for.cgccpf
          AND e095for.sitfor = 'A'
          AND e000nfc.datemi > SYSDATE - 60
          AND e070fil.codemp = 2
          AND EXISTS (
              SELECT 1 FROM sapiens.e440cnt
              WHERE e440cnt.codemp = e070fil.codemp
                AND e440cnt.codfil = e070fil.codfil
                AND INSTR(e440cnt.obscnt, e000nfc.numnfc) > 0
                AND e440cnt.sitcnt = 2 )
    """
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql_base)
            nfs_raw = rows_to_dict(cur)
        finally:
            conn.close()

        nfs = [
            {
                "numnfc": nf["NUMNFC"],
                "chvnel": nf["CHVNEL"],
                "codfil": nf["CODFIL"],
                "codemp": nf["CODEMP"],
                "cgcfor": nf["CGCFOR"],
                "cgcfil": nf["CGCFIL"],
                "codfor": nf["CODFOR"],
                "nomfor": nf["NOMFOR"],
                "datemi": nf["DATEMI"],
                "vlrfin": nf["VLRFIN"],
                "qtdite": nf["QTDITE"],
                "vctpar": nf["VCTPAR"],
                "tipnfe": nf["TIPNFE"],
                "inddev": nf["INDDEV"],
            }
            for nf in nfs_raw
        ]

        return jsonify({
            "nfs":        nfs,
            "divergentes": nfs,
            "ok":         [],
            "resumo":     {"total": len(nfs)},
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[dashboard_nf] ERRO: {e!r}")
        return jsonify({"erro": str(e)}), 500


@app.route("/api/nf/pendente-importacao-xml", methods=["GET"])
def nf_pendente_importacao_xml():
    """
    NFs que já têm contagem (E440CNT, SITCNT=2) mas o XML ainda não foi
    importado no SDE, vão aparecer no SELECT de /api/nf/dashboard na primeira coluna
    enquanto o XML não entrar.
    """
    sql = """
        SELECT numcnt, obscnt
        FROM sapiens.e440cnt
        WHERE codemp = 2
          AND sitcnt = 2
          AND NOT EXISTS (
              SELECT 1 FROM sapiens.e000nfc
              WHERE TO_CHAR(numnfc) = REGEXP_SUBSTR(obscnt, '^\\S+')
                AND e000nfc.datemi > SYSDATE - 60
          )
    """
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = rows_to_dict(cur)
        finally:
            conn.close()
        itens = [{"numcnt": r["NUMCNT"], "obscnt": r["OBSCNT"]} for r in rows]
        return jsonify({"itens": itens})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[nf_pendente_importacao_xml] ERRO: {e!r}")
        return jsonify({"erro": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# OC — Ordens de Compra (E420OCP / E420IPO)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/oc/conferir-nf", methods=["POST"])
def oc_conferir_nf():
    """
    Compara contra TODAS as OCs
    Agora usa conferir_nf_vs_portfolio(), que retorna um dicionário com:

      tem_oc         → existe OC vinculada ao portfólio pra algum item
      itens_ok       → todos os itens têm cobertura de quantidade
      itens_nf       → detalhe item a item com flag coberto/divergência
      ocs_abertas    → OCs encontradas (só as vinculadas ao portfólio)
      fora_portfolio → itens da NF que não estão no portfólio deste fornecedor
    """
    body   = request.get_json(force=True)
    codemp = str(body.get("codemp") or "").strip()
    codfil = str(body.get("codfil") or "").strip()
    codfor = str(body.get("codfor") or "").strip()
    chvnel = str(body.get("chvnel") or "").strip()
    cgcfor = str(body.get("cgcfor") or "").strip()
    cgcfil = str(body.get("cgcfil") or "").strip()

    if not all([codemp, codfil, codfor, chvnel, cgcfor, cgcfil]):
        return jsonify({"erro": "Informe codemp, codfil, codfor, chvnel, cgcfor e cgcfil."}), 400

    try:
        resultado = conferir_nf_vs_portfolio(codemp, codfil, codfor, chvnel, cgcfor, cgcfil)
        return jsonify(resultado)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500 

@app.route("/api/oc/processamentos", methods=["GET"])
def oc_processamentos():
    """
    Retorna, por NF, a ÚLTIMA linha de nf_processamento — estágio atual
    (status_interno), detalhe, referencia (PROFOR pendentes ou NUMOCP) e os
    dados de exibição do card (fornecedor, valor, data, qtd itens), que
    ficam salvos aqui desde a primeira vez que a NF foi vista. É essa
    persistência local que permite ao dashboard continuar mostrando o card
    de uma NF mesmo depois que ela sumir do SELECT de /api/nf/dashboard
    (que exclui SITNFC=6, marcado já na primeira etapa).

    """
    import json as _json
    try:
        conn = get_local_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT t.* FROM nf_processamento t
            INNER JOIN (
                SELECT numnfc, MAX(id) AS max_id FROM nf_processamento GROUP BY numnfc
            ) u ON u.numnfc = t.numnfc AND u.max_id = t.id
            ORDER BY t.id DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        # Extrai status_interno/detalhe/referencia do JSON de divergencias
        for row in rows:
            try:
                divs = _json.loads(row.get("divergencias") or "[]")
                if divs:
                    row["status_interno"] = divs[0].get("tipo", row["status"])
                    row["detalhe"]        = divs[0].get("detalhe", "")
                    row["referencia"]     = divs[0].get("referencia")
                else:
                    row["status_interno"] = row["status"]
                    row["detalhe"]        = ""
                    row["referencia"]     = None
            except Exception:
                row["status_interno"] = row["status"]
                row["detalhe"]        = ""
                row["referencia"]     = None
            row["processado_em"] = row.pop("criado_em", "")

        # Filtro de exibição: card da coluna "Aguardando Integração" some
        # quando E000NFC.STANFV = '3' (NF já processada/importada de verdade
        # — não precisa mais aparecer aguardando). Não remove a linha da
        # resposta — só troca o status_interno pra um valor sem case no
        # frontend ("NF_INTEGRADA"): a coluna 1 (Verificação Cadastral) usa
        # o procMap desta resposta pra saber quais NFs já têm registro, então
        # remover a linha faria essa NF reaparecer ali por engano. Qualquer
        # erro na consulta mantém o card visível (fail open). Consulta ao
        # vivo, não altera o que fica salvo no SQLite.
        for row in rows:
            if row["status_interno"] == "AGUARDANDO_INTEGRACAO":
                try:
                    if stanfv_atual(row.get("chvnel", "")) == "3":
                        row["status_interno"] = "NF_INTEGRADA"
                except Exception:
                    pass

        return jsonify(rows)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/oc/log/<numnfc>", methods=["GET"])
def oc_log_acoes(numnfc):
    """
    Retorna o histórico completo de ações registradas (nf_log_acoes) para
    uma NF específica, em ordem cronológica — pra comparar passo a passo
    o que o sistema fez (Etapa 1, portfólio, item a item, notificações,
    monitoramento, liberação).
    """
    try:
        conn = get_local_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT id, numnfc, chvnel, etapa, acao, detalhe, seqipc, criado_em
            FROM nf_log_acoes
            WHERE numnfc = ?
            ORDER BY id ASC
        """, (str(numnfc),))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# Nome fixo da ação gravada em nf_log_acoes quando a conferência de
# integração é confirmada — usado também pelo /api/crm/estatisticas pra
# contar quantas NFs foram de fato resolvidas (saíram de AGUARDANDO_INTEGRACAO).
ACAO_CONFERENCIA_INTEGRACAO = "Conferência de integração confirmada"


def _pode_conferir_integracao(chvnel: str) -> bool:
    """Admin sempre pode; usuário comum só se a NF estiver atribuída a ele."""
    if session.get("is_admin"):
        return True
    atrib = atribuicao_atual(chvnel)
    return bool(atrib) and atrib["codusu"] == session.get("codusu")


@app.route("/api/oc/conferencia-integracao", methods=["GET"])
def oc_conferencia_integracao():
    """
    Roda a classificação fiscal (TNSPRO/CODSNF/SOMFRE/CTARED/CODCCU) da NF
    pra exibir na tela — não grava nada. Substitui o passo manual de rodar
    esse SELECT direto no banco antes do UPDATE de liberação.
    """
    chvnel = str(request.args.get("chvnel") or "").strip()
    cgcfor = str(request.args.get("cgcfor") or "").strip()
    cgcfil = str(request.args.get("cgcfil") or "").strip()
    if not chvnel or not cgcfor or not cgcfil:
        return jsonify({"erro": "Informe chvnel, cgcfor e cgcfil."}), 400
    if not _pode_conferir_integracao(chvnel):
        return jsonify({"erro": "Esta NF não está atribuída a você."}), 403
    try:
        return jsonify(buscar_conferencia_integracao(chvnel, cgcfor, cgcfil))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500


@app.route("/api/oc/conferencia-integracao/confirmar", methods=["POST"])
def oc_conferencia_integracao_confirmar():
    """
    Confirma a conferência: grava E000IPC (TNSPRO/CTARED/CODCCU por item) e
    E000NFC (CODSNF/SOMFRE do cabeçalho). Mesma permissão da consulta —
    admin, ou o usuário a quem a NF estiver atribuída.
    """
    body   = request.get_json(force=True)
    chvnel = str(body.get("chvnel") or "").strip()
    numnfc = str(body.get("numnfc") or "").strip()
    cgcfor = str(body.get("cgcfor") or "").strip()
    cgcfil = str(body.get("cgcfil") or "").strip()
    if not chvnel or not numnfc or not cgcfor or not cgcfil:
        return jsonify({"erro": "Informe chvnel, numnfc, cgcfor e cgcfil."}), 400
    if not _pode_conferir_integracao(chvnel):
        return jsonify({"erro": "Esta NF não está atribuída a você."}), 403
    try:
        resultado = confirmar_conferencia_integracao(chvnel, cgcfor, cgcfil)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 409
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500

    registrar_log_acao(
        numnfc, chvnel, "AGUARDANDO_INTEGRACAO", ACAO_CONFERENCIA_INTEGRACAO,
        f"Confirmado por {session['codusu']} — {resultado}",
    )
    return jsonify(resultado)


@app.route("/api/oc/relatorio-aprovadas", methods=["GET"])
@admin_required
def oc_relatorio_aprovadas():
    """
    Relatório Excel (NF x OC gerada) das OCs aprovadas cuja NF chegou na
    coluna "Aguardando Integração" dentro do período informado.
    Query string: ?de=YYYY-MM-DD&ate=YYYY-MM-DD  (sem datas => somente hoje).
    Restrito ao admin.
    """
    from datetime import datetime
    from relatorio_oc import gerar_relatorio_oc_aprovadas
    de  = (request.args.get("de")  or "").strip() or None
    ate = (request.args.get("ate") or "").strip() or None
    try:
        conteudo = gerar_relatorio_oc_aprovadas(de, ate)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500
    sufixo = de or datetime.now().strftime("%Y-%m-%d")
    nome = f"relatorio_oc_aprovadas_{sufixo}.xlsx"
    return send_file(
        BytesIO(conteudo),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=nome,
    )


@app.route("/api/oc/alertas-nao-fechadas", methods=["GET"])
@admin_required
def oc_alertas_nao_fechadas():
    """
    OCs geradas e aprovadas pelo fluxo que estão há mais de 3 dias sem fechar
    (SITOCP 1/2/9, contando da emissão da OC). Só admin — alimenta o alerta
    destacado da sidebar. Resposta em cache (ver alertas_oc._TTL_SEG);
    ?force=1 refaz a consulta na hora.
    """
    from alertas_oc import ocs_nao_fechadas, DIAS_LIMITE
    try:
        itens = ocs_nao_fechadas(force=request.args.get("force") == "1")
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500
    return jsonify({"dias": DIAS_LIMITE, "itens": itens})


@app.route("/api/oc/detalhe", methods=["GET"])
@admin_required
def oc_detalhe():
    """Cabeçalho + itens de uma OC — modal de detalhes do alerta da sidebar."""
    codemp = str(request.args.get("codemp") or "").strip()
    codfil = str(request.args.get("codfil") or "").strip()
    numocp = str(request.args.get("numocp") or "").strip()
    if not codemp or not codfil or not numocp:
        return jsonify({"erro": "Informe codemp, codfil e numocp."}), 400
    from oc_service import buscar_oc_detalhe
    try:
        oc = buscar_oc_detalhe(codemp, codfil, numocp)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500
    if not oc:
        return jsonify({"erro": "OC não encontrada."}), 404
    return jsonify(oc)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTENTICAÇÃO — login por codusu, dois níveis (admin/comum)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/identificar", methods=["POST"])
def auth_identificar():
    """
    Passo 1 do login: só lê (não cria) — evita criar usuário fantasma por
    erro de digitação do codusu. existe=False → frontend pede nome/senha/cor
    (primeiro acesso de verdade); existe=True + precisa_criar_senha=True →
    já foi identificado antes (ex: admin atribuiu algo a ele) mas nunca
    definiu senha — mesma tela de criar senha, sem pedir nome de novo.
    """
    body = request.get_json(force=True)
    codusu = str(body.get("codusu") or "").strip()
    if not codusu:
        return jsonify({"erro": "Informe o código de usuário."}), 400
    usuario = buscar_usuario(codusu)
    if not usuario:
        return jsonify({"existe": False, "codusu": codusu})
    return jsonify({"existe": True, **usuario})


@app.route("/api/auth/criar-senha", methods=["POST"])
def auth_criar_senha():
    """Primeiro acesso: cria o usuário (se ainda não existir) e define senha + cor. Já loga em seguida."""
    body = request.get_json(force=True)
    codusu = str(body.get("codusu") or "").strip()
    nomusu = str(body.get("nomusu") or "").strip()
    senha  = str(body.get("senha") or "")
    cor    = str(body.get("cor") or "").strip()

    if not codusu or not senha or not cor:
        return jsonify({"erro": "Informe codusu, senha e cor."}), 400
    if len(senha) < 4:
        return jsonify({"erro": "Senha muito curta (mínimo 4 caracteres)."}), 400

    try:
        obter_ou_criar_usuario(codusu, nomusu)
        usuario = definir_senha(codusu, senha, cor)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400

    session["codusu"]   = codusu
    session["is_admin"] = usuario["is_admin"]
    return jsonify(usuario)


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.get_json(force=True)
    codusu = str(body.get("codusu") or "").strip()
    senha  = str(body.get("senha") or "")
    usuario = validar_login(codusu, senha)
    if not usuario:
        return jsonify({"erro": "Código de usuário ou senha inválidos."}), 401
    session["codusu"]   = codusu
    session["is_admin"] = usuario["is_admin"]
    return jsonify(usuario)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    codusu = session.get("codusu")
    if not codusu:
        return jsonify({"autenticado": False})
    usuario = buscar_usuario(codusu)
    if not usuario:
        session.clear()
        return jsonify({"autenticado": False})
    return jsonify({"autenticado": True, **usuario})


@app.route("/api/auth/cor", methods=["POST"])
def auth_trocar_cor():
    """Troca a cor pessoal do usuário logado (avatar + pontinhos de atribuição)."""
    codusu = session.get("codusu")
    if not codusu:
        return jsonify({"erro": "Não autenticado."}), 401
    body = request.get_json(force=True)
    cor = str(body.get("cor") or "").strip()
    try:
        usuario = atualizar_cor(codusu, cor)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify(usuario)


@app.route("/api/usuarios", methods=["GET"])
@admin_required
def usuarios_listar():
    """Lista de usuários já com senha definida — pra popular o seletor de 'atribuir a...' (só admin)."""
    return jsonify(listar_usuarios())


@app.route("/api/usuarios/<codusu>/colunas", methods=["POST"])
@admin_required
def usuarios_definir_colunas(codusu):
    """
    Define quais colunas do board o usuário comum <codusu> pode ver — painel
    de configuração do admin. Body: {"colunas": ["aguardando-ligacao", ...]}
    ou {"colunas": null} pra remover a restrição (volta a ver todas).
    """
    body = request.get_json(force=True)
    colunas = body.get("colunas", None)
    if colunas is not None and not isinstance(colunas, list):
        return jsonify({"erro": "colunas deve ser uma lista ou null."}), 400
    try:
        usuario = definir_colunas_visiveis(codusu, colunas)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify(usuario)


# ═══════════════════════════════════════════════════════════════════════════════
# ATRIBUIÇÃO DE NF e NOTIFICAÇÕES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/nf/atribuicoes", methods=["GET"])
def nf_atribuicoes_listar():
    """Todas as atribuições vigentes, indexadas por chvnel — o frontend usa isso pra colorir os cards."""
    return jsonify(atribuicoes_ativas())


@app.route("/api/nf/atribuir", methods=["POST"])
@admin_required
def nf_atribuir():
    body   = request.get_json(force=True)
    chvnel = str(body.get("chvnel") or "").strip()
    numnfc = str(body.get("numnfc") or "").strip()
    codusu_destino = str(body.get("codusu") or "").strip()
    if not chvnel or not numnfc or not codusu_destino:
        return jsonify({"erro": "Informe chvnel, numnfc e codusu."}), 400
    if not buscar_usuario(codusu_destino):
        return jsonify({"erro": "Usuário destino não encontrado."}), 404
    atribuir_nf(chvnel, numnfc, codusu_destino, session["codusu"])
    return jsonify(atribuicao_atual(chvnel))


@app.route("/api/nf/desatribuir", methods=["POST"])
@admin_required
def nf_desatribuir():
    body   = request.get_json(force=True)
    chvnel = str(body.get("chvnel") or "").strip()
    if not chvnel:
        return jsonify({"erro": "Informe chvnel."}), 400
    desatribuir_nf(chvnel)
    return jsonify({"ok": True})


@app.route("/api/notificacoes", methods=["GET"])
def notificacoes_listar():
    codusu = session["codusu"]
    return jsonify({
        "nao_lidas": contar_notificacoes_nao_lidas(codusu),
        "itens":     listar_notificacoes(codusu),
    })


@app.route("/api/notificacoes/marcar-lidas", methods=["POST"])
def notificacoes_marcar_lidas():
    marcar_notificacoes_lidas(session["codusu"])
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# CRM — ranking de conferências de integração confirmadas, por usuário (só admin)
# ═══════════════════════════════════════════════════════════════════════════════

# ACAO_CONFERENCIA_INTEGRACAO está definida lá em cima, junto das rotas de
# /api/oc/conferencia-integracao. O log (nf_log_acoes.detalhe) grava
# "Confirmado por {codusu} — {resultado}" como texto livre — não existe
# coluna estruturada pro codusu, então extrai daqui via regex pra montar o
# ranking. Formato gravado sempre nesse padrão (ver oc_conferencia_integracao_confirmar).
_RE_CONFIRMADO_POR = re.compile(r"^Confirmado por (\S+)")


def _desde_periodo(periodo: str) -> str | None:
    from datetime import datetime, timedelta
    agora = datetime.now()
    if periodo == "dia":
        desde = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    elif periodo == "semana":
        desde = agora - timedelta(days=7)
    elif periodo == "mes":
        desde = agora - timedelta(days=30)
    else:
        return None  # "total" (ou qualquer outro valor) — sem filtro
    return desde.strftime("%Y-%m-%dT%H:%M:%S")

@app.route("/crm")
def crm_page():
    # A página carrega sem sessão (igual "/"); o JS dela chama /api/auth/me
    # e manda de volta pra "/" se não for admin. O endpoint de dados abaixo
    # é @admin_required, então os números ficam protegidos de qualquer forma.
    return render_template("crm.html")


@app.route("/api/crm/desempenho", methods=["GET"])
@admin_required
def crm_desempenho():
    desde = _desde_periodo(request.args.get("periodo", "total"))
    return jsonify(desempenho_usuarios(desde))

@app.route("/api/crm/estatisticas", methods=["GET"])
@admin_required
def crm_estatisticas():
    """
    Ranking de quem mais confirmou conferência de integração (saiu de
    AGUARDANDO_INTEGRACAO de verdade) dentro do período pedido — controle da
    Jéssica de quem tratou mais NFs. Uma NF confirmada mais de uma vez pelo
    mesmo usuário (reabrindo o modal antes do Sapiens importar) conta só 1
    vez pra esse usuário, pelo mesmo motivo do "resolvidas" antigo.
    """
    periodo = request.args.get("periodo", "total")
    desde = _desde_periodo(periodo)

    conn = get_local_connection()
    try:
        if desde:
            rows = conn.execute("""
                SELECT numnfc, detalhe, criado_em FROM nf_log_acoes
                WHERE acao = ? AND criado_em >= ?
                ORDER BY criado_em DESC
            """, (ACAO_CONFERENCIA_INTEGRACAO, desde)).fetchall()
        else:
            rows = conn.execute("""
                SELECT numnfc, detalhe, criado_em FROM nf_log_acoes
                WHERE acao = ?
                ORDER BY criado_em DESC
            """, (ACAO_CONFERENCIA_INTEGRACAO,)).fetchall()

        vistos = set()          # (codusu, numnfc) já contado
        por_usuario = {}
        numnfcs_distintos = set()
        for r in rows:
            m = _RE_CONFIRMADO_POR.match(r["detalhe"] or "")
            codusu = m.group(1) if m else "desconhecido"
            numnfcs_distintos.add(r["numnfc"])

            chave = (codusu, r["numnfc"])
            if chave in vistos:
                continue
            vistos.add(chave)

            # Linhas vêm em ORDER BY criado_em DESC — a primeira ocorrência
            # de cada usuário já é a confirmação mais recente dele.
            info = por_usuario.setdefault(codusu, {"total": 0, "ultima": r["criado_em"], "numeros": []})
            info["total"] += 1
            info["numeros"].append(r["numnfc"])

        usuarios_map = {u["codusu"]: u for u in listar_usuarios()}
        ranking = [
            {
                "codusu":             codusu,
                "nomusu":             (usuarios_map.get(codusu) or {}).get("nomusu", codusu),
                "cor":                (usuarios_map.get(codusu) or {}).get("cor") or "#94a3b8",
                "total":              info["total"],
                "ultima_confirmacao": info["ultima"],
                "numeros":            info["numeros"],
            }
            for codusu, info in por_usuario.items()
        ]
        ranking.sort(key=lambda x: (-x["total"], x["nomusu"]))

        return jsonify({
            "periodo":            periodo,
            "total_confirmadas":  len(numnfcs_distintos),
            "ranking":            ranking,
        })
    finally:
        conn.close()

# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False, threaded=False)