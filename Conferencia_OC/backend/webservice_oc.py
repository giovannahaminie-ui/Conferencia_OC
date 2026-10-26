"""
webservice_oc.py
─────────────────────────────────────────────────────────────────────────────
Integração com o webservice SOAP do Senior G5 — GravarOrdensCompra_8
(com.senior.g5.co.mcm.cpr.ordemcompra) — geração e cancelamento de Ordem de
Compra no Sapiens.

Credenciais em backend/.env (nunca hardcoded no código):
    WS_OC_URL, WS_OC_USER, WS_OC_PASSWORD, WS_OC_IDENTIFICADOR

Regras:
  • Cancelamento de QUANTIDADE de item é sempre por item (numOcp existente +
    seqIpo + qtdCan) — nunca fecha/cancela a OC inteira (fechaOC nunca é
    setado).
  • tipoProcessamento: 1=Inclusão, 2=Alteração, 3=Cancelamento (da ORDEM
    inteira), 4=Reabilitação. Confirmado contra a doc oficial da Senior
    (GravarOrdensCompra): para cancelar/reduzir a QUANTIDADE de um item sem
    mexer na ordem, o valor correto é 2 (Alteração), não 3 — 3 cancela a
    ordem. Isso é o que `cancelar_item_oc_via_webservice` usa.
  • A partir da v7, toda vez que a operação é uma Alteração (2) e envia
    qtdCan, a quantidade PEDIDA do item (qtdPed) também tem que ser enviada
    junto — sem isso o Senior rejeita ou ignora o cancelamento.
  • Fora do código: a tela F099UCP (Parâmetros de Usuário p/ Compras e
    Recebimento) precisa ter "Alterar Quantidade OC/NF = S" pro usuário do
    webservice — sem isso, o webservice responde sucesso (tipRet=1) mas NÃO
    cancela a quantidade de verdade. Não tem como detectar isso pela resposta
    do webservice; é pré-requisito de ambiente.
  • Não existe campo de "OC já aprovada" no webservice — gerar_oc_aprovada e
    gerar_oc_para_aprovacao fazem a MESMA Inclusão (tipoProcessamento=1); a
    diferença é só o obsOcp e o que o fluxo faz depois de cada uma.
  • preUni vem de E000IPC.PREUNI (preço da própria NF).
  • cplIpo (complemento do item, E420IPO.CPLIPO até 250 chars) é obrigatório
    — confirmado num teste real: sem ele a Sapiens recusa com tipRet=2 e
    "É necessário informar complemento". Preenchido com a descrição do
    produto da NF (despro), com fallback pro código.
  • Formato de número (_num): inteiro sem ".0", decimal com ponto.
  • Timeout 300s (gravação no Senior pode demorar).
  • Resposta confirmada num teste real contra a _8: <result><dadosRetorno>
    <numOcp>/<retorno>/<tipRet></dadosRetorno><erroExecucao/><mensagemRetorno>
    <tipoRetorno></result> — mesmo formato documentado pra _5. `erro` no
    dict de retorno vem preenchido sempre que sucesso não é True (mesmo com
    <erroExecucao> vazio — a Sapiens costuma mandar o motivo em
    <retorno>/<mensagemRetorno> com tipRet≠1, não em erroExecucao).
  • Toda chamada grava o XML enviado (senha mascarada) e o recebido em
    backend/webservice_logs/ — auditoria/depuração sem precisar reproduzir
    a chamada.
─────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import requests
from datetime import datetime
from xml.sax.saxutils import escape as _xml_escape

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
_LOG_DIR  = os.path.join(os.path.dirname(__file__), "webservice_logs")

# Timeouts por tipo de chamada (segundos). Eram 300s pra tudo — com ~17
# cancelamentos numa NF grande, um webservice pendurado segurava a NF por mais
# de uma hora (o scheduler não inicia outra rodada enquanto uma está rodando).
# Cancelamento é uma Alteração de 1 linha: 60s é folga de sobra. A Inclusão
# manda a OC inteira (dezenas/centenas de linhas numa chamada só) e grava mais
# devagar, por isso tem folga maior.
_TIMEOUT_CANCELAR = 60
_TIMEOUT_GERAR    = 180


# ─────────────────────────────────────────────────────────────────────────────
# Configuração (.env)
# ─────────────────────────────────────────────────────────────────────────────

def _carregar_config() -> dict:
    """
    Lê backend/.env (linhas KEY=VALUE, sem lib externa) e cai pra os.environ
    como fallback. A senha nunca é logada/impressa a partir daqui.

    """
    valores = {}
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                chave, valor = linha.split("=", 1)
                valores[chave.strip()] = valor.strip()

    def _get(chave, default=""):
        return valores.get(chave) or os.environ.get(chave) or default

    url_completa = _get("WS_OC_URL")
    return {
        "url":           url_completa.split("?")[0],  # remove "?wsdl" antes do POST
        "usuario":       _get("WS_OC_USER"),
        "senha":         _get("WS_OC_PASSWORD"),
        "identificador": _get("WS_OC_IDENTIFICADOR", "INTEGRADO"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Montagem do envelope
# ─────────────────────────────────────────────────────────────────────────────

def _num(valor) -> str:
    """Inteiro sai sem '.0' (100, não 100.0); decimal sai com ponto (257.5)."""
    valor = float(valor or 0)
    if valor == int(valor):
        return str(int(valor))
    return str(valor)


def _mascarar_senha(xml: str, senha: str) -> str:
    """Nunca expõe a senha em texto puro — usado em qualquer log/retorno do envelope."""
    if not senha:
        return xml
    return xml.replace(senha, "******")


def _montar_envelope(codemp, codfil, tipo_processamento, codfor=None, num_ocp=None,
                     produtos=None, obs_ocp="", obs_mot="", cod_mot="", prc_ocp=None) -> tuple[str, dict]:
    """
    Monta o envelope SOAP de GravarOrdensCompra_8 só com os campos que
    usamos — o resto do XSD é opcional (marcado "?" no XML completo que enviado)
    e fica de fora.

    `produtos`: lista de dicts, cada um com as chaves que fizerem sentido pro
    caso (codPro/qtdPed/preUni pra Inclusão; seqIpo/qtdCan pra Cancelamento).

    `prc_ocp`: grava E420OCP.PRCOCP — usado pra marcar toda OC criada por
    este sistema com um valor exclusivo (20), que não colide com nenhum dos
    valores já usados manualmente no Sapiens (1, 9, 11) — dá pra identificar
    depois quais OCs vieram da integração só com `WHERE PRCOCP = 20`.
    """
    cfg = _carregar_config()
    produtos = produtos or []

    partes_produtos = []
    for p in produtos:
        campos = []
        if "codPro" in p:
            campos.append(f"<codPro>{_xml_escape(str(p['codPro']))}</codPro>")
        if "qtdPed" in p:
            campos.append(f"<qtdPed>{_num(p['qtdPed'])}</qtdPed>")
        if "preUni" in p:
            campos.append(f"<preUni>{_num(p['preUni'])}</preUni>")
        if "cplIpo" in p:
            campos.append(f"<cplIpo>{_xml_escape(str(p['cplIpo']))}</cplIpo>")
        if "seqIpo" in p:
            campos.append(f"<seqIpo>{p['seqIpo']}</seqIpo>")
        if "qtdCan" in p:
            campos.append(f"<qtdCan>{_num(p['qtdCan'])}</qtdCan>")
        partes_produtos.append("<produtos>" + "".join(campos) + "</produtos>")

    dados_gerais = [
        f"<codEmp>{codemp}</codEmp>",
        f"<codFil>{codfil}</codFil>",
    ]
    if codfor is not None:
        dados_gerais.append(f"<codFor>{codfor}</codFor>")
    if num_ocp is not None:
        dados_gerais.append(f"<numOcp>{num_ocp}</numOcp>")
    if obs_ocp:
        dados_gerais.append(f"<obsOcp>{_xml_escape(obs_ocp)}</obsOcp>")
    if cod_mot:
        dados_gerais.append(f"<codMot>{_xml_escape(str(cod_mot))}</codMot>")
    if obs_mot:
        dados_gerais.append(f"<obsMot>{_xml_escape(obs_mot)}</obsMot>")
    if prc_ocp is not None:
        dados_gerais.append(f"<prcOcp>{prc_ocp}</prcOcp>")
    dados_gerais.extend(partes_produtos)
    dados_gerais.append(f"<tipoProcessamento>{tipo_processamento}</tipoProcessamento>")

    xml = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ser="http://services.senior.com.br">'
        "<soapenv:Header/>"
        "<soapenv:Body>"
        "<ser:GravarOrdensCompra_8>"
        f"<user>{_xml_escape(cfg['usuario'])}</user>"
        f"<password>{_xml_escape(cfg['senha'])}</password>"
        "<encryption>0</encryption>"
        "<parameters>"
        f"<dadosGerais>{''.join(dados_gerais)}</dadosGerais>"
        f"<identificadorSistema>{cfg['identificador']}</identificadorSistema>"
        f"<tipoProcessamento>{tipo_processamento}</tipoProcessamento>"
        "</parameters>"
        "</ser:GravarOrdensCompra_8>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )
    return xml, cfg


# ─────────────────────────────────────────────────────────────────────────────
# Chamada HTTP + parse da resposta
# ─────────────────────────────────────────────────────────────────────────────

def _gravar_log_arquivo(envelope_mascarado: str, conteudo: str) -> None:
    """
    Grava request+response de cada chamada em backend/webservice_logs/, um
    arquivo por chamada (timestamp no nome) — pra auditoria/depuração sem
    precisar reproduzir a chamada real. Nunca derruba a chamada de verdade
    se der erro ao gravar (ex: pasta sem permissão) — só ignora.

    O arquivo é um XML VÁLIDO de verdade: envelope e resposta crus (que já
    trazem sua própria declaração <?xml ...?> e namespaces) vão dentro de
    CDATA, não colados soltos no meio do arquivo — <?xml ...?> só é permitido
    no comecinho de um documento, então colar dois XMLs completos um atrás do
    outro (como era antes) produz um arquivo inválido que navegador nenhum
    abre.
    """
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        nome = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".xml"
        doc = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<log>\n"
            f"  <enviado><![CDATA[{envelope_mascarado}]]></enviado>\n"
            f"  <recebido><![CDATA[{conteudo}]]></recebido>\n"
            "</log>\n"
        )
        with open(os.path.join(_LOG_DIR, nome), "w", encoding="utf-8") as f:
            f.write(doc)
    except Exception:
        pass


def _chamar(xml: str, cfg: dict, timeout: int = _TIMEOUT_GERAR) -> str:
    """
    POST do envelope no endpoint (sem '?wsdl'). `timeout` em segundos (ver
    _TIMEOUT_CANCELAR/_TIMEOUT_GERAR). Grava request/response em backend/webservice_logs/
    (senha sempre mascarada), inclusive quando a chamada falha antes de ter
    resposta (erro de rede/timeout).
    """
    envelope_log = _mascarar_senha(xml, cfg["senha"])
    try:
        resp = requests.post(
            cfg["url"],
            data=xml.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:
        _gravar_log_arquivo(envelope_log, f"[ERRO NA CHAMADA — sem resposta] {e}")
        raise
    _gravar_log_arquivo(envelope_log, resp.text)
    return resp.text


def _parse_resposta(xml_resposta: str) -> dict:
    """
    Extrai numOcp / tipRet / erroExecucao / mensagemRetorno da resposta.

    Baseado no formato confirmado do GravarOrdensCompra_5 (documento RTL):
        <result><dadosRetorno><numOcp>.../<retorno>.../<tipRet>...
        </dadosRetorno><erroExecucao xsi:nil="true"/><mensagemRetorno>...
        <tipoRetorno>...</result>

    Pra Cancelamento/Reabilitação (tipoProcessamento 3/4),os campos de dado novo
    (numOcp) costumam vir vazios/repetidos — o veredito de sucesso aqui NÃO
    depende de numOcp estar presente, só de tipRet/tipoRetorno=1 sem erro.
    """
    def _extrair(tag, texto):
        m = re.search(fr"<{tag}[^>]*>(.*?)</{tag}>", texto, re.DOTALL)
        return m.group(1).strip() if m else None

    numero_oc = _extrair("numOcp", xml_resposta)
    tip_ret = _extrair("tipRet", xml_resposta) or _extrair("tipoRetorno", xml_resposta)

    erro_bloco = re.search(
        r"<erroExecucao[^>]*>(.*?)</erroExecucao>|<erroExecucao[^/]*/>",
        xml_resposta, re.DOTALL,
    )
    tem_erro = bool(
        erro_bloco
        and erro_bloco.group(1)
        and erro_bloco.group(1).strip()
        and 'nil="true"' not in erro_bloco.group(0)
    )
    # <retorno> (dentro de dadosRetorno) costuma vir mais específico (ex: "É
    # necessário informar complemento") que <mensagemRetorno> (mais genérico,
    # ex: "Ocorreram erros.") — prioriza o mais útil pra depuração.
    mensagem = _extrair("retorno", xml_resposta) or _extrair("mensagemRetorno", xml_resposta) or ""

    sucesso = None
    if tem_erro:
        sucesso = False
    elif tip_ret == "1":
        sucesso = True
    elif tip_ret and tip_ret != "1":
        sucesso = False
    elif numero_oc and numero_oc.isdigit() and int(numero_oc) > 0:
        sucesso = True

    # erro: sempre que NÃO deu sucesso, usa a mensagem disponível — o Sapiens
    # às vezes manda o motivo em <retorno>/<mensagemRetorno> com
    # <erroExecucao> vazio (nil="true"), ex: tipRet=2 + "É necessário
    # informar complemento". Não dá pra depender só de erroExecucao ter
    # conteúdo, senão o erro real fica escondido (sucesso=False, erro=None).
    erro = mensagem if sucesso is not True and mensagem else None

    return {
        "sucesso":   sucesso,
        "numero_oc": numero_oc,
        "erro":      erro,
        "mensagem":  mensagem,
        "raw":       xml_resposta,
    }

# ─────────────────────────────────────────────────────────────────────────────
# As 3 funções que oc_service.py reexporta pro resto do fluxo (nf_processor.py).
# ─────────────────────────────────────────────────────────────────────────────

def gerar_oc_aprovada_via_webservice(codemp, codfil, codfor, produtos, observacao="") -> dict:
    """
    Caso Juntar_OC — Inclusão (tipoProcessamento=1) de uma OC nova
    consolidando a quantidade que já estava aprovada em várias OCs de
    origem (essas são canceladas à parte, por cancelar_item_oc_via_webservice).

    `produtos`: lista de dicts vindos de nf_processor.py (produtos_apr), cada
    um com seqipc/codpro/profor/despro/preuni/qtd.
    """
    itens = [{
        # CODPRO da NF costuma vir só com espaço em branco (' '), não vazio
        # de verdade — sem o .strip(), "' ' or profor" prende no espaço (é
        # truthy em Python) e o PROFOR nunca é usado como fallback.
        "codPro": str(p.get("codpro") or "").strip() or str(p.get("profor") or "").strip(),
        "qtdPed": p["qtd"],
        "preUni": p.get("preuni") or 0,
        # E420IPO.CPLIPO tem 250 caracteres — a NF quase nunca chega perto.
        "cplIpo": (str(p.get("despro") or "").strip()
                  or str(p.get("codpro") or "").strip()
                  or str(p.get("profor") or "").strip())[:250],
    } for p in produtos]

    xml, cfg = _montar_envelope(
        codemp=codemp, codfil=codfil, codfor=codfor,
        tipo_processamento=1, produtos=itens, obs_ocp=observacao, prc_ocp=20,
    )
    try:
        resp = _chamar(xml, cfg)
    except Exception as e:
        return {
            "sucesso": False, "numero_oc": None, "erro": str(e), "mensagem": str(e),
            "envelope": _mascarar_senha(xml, cfg["senha"]),
        }
    resultado = _parse_resposta(resp)
    resultado["envelope"] = _mascarar_senha(xml, cfg["senha"])
    return resultado


def gerar_oc_para_aprovacao(codemp, codfil, codfor, produtos, observacao="") -> dict:
    """
    Caso OC_Pendente — mesma Inclusão (tipoProcessamento=1) da função acima;
    não existe campo de "aprovação" no webservice, então a diferença fica só
    no obsOcp e no que o fluxo faz depois (essa aqui ainda espera o Junior
    aprovar manualmente no Sapiens antes de a NF poder integrar).

    `produtos`: lista de dicts vindos de nf_processor.py (produtos_pen), cada
    um com seqipc/codpro/profor/despro/preuni/qtd/qtd_oc/falta. `qtd` é o
    QtdRec TOTAL do item (o que vai como `qtdPed` nessa OC nova — inclui o
    que já tinha de saldo + a diferença nova; a origem é cancelada à parte,
    por cancelar_item_oc_via_webservice); `qtd_oc`/`falta` ficam só pra
    exibição/WhatsApp ("precisa X, tem Y, faltam Z").
    """
    itens = [{
        # CODPRO da NF costuma vir só com espaço em branco (' '), não vazio
        # de verdade — sem o .strip(), "' ' or profor" prende no espaço (é
        # truthy em Python) e o PROFOR nunca é usado como fallback.
        "codPro": str(p.get("codpro") or "").strip() or str(p.get("profor") or "").strip(),
        "qtdPed": p["qtd"],
        "preUni": p.get("preuni") or 0,
        "cplIpo": (str(p.get("despro") or "").strip()
                  or str(p.get("codpro") or "").strip()
                  or str(p.get("profor") or "").strip())[:250],
    } for p in produtos]

    xml, cfg = _montar_envelope(
        codemp=codemp, codfil=codfil, codfor=codfor,
        tipo_processamento=1, produtos=itens, obs_ocp=observacao, prc_ocp=20,
    )
    try:
        resp = _chamar(xml, cfg)
    except Exception as e:
        return {
            "sucesso": False, "numero_oc": None, "erro": str(e), "mensagem": str(e),
            "envelope": _mascarar_senha(xml, cfg["senha"]),
        }
    resultado = _parse_resposta(resp)
    resultado["envelope"] = _mascarar_senha(xml, cfg["senha"])
    return resultado


def cancelar_item_oc_via_webservice(codemp, codfil, numocp, seqipo, qtd, qtd_pedida) -> dict:
    """
    Alteração (tipoProcessamento=2) da QUANTIDADE CANCELADA de UM item
    (seqIpo) de uma OC EXISTENTE (numOcp) — nunca a OC inteira. `qtd` vira
    qtdCan. fechaOC nunca é setado.

    `qtd_pedida` (E420IPO.QTDORI — quantidade originalmente pedida do item)
    é obrigatório junto: confirmado na doc oficial da Senior que, a partir da
    v7, toda Alteração que envia qtdCan também precisa mandar qtdPed, senão o
    cancelamento não é efetivado (mesmo com resposta de sucesso).

    tipoProcessamento=3 cancelaria a ORDEM inteira — por isso não é usado
    aqui; ver regra no docstring do módulo.

    Pré-requisito de ambiente (fora deste código): tela F099UCP com "Alterar
    Quantidade OC/NF = S" pro usuário do webservice — sem isso, a chamada
    responde sucesso mas não altera nada de verdade.

    Usado tanto em Juntar_OC (cancela só o consumido, limitado ao QtdRec)
    quanto em OC_Pendente (cancela o saldo inteiro do item).
    """
    xml, cfg = _montar_envelope(
        codemp=codemp, codfil=codfil, num_ocp=numocp,
        tipo_processamento=2,
        produtos=[{"seqIpo": seqipo, "qtdPed": qtd_pedida, "qtdCan": qtd}],
        obs_mot="Conferencia NF x OC - cancelamento automatico",
    )
    try:
        resp = _chamar(xml, cfg, timeout=_TIMEOUT_CANCELAR)
    except Exception as e:
        return {
            "sucesso": False, "numocp": numocp, "seqipo": seqipo, "qtd": qtd,
            "cancelado": False, "erro": str(e), "envelope": _mascarar_senha(xml, cfg["senha"]),
        }
    resultado = _parse_resposta(resp)
    resultado.update({
        "numocp": numocp, "seqipo": seqipo, "qtd": qtd,
        "cancelado": bool(resultado.get("sucesso")),
    })
    resultado["envelope"] = _mascarar_senha(xml, cfg["senha"])
    return resultado




