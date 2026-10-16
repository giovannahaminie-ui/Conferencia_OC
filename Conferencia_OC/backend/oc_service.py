"""
oc_service.py
─────────────────────────────────────────────────────────────────────────────
Toda a lógica de Ordens de Compra (E420OCP / E420IPO) para o fluxo de
conferência de NF de entrada.

Regras de negócio:
  • OC "disponível"  → SITOCP IN (1, 2)   (aberto total / aberto parcial)
  • OC "aprovada"    → SITAPR = 'APR'
  • OC "cancelada"   → SITOCP = 5
  • Item "em aberto" → QTDABE > 0
─────────────────────────────────────────────────────────────────────────────
"""

from db_oracle import get_connection, rows_to_dict


# ─────────────────────────────────────────────────────────────────────────────
# 0. PRÉ-CHECK — portfólio do fornecedor (E403FPR)
# ─────────────────────────────────────────────────────────────────────────────

def verificar_portfolio_fornecedor(codemp: str, codfil: str, codfor: str, chvnel: str) -> list[dict]:
    """
    Retorna os itens da NF cujo PROFOR/CODPRO não está cadastrado no
    portfólio deste fornecedor (E403FPR). Lista vazia = tudo cadastrado.
    """
    sql = """
        SELECT i.SEQIPC, i.CODPRO, i.PROFOR, i.CPLIPC
        FROM sapiens.E000IPC i
        WHERE i.CHVNEL = :chvnel
          AND NOT EXISTS (
              SELECT 1 FROM sapiens.E403FPR p
              WHERE p.CODEMP = :codemp
                AND p.CODFOR = :codfor
                AND p.PROFOR = i.PROFOR)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfor=codfor, chvnel=chvnel)
        rows = rows_to_dict(cur)
    finally:
        conn.close()

    return [
        {
            "seqipc": row["SEQIPC"],
            "codpro": row["CODPRO"],
            "profor": row["PROFOR"],
            "despro": row["CPLIPC"],
        }
        for row in rows
    ]


def codpro_do_portfolio(codemp: str, codfor: str, profor: str) -> str | None:
    """
    Busca o CODPRO real (código interno do Sapiens) ligado a esse PROFOR no
    portfólio do fornecedor (E403FPR). Necessário pro webservice de OC: o
    campo `codPro` do GravarOrdensCompra_8 exige o código interno do Sapiens
    — mandar o PROFOR (código do fornecedor) direto dá "Produto não
    encontrado", mesmo quando a NF não tem CODPRO preenchido (E000IPC.CODPRO
    em branco, só PROFOR). Retorna None se não achar o vínculo.
    """
    if not profor:
        return None
    sql = """
        SELECT CODPRO FROM sapiens.E403FPR
        WHERE CODEMP = :codemp AND CODFOR = :codfor AND PROFOR = :profor
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfor=codfor, profor=profor)
        row = cur.fetchone()
        return str(row[0]).strip() if row and row[0] else None
    finally:
        conn.close()


def verificar_derivacao_deposito(codemp: str, codfor: str, chvnel: str) -> list[dict]:
    """
    Segundo pré-check da Verificação Cadastral, depois do portfólio: pra cada
    item da NF que JÁ tem ligação no portfólio do fornecedor (E403FPR — o
    JOIN abaixo garante isso; item sem ligação nem entra aqui, já foi pego
    antes por verificar_portfolio_fornecedor), confere se o produto (CODPRO
    resolvido via portfólio) tem:
      • derivação cadastrada (E075DER)
      • vínculo de depósito/estoque (E210EST)
    Sem qualquer um dos dois, a geração/cancelamento de OC no Sapiens tende a
    falhar mais na frente — pegar aqui, na Verificação Cadastral, evita
    descobrir isso só quando o webservice rejeitar.

    Lista vazia = todos os produtos com derivação e vínculo de depósito/estoque.
    """
    sql = """
        SELECT DISTINCT i.SEQIPC, i.PROFOR, i.CPLIPC, f.CODPRO,
            CASE WHEN NOT EXISTS (
                SELECT 1 FROM sapiens.E075DER D
                WHERE D.CODEMP = :codemp AND D.CODPRO = f.CODPRO
            ) THEN 1 ELSE 0 END AS SEM_DERIVACAO,
            CASE WHEN NOT EXISTS (
                SELECT 1 FROM sapiens.E210EST E
                WHERE E.CODEMP = :codemp AND E.CODPRO = f.CODPRO
            ) THEN 1 ELSE 0 END AS SEM_DEPOSITO
        FROM sapiens.E000IPC i
        JOIN sapiens.E403FPR f ON f.CODEMP = :codemp AND f.CODFOR = :codfor AND f.PROFOR = i.PROFOR
        WHERE i.CHVNEL = :chvnel
          AND (
              NOT EXISTS (SELECT 1 FROM sapiens.E075DER D WHERE D.CODEMP = :codemp AND D.CODPRO = f.CODPRO)
              OR NOT EXISTS (SELECT 1 FROM sapiens.E210EST E WHERE E.CODEMP = :codemp AND E.CODPRO = f.CODPRO)
          )
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfor=codfor, chvnel=chvnel)
        rows = rows_to_dict(cur)
    finally:
        conn.close()

    return [
        {
            "seqipc": row["SEQIPC"],
            "codpro": row["CODPRO"],
            "profor": row["PROFOR"],
            "despro": row["CPLIPC"],
            "sem_derivacao": bool(row["SEM_DERIVACAO"]),
            "sem_deposito": bool(row["SEM_DEPOSITO"]),
        }
        for row in rows
    ]


# Usada pelo modal manual (/api/oc/conferir-nf) — mesma lógica item a item
# da Etapa 3 (buscar_item_nf + _listar_ocs_candidatas, considerando reserva
# via USU_CODANA), só que sem gravar nada: é só consulta, para exibição.
def conferir_nf_vs_portfolio(
    codemp: str,
    codfil: str,
    codfor: str,
    chvnel: str,
    cgcfor: str,
    cgcfil: str,
) -> dict:

    fora_portfolio = verificar_portfolio_fornecedor(codemp, codfil, codfor, chvnel)
    # usado abaixo pra marcar, item a item, se a causa da divergência é
    # "fora do portfólio" (nem cadastrado) ou "sem OC" (cadastrado mas sem OC cobrindo).
    profors_fora_portfolio = {str(it["profor"] or "") for it in fora_portfolio}

    seqipcs = listar_seqipc_nf(chvnel)

    itens_resultado = []
    itens_nao_encontrados = []
    numocps_relevantes: set[str] = set()
    # Quais SEQIPO de cada OC a NF de fato usa — {"24706": {4, 33}}. Uma OC
    # pode ter dezenas de sequências e a NF consumir só duas; sem isso o
    # bloco "OCs Abertas do Fornecedor" abre a OC inteira. Preenchido em
    # paralelo ao numocps_relevantes, nos mesmos dois pontos.
    seqipos_relevantes: dict[str, set] = {}
    total_cobertos = 0
    for seqipc in seqipcs:
        item = buscar_item_nf(chvnel, seqipc)
        if not item:
            itens_nao_encontrados.append(seqipc)
            continue

        qtd_nf = item["qtd_recebida"]
        profor = str(item["profor"] or "")

        if profor in profors_fora_portfolio:
            # Sem ligação no portfólio (E403FPR) pra esse fornecedor —
            # decisão de negócio confirmada: NÃO busca nem mostra OC nenhuma
            # pra esse item, mesmo que o CODPRO já venha preenchido na NF e
            # exista uma OC real pro mesmo par fornecedor/produto. O
            # portfólio é o cadastro que autoriza a ligação; sem ele, a OC
            # não deve aparecer como cobertura. Achado num caso real (NF
            # 514): fornecedor sem NENHUM registro em E403FPR, mas com
            # CODPRO já preenchido igual ao de uma OC aberta de verdade — o
            # modal mostrava "coberto" mesmo sem o cadastro de portfólio
            # existir, porque a busca usava o CODPRO cru da NF direto.
            itens_resultado.append({
                "seqipc":            item["seqipc"],
                "codpro":            item["codpro"],
                "profor":            item["profor"],
                "despro":            item["cplipc"],
                "qtdinf":            qtd_nf,
                "qtd_oc_disponivel": 0,
                "coberto":           False,
                "fora_portfolio":    True,
                "ocs_cobertura":     [],
                "divergencia":       "Produto sem ligação no portfólio do fornecedor (E403FPR)",
            })
            continue

        # Resolvido cedo (antes da checagem de vínculo) porque também é
        # usado pra validar que o vínculo existente é do produto certo.
        codpro_ws = str(item["codpro"] or "").strip() or codpro_do_portfolio(codemp, codfor, item["profor"])

        if item["numocp"] and numocp_vinculo_valido(codemp, codfil, item["numocp"], item["seqipo"], item["codfor"], codpro_ws):
            # Item já vinculado a uma OC REAL (E000IPC.NUMOCP setado e a OC
            # existe de fato) — a reserva feita na época (USU_CODANA) é PRA
            # ESSE item, então reconsultar o saldo aqui subtrairia a própria
            # reserva dele e mostraria "sem OC" mesmo já estando coberto.
            # Trata como coberto direto, sem reconsultar saldo.
            # (Se o NUMOCP for lixo — OC inexistente, ou uma data como
            # 20260722 — cai fora deste if e é conferido do zero abaixo.)
            #
            # EXCEÇÃO: se a OC vinculada foi cancelada (SITOCP=5) ou liquidada
            # (SITOCP=4) DEPOIS do vínculo, ela não cobre mais nada — mostra
            # como divergência (e não como "coberto" com a qtd da NF ecoada),
            # mas ainda lista a OC no bloco de baixo pra ficar visível o que
            # aconteceu.
            sitocp_vinc = oc_situacao(codemp, codfil, item["numocp"])[0]
            if sitocp_vinc in ("4", "5"):
                numocps_relevantes.add(str(item["numocp"]))
                if item["seqipo"] is not None:
                    seqipos_relevantes.setdefault(str(item["numocp"]), set()).add(item["seqipo"])
                itens_resultado.append({
                    "seqipc":            item["seqipc"],
                    "codpro":            item["codpro"],
                    "profor":            item["profor"],
                    "despro":            item["cplipc"],
                    "qtdinf":            qtd_nf,
                    "qtd_oc_disponivel": 0,
                    "coberto":           False,
                    "fora_portfolio":    False,
                    "ocs_cobertura":     [],
                    "divergencia":       f"OC {item['numocp']} vinculada está {descricao_sitocp(sitocp_vinc)}",
                })
                continue
            total_cobertos += 1
            numocps_relevantes.add(str(item["numocp"]))
            if item["seqipo"] is not None:
                seqipos_relevantes.setdefault(str(item["numocp"]), set()).add(item["seqipo"])
            itens_resultado.append({
                "seqipc":            item["seqipc"],
                "codpro":            item["codpro"],
                "profor":            item["profor"],
                "despro":            item["cplipc"],
                "qtdinf":            qtd_nf,
                "qtd_oc_disponivel": qtd_nf,
                "coberto":           True,
                "fora_portfolio":    False,  # chegou aqui só se profor JÁ está no portfólio (trava acima)
                "ocs_cobertura":     [{"numocp": item["numocp"], "seqipo": item["seqipo"], "codpro": item["codpro"]}],
                "divergencia":       None,
            })
            continue

        # Busca só por CODPRO (decisão de negócio confirmada — nunca cai pro
        # PROFOR) — codpro_ws já resolvido acima.
        candidatas = _listar_ocs_candidatas(
            item["codemp"], item["codfil"], item["codfor"], codpro_ws,
        )
        # ALINHADO ao fluxo automático (contar_e_somar_ocs): SOMA o saldo de
        # TODAS as OCs candidatas em vez de olhar só a melhor OC individual.
        # Assim o item é "coberto" quando a soma das OCs cobre (consolidação
        # via Juntar_OC), e a tela mostra TODAS as OCs que compõem a cobertura
        # — não só a maior.
        com_saldo = [c for c in candidatas if c["disponivel"] > 0]
        qtd_oc_disponivel = sum(c["disponivel"] for c in com_saldo)
        coberto = qtd_oc_disponivel >= qtd_nf and qtd_nf > 0
        consolidada = coberto and len(com_saldo) > 1  # precisou de mais de uma OC
        if coberto:
            total_cobertos += 1

        ocs_cobertura = [
            {"numocp": c["numocp"], "seqipo": c["seqipo"], "codpro": c["codpro"],
             "disponivel": c["disponivel"]}
            for c in com_saldo
        ]
        for c in com_saldo:
            numocps_relevantes.add(str(c["numocp"]))
            if c["seqipo"] is not None:
                seqipos_relevantes.setdefault(str(c["numocp"]), set()).add(c["seqipo"])

        itens_resultado.append({
            "seqipc":            item["seqipc"],
            "codpro":            item["codpro"],
            "profor":            item["profor"],
            "despro":            item["cplipc"],
            "qtdinf":            qtd_nf,
            "qtd_oc_disponivel": qtd_oc_disponivel,
            "coberto":           coberto,
            "consolidada":       consolidada,
            "num_ocs":           len(com_saldo),
            "fora_portfolio":    False,  # chegou aqui só se profor JÁ está no portfólio (trava acima)
            "ocs_cobertura":     ocs_cobertura,
            "divergencia":       None if coberto else f"NF={qtd_nf} / OC={qtd_oc_disponivel}",
        })

    conn2 = get_connection()
    try:
        ocs_abertas = []
        for numocp in sorted(numocps_relevantes):
            # Passa as sequências que esta NF usa nessa OC, pra exibir só elas
            # (ver seqipos_relevantes acima).
            ocs_abertas.extend(_buscar_oc_por_numocp_conn(
                conn2, codemp, codfil, numocp, seqipos=seqipos_relevantes.get(numocp)))
    finally:
        conn2.close()

    # só fica True quando TODOS os itens da NF (seqipcs) foram
    # de fato carregados e conferidos contra as OCs. Se a NF não trouxe
    # nenhum item (seqipcs vazio) ou algum item não foi encontrado na
    # E000IPC (join falhou), não dá pra afirmar "sem OC" — é dado
    # incompleto, uma condição diferente que o modal precisa distinguir.
    confirmado = bool(seqipcs) and not itens_nao_encontrados

    # DIAGNÓSTICO: se ainda assim não achou nenhuma OC pro item (nem por
    # CODPRO nem por PROFOR), busca TODAS as OCs abertas desse fornecedor
    # (sem filtrar produto) só pra mostrar na tela — ajuda a enxergar se a
    # OC criada manualmente existe mas está com empresa/filial diferente,
    # ou com produto que realmente não bate com o item da NF.
    ocs_debug_fornecedor = []
    if not numocps_relevantes and itens_resultado:
        try:
            ocs_debug_fornecedor = buscar_ocs_abertas(codemp, codfil, codfor)
        except Exception as e:
            print(f"  ⚠️  [conferir_nf_vs_portfolio] erro ao buscar OCs de diagnóstico: {e}")

    return {
        "tem_oc":               bool(numocps_relevantes),
        "itens_ok":             total_cobertos == len(itens_resultado) if itens_resultado else False,
        "itens_nf":             itens_resultado,
        "ocs_abertas":          ocs_abertas,
        "fora_portfolio":       fora_portfolio,
        "total_nf":             len(itens_resultado),
        "total_cobertos":       total_cobertos,
        "confirmado":           confirmado,
        "itens_nao_encontrados": itens_nao_encontrados,
        "ocs_debug_fornecedor": ocs_debug_fornecedor,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Conferência item a item (chvnel + seqipc), com reserva de saldo de OC
# via USU_CODANA (E420IPO). 
#   - A reserva é ACUMULADA: cada novo item que usa aquela OC/sequência soma
#     em cima do que já tinha, nunca sobrescreve.
#   - Se o item já vem com NUMOCP/SEQIPO informado na própria NF, valida
#     ESSA OC específica primeiro (fornecedor, situação, produto, saldo já
#     descontando reserva); só cai pra busca ampla se essa não for válida.
#   - quando a NF for marcada como pronta pra importação (e o QTDABE real do
#     Sapiens já tiver sido abatido), a reserva feita deve ser descontada de
#     volta via liberar_reserva_item_oc() — ainda NÃO está conectada em
#     nf_processor.py.
#
# AINDA NÃO CONECTADO ao fluxo automático (processar_nf) — falta decidir como
# o status da NF inteira se comporta quando a validação é item a item (ela
# pode ficar com uns itens cobertos e outros não).
# ─────────────────────────────────────────────────────────────────────────────

def listar_seqipc_nf(chvnel: str) -> list:
    """Retorna a lista de SEQIPC (itens) de uma NF, pra processar item a item."""
    sql = "SELECT SEQIPC FROM sapiens.E000IPC WHERE CHVNEL = :chvnel ORDER BY SEQIPC"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, chvnel=chvnel)
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def buscar_item_nf(chvnel: str, seqipc) -> dict | None:
    """
    Busca um item específico da NF (chvnel + seqipc), com empresa/filial/
    fornecedor derivados via E070FIL/E095FOR, e o NUMOCP/SEQIPO se a NF já
    veio com OC informada pra esse item.
    """
    sql = """
        SELECT
            e.codemp, e.codfil, f.codfor,
            n.seqipc, n.codpro, n.profor, n.cplipc,
            n.qtdrec AS qtd_recebida,
            n.preuni,
            n.numocp, n.seqipo
        FROM sapiens.e000ipc n
        JOIN sapiens.e070fil e ON e.numcgc = n.cgcfil
        JOIN sapiens.e095for f ON f.cgccpf = n.cgcfor
        WHERE n.chvnel = :chvnel
          AND n.seqipc = :seqipc
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, chvnel=chvnel, seqipc=seqipc)
        rows = rows_to_dict(cur)
    finally:
        conn.close()

    if not rows:
        return None
    row = rows[0]
    return {
        "codemp":       row["CODEMP"],
        "codfil":       row["CODFIL"],
        "codfor":       row["CODFOR"],
        "seqipc":       row["SEQIPC"],
        "codpro":       row["CODPRO"],
        "profor":       row["PROFOR"],
        "cplipc":       row["CPLIPC"],
        "qtd_recebida": float(row["QTD_RECEBIDA"] or 0),
        "preuni":       float(row["PREUNI"] or 0),
        "numocp":       row["NUMOCP"],
        "seqipo":       row["SEQIPO"],
    }


def _qtd_reservada(codemp, codfil, numocp, seqipo) -> float:
    """Lê a quantidade já reservada (USU_CODANA) pra um item de OC."""
    sql = """
        SELECT USU_CODANA FROM sapiens.E420IPO
        WHERE CODEMP = :codemp 
          AND CODFIL = :codfil
          AND NUMOCP = :numocp 
          AND SEQIPO = :seqipo
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfil=codfil, numocp=numocp, seqipo=seqipo)
        row = cur.fetchone()
    finally:
        conn.close()
    return float(row[0]) if row and row[0] is not None else 0.0


def reservar_item_oc(codemp, codfil, numocp, seqipo, qtd: float) -> None:
    """Soma `qtd` à reserva acumulada (USU_CODANA) desse item de OC."""
    nova = _qtd_reservada(codemp, codfil, numocp, seqipo) + qtd
    sql = """
        UPDATE sapiens.E420IPO
           SET USU_CODANA = :nova
         WHERE CODEMP = :codemp AND CODFIL = :codfil
           AND NUMOCP = :numocp AND SEQIPO = :seqipo
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, nova=nova, codemp=codemp, codfil=codfil, numocp=numocp, seqipo=seqipo)
        conn.commit()
    finally:
        conn.close()


def liberar_reserva_item_oc(codemp, codfil, numocp, seqipo, qtd: float) -> None:
    """
    Desconta `qtd` da reserva acumulada (USU_CODANA) — chamar quando a NF que
    fez essa reserva for confirmada como importada de verdade no Sapiens
    (QTDABE real já abatido), pra não ficar contando a mesma baixa duas vezes.
    """
    nova = max(0.0, _qtd_reservada(codemp, codfil, numocp, seqipo) - qtd)
    sql = """
        UPDATE sapiens.E420IPO
           SET USU_CODANA = :nova
         WHERE CODEMP = :codemp AND CODFIL = :codfil
           AND NUMOCP = :numocp AND SEQIPO = :seqipo

    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, nova=nova, codemp=codemp, codfil=codfil, numocp=numocp, seqipo=seqipo)
        conn.commit()
    finally:
        conn.close()

def _listar_ocs_candidatas(codemp, codfil, codfor, codpro, numocp=None, seqipo=None, reservas_locais=None) -> list[dict]:
    """
    Lista as OCs abertas candidatas pra esse produto, com saldo já
    descontando a reserva provisória (USU_CODANA), ordenadas por data
    (mais antiga primeiro). Não filtra por quantidade necessária — quem
    chama decide o que fazer com cada candidata.

    `reservas_locais`, se informado, é o livro-caixa em memória (ver
    nf_processor._rodar_verificacao_oc): dict {(numocp, seqipo): quantidade}
    já "usada" por itens desta MESMA NF, já classificados nesta mesma
    rodada, mas ainda não gravada no Oracle. Descontado do `disponivel` de
    cada candidata — sem isso, dois itens da mesma NF disputando a mesma
    linha enxergariam o mesmo saldo cheio, sem saber um do outro.

    Casa SÓ pelo CODPRO (decisão de negócio confirmada — nunca cai pro
    PROFOR como alternativa, mesmo como valor adicional de busca). Quem
    chama é responsável por resolver o CODPRO real via portfólio
    (oc_service.codpro_do_portfolio) antes de chamar esta função, quando a
    NF vier sem CODPRO preenchido (comum — a NF quase sempre traz só o
    PROFOR, com CODPRO em branco). Sem CODPRO nenhum pra buscar, não busca
    nada — vira "sem candidata" (erro/pendente), em vez de arriscar casar
    pelo PROFOR sozinho.
    """
    codpro_str = str(codpro or "").strip()
    valores = [codpro_str] if codpro_str else []

    rows = []
    if valores:
        binds = {f"v{i}": v for i, v in enumerate(valores)}
        in_list = ", ".join(f":{k}" for k in binds)
        sql = f"""
            SELECT
                o.numocp,
                o.datemi,
                o.codfor,
                i.seqipo,
                i.codpro,
                i.qtdabe,
                i.qtdori,
                i.qtdcan
            FROM sapiens.e420ocp o
            JOIN sapiens.e420ipo i
              ON i.codemp = o.codemp
             AND i.codfil = o.codfil
             AND i.numocp = o.numocp
            WHERE o.codemp = :codemp
              AND o.codfil = :codfil
              AND o.codfor = :codfor
              AND o.sitocp IN (1, 2)
              AND i.CODPRO IN ({in_list}) 
              AND i.sitipo IN (1, 2)
              AND (:numocp IS NULL OR o.numocp = :numocp)
              AND (:seqipo IS NULL OR i.seqipo = :seqipo)
            ORDER BY o.datemi, o.numocp, i.seqipo
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, codemp=codemp, codfil=codfil, codfor=codfor,
                        numocp=numocp, seqipo=seqipo, **binds)
            rows = rows_to_dict(cur)
        finally:
            conn.close()

    if not rows:
        print(f"  ⚠️  [_listar_ocs_candidatas] nenhuma OC aberta casou codemp={codemp} codfil={codfil} codfor={codfor} codpro={codpro!r}")

    candidatas = []
    for row in rows:
        n_ocp      = row["NUMOCP"]
        s_ipo      = row["SEQIPO"]
        reservado  = _qtd_reservada(codemp, codfil, n_ocp, s_ipo)
        disponivel = float(row["QTDABE"] or 0) - reservado
        if reservas_locais:
            disponivel -= reservas_locais.get((n_ocp, s_ipo), 0)
        candidatas.append({
            "numocp": n_ocp, "seqipo": s_ipo, "codpro": row["CODPRO"],
            "qtdabe": float(row["QTDABE"] or 0), "reservado": reservado,
            "disponivel": disponivel,
            # QTDORI (quantidade originalmente pedida) — exigida como qtdPed
            # junto do qtdCan em cancelar_item_oc_via_webservice (Alteração).
            "qtdori": float(row["QTDORI"] or 0),
            # QTDCAN (quantidade JÁ cancelada antes de qualquer coisa que a
            # gente vá cancelar agora) — o Senior trata qtdCan como valor
            # ABSOLUTO (a quantidade cancelada TOTAL da linha), não um delta.
            # Confirmado num teste real: mandar só o que queríamos cancelar
            # AGORA (ex: 20) SOBRESCREVEU um QTDCAN anterior (12), ao invés
            # de somar — o saldo final ficou errado (12 em vez de 0). Quem
            # for cancelar mais dessa linha precisa somar este valor à
            # quantidade nova antes de mandar como qtdCan.
            "qtdcan": float(row["QTDCAN"] or 0),
        })
    return candidatas


def contar_e_somar_ocs(codemp, codfil, codfor, codpro, reservas_locais=None) -> dict:
    """
    Pra um produto: soma o saldo disponível de TODAS as linhas (SEQIPO) que
    cobrem ele (QtdOcp = Σ QTDABE − USU_CODANA), já descontando reserva
    provisória. `codpro` deve ser o CODPRO real já resolvido (ver
    _listar_ocs_candidatas — busca só por CODPRO, nunca cai pro PROFOR).
    Reaproveita _listar_ocs_candidatas (mesmo cálculo de disponível), então
    quem chama decide o cenário (OC_OK / Juntar_OC / OC_Pendente) só olhando
    os números devolvidos — sem repetir a lógica de saldo.

    `reservas_locais` é repassado direto pra _listar_ocs_candidatas (ver lá)
    — livro-caixa em memória dos itens desta NF já classificados nesta mesma
    rodada.

    `num_linhas` é o número de LINHAS (SEQIPO) com saldo — o critério certo
    pra saber se dá pra cobrir com uma linha só ou se precisa consolidar.
    `numocp` (OCs DISTINTAS) é mantido só como informação complementar pra
    log/diagnóstico (ex: "2 linhas na mesma OC" vs "2 linhas em 2 OCs") — não
    usar `numocp` como critério de decisão: uma OC só pode ter 2+ linhas que,
    juntas, cobrem o item sem nenhuma cobrir sozinha (o caso que
    `numocp==1` escondia antes).

    Retorna: {"numocp": N, "num_linhas": M, "qtdocp": soma, "candidatas": [...]}
    """
    candidatas = _listar_ocs_candidatas(codemp, codfil, codfor, codpro, reservas_locais=reservas_locais)
    # só conta OC que de fato tem saldo positivo disponível
    com_saldo = [c for c in candidatas if c["disponivel"] > 0]
    numocp = len({str(c["numocp"]) for c in com_saldo})
    qtdocp = sum(c["disponivel"] for c in com_saldo)
    return {"numocp": numocp, "num_linhas": len(com_saldo), "qtdocp": qtdocp, "candidatas": com_saldo}


def vincular_item_oc(chvnel: str, seqipc, numocp, seqipo) -> None:
    """
    ("OC tá ok") — grava NUMOCP/SEQIPO na E000IPC,
    vinculando esse item da NF à sequência da OC que cobriu ele. Update
    direto, sem webservice (só se aplica quando uma OC só já resolve o item).
    """
    sql = """
        UPDATE sapiens.E000IPC
           SET NUMOCP = :numocp, SEQIPO = :seqipo
         WHERE CHVNEL = :chvnel AND SEQIPC = :seqipc
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, numocp=numocp, seqipo=seqipo, chvnel=chvnel, seqipc=seqipc)
        conn.commit()
    finally:
        conn.close()


def limpar_vinculo_item_oc(chvnel: str, seqipc) -> None:
    """
    Apaga o vínculo de OC de um item da NF (E000IPC): zera NUMOCP/SEQIPO.

    Usada quando o NUMOCP gravado no item é lixo — uma OC que NÃO existe pra
    esse fornecedor (número da OC do fornecedor, ou até uma data como
    20260722). Depois de limpar, o fluxo reconfere o item do zero contra as
    OCs reais (procura a existência de outra). Sem limpar, o campo fantasma
    fica na E000IPC pra sempre.
    """
    sql = """
        UPDATE sapiens.E000IPC
           SET NUMOCP = 0, SEQIPO = 0
         WHERE CHVNEL = :chvnel AND SEQIPC = :seqipc
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, chvnel=chvnel, seqipc=seqipc)
        conn.commit()
    finally:
        conn.close()


def buscar_seqipos_da_oc(codemp, codfil, numocp) -> list:
    """
    Retorna os SEQIPO de uma OC, ordenados. Usada depois de gerar uma OC nova
    via webservice (Inclusão) — a resposta do GravarOrdensCompra_8 devolve só
    o NUMOCP, nunca o SEQIPO de cada produto enviado — pra mapear de volta,
    na mesma ordem em que os produtos foram enviados, quais SEQIPO o Sapiens
    de fato criou pra cada um (ver nf_processor._vincular_produtos_a_oc_nova).
    """
    sql = """
        SELECT SEQIPO FROM sapiens.E420IPO
        WHERE CODEMP = :codemp AND CODFIL = :codfil AND NUMOCP = :numocp
        ORDER BY SEQIPO
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfil=codfil, numocp=numocp)
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def marcar_nf_aguardando_wms(chvnel: str, cgcfor: str, cgcfil: str) -> None:
    """Grava E000NFC.SITNFC = 6 (aguardando integração WMS) para esta NF."""
    sql = """
        UPDATE sapiens.E000NFC
           SET SITNFC = 6
         WHERE CHVNEL = :chvnel
           AND CGCFOR = :cgcfor
           AND CGCFIL = :cgcfil
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, chvnel=chvnel, cgcfor=cgcfor, cgcfil=cgcfil)
        conn.commit()
    finally:
        conn.close()

def marcar_nf_pronta_importacao(codemp: str, codfil: str, codfor: str, numnfc: str) -> dict:
    """
    PLACEHOLDER — integração da NF na tela de importação do Sapiens.

    A E440NFC NÃO é uma "flag de liberação": é o cabeçalho fiscal COMPLETO da
    NF de entrada (~180 colunas, 11 obrigatórias — CODSNF, DATEMI, CODCPG,
    CIFFOB, SITNFC, INTIMP, INDSIG, além de emp/fil/for/numnfc, e dezenas de
    campos de imposto/base). Um INSERT com poucos campos falha com
    ORA-01400 (foi o que prendia a NF). Montar esse cabeçalho todo à mão é
    inviável — a integração real tem que ser via webservice de importação do
    Sapiens (com.senior.g5.co.mcm.cpr.notafiscalentrada ou equivalente).

    Por enquanto é placeholder: não grava nada no Oracle, só devolve o
    marcador. Quando o webservice existir, é só trocar o corpo aqui.
    """
    # TODO: implementar importação via webservice Sapiens
    print(f"  📥 [PLACEHOLDER] Integrar NF {numnfc} (emp {codemp}/fil {codfil}) — webservice de importação ainda não implementado")
    return {"status": "PLACEHOLDER", "numnfc": numnfc}


def numocp_vinculo_valido(codemp, codfil, numocp, seqipo=None, codfor=None, codpro_esperado=None) -> bool:
    """
    Confere se o NUMOCP gravado na E000IPC corresponde a uma OC REAL (existe
    na E420OCP dessa empresa/filial). Muita NF chega com lixo no campo NUMOCP
    — número da OC do fornecedor, ou até uma data (ex: 20260722 = 22/07/2026)
    — que NÃO é uma OC do nosso Sapiens. Sem validar, o fluxo trata o item
    como "já coberto" por uma OC fantasma e pula a conferência de verdade.

    Retorna True só se a OC existe. Se seqipo for informado, exige também
    que o item (SEQIPO) exista naquela OC. Se `codpro_esperado` for
    informado, exige AINDA que o CODPRO daquela linha bata com o produto do
    item — confirmado num caso real: um vínculo antigo/travado (dado de
    outro teste, de antes desta validação existir) apontava um item pra uma
    OC de um produto completamente diferente ("KIT CORREIA DENTADA"
    vinculado a um item que era outro produto), e como só existência era
    checada, o sistema aceitava pra sempre sem nunca reconferir o produto.

    Propositalmente NÃO confere SITAPR aqui (aprovação da OC) — quem decide
    o que fazer com um vínculo existente-mas-ainda-não-aprovado é
    _rodar_verificacao_oc, não esta função. Tratar "ainda não aprovado" como
    "inválido" faria o chamador (que apaga o vínculo com
    limpar_vinculo_item_oc sempre que esta função retorna False) apagar um
    vínculo perfeitamente correto só porque o Junior ainda não aprovou —
    reclassificando o item do zero e arriscando cancelar/gerar OUTRA OC nova
    à toa, duplicando a que já está esperando aprovação. A checagem de
    aprovação é feita à parte, em cima do retorno True desta função, sem
    mexer no vínculo.

    Atenção ao SEQIPO=0: é o sentinela do Sapiens pra "sem linha" — a NF
    referenciou a OC só no cabeçalho, sem apontar uma linha real da E420IPO.
    Isso NÃO é um vínculo de item válido, então 0 tem que cair no ramo que
    exige a linha existir (e não existe → retorna False), e não no atalho que
    confere só o cabeçalho. Por isso testamos `is not None` em vez de `if
    seqipo` (0 é falsy e escaparia pro atalho, dando True indevido).
    """
    if not numocp:
        return False
    conn = get_connection()
    try:
        cur = conn.cursor()
        if seqipo is not None:
            codpro_str = str(codpro_esperado or "").strip()
            cur.execute("""
                SELECT 1
                FROM sapiens.E420OCP o
                JOIN sapiens.E420IPO i ON i.CODEMP=o.CODEMP AND i.CODFIL=o.CODFIL AND i.NUMOCP=o.NUMOCP
                WHERE o.CODEMP=:codemp AND o.CODFIL=:codfil AND o.NUMOCP=:numocp
                AND o.CODFOR=:codfor
                AND (:seqipo IS NULL OR i.SEQIPO=:seqipo)
                AND (:codpro_esperado IS NULL OR i.CODPRO=:codpro_esperado)
            """, codemp=codemp, codfil=codfil, numocp=numocp, seqipo=seqipo, codfor=codfor,
                 codpro_esperado=(codpro_str or None))
        else:
            cur.execute("""
                SELECT 1 FROM sapiens.E420OCP
                WHERE CODEMP = :codemp AND CODFIL = :codfil AND NUMOCP = :numocp
            """, codemp=codemp, codfil=codfil, numocp=numocp)
        return cur.fetchone() is not None
    finally:
        conn.close()


def nf_esta_cancelada(chvnel: str) -> bool:
    """
    Confere E000NFC.INDCAN pra essa NF (CHVNEL é a chave única — 44 dígitos).
    'S' = cancelada. Usada como trava final, imediatamente antes de gerar
    qualquer OC: a NF pode ficar dias em VERIFICACAO_OC/AGUARDANDO_APROVACAO_OC
    esperando resolução, e nesse meio-tempo pode ser cancelada no Sapiens —
    sem essa checagem, o fluxo geraria/consolidaria uma OC pra uma NF que já
    não vale mais.
    """
    sql = "SELECT INDCAN FROM sapiens.E000NFC WHERE CHVNEL = :chvnel"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, chvnel=chvnel)
        row = cur.fetchone()
        return bool(row and str(row[0]).strip().upper() == "S")
    finally:
        conn.close()


def stanfv_atual(chvnel: str) -> str | None:
    """
    Lê o valor ATUAL de E000NFC.STANFV pra essa NF (CHVNEL é a chave única).
    Usado como filtro de exibição do card na coluna "Aguardando Integração":
    '3' significa que a NF já foi processada/importada de verdade, então o
    card não precisa mais aparecer aguardando.
    """
    sql = "SELECT STANFV FROM sapiens.E000NFC WHERE CHVNEL = :chvnel"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, chvnel=chvnel)
        row = cur.fetchone()
        return str(row[0]).strip() if row else None
    finally:
        conn.close()

def nf_ja_integrada(codemp: str, codfil: str, codfor: str, numnfc: str, chvnel: str) -> bool:
    """ 2° verificação da nota, em caso de já ter sido integrada por algum motivo antes 
        de prosseguir no fluxo de validações.
    """
    conn = get_connection()

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT  CODSNF FROM SAPIENS.E000NFC WHERE CHVNEL = :CHVNEL",
            chvnel=chvnel,
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return False
        codsnf = row [0]

        cur.execute("""
                    SELECT 1 FROM DUAL WHERE EXISTS (
                    SELECT 1 FROM SAPIENS.E440NFC
                    WHERE codemp = :codemp
                    AND codfil = :codfil
                    AND codfor = :codfor
                    AND numnfc = :numnfc
                    AND codsnf = :codsnf 
                    )
        """, codemp=codemp, codfil=codfil, codfor=codfor, numnfc=numnfc, codsnf=codsnf)
        return cur.fetchone() is not None
    finally:
        conn.close()

def verificar_pronto_integracao(codemp: str, codfil: str, codfor: str, numnfc: str) -> bool:
    """
    Confere se a NF já está marcada pronta pra importação em E440NFC
    (SITNFC=2) — mesmo EXISTS que já era usado embutido no SELECT grande de
    /api/nf/dashboard, extraído aqui como função isolada pra servir de porta
    de entrada da etapa AGUARDANDO_INTEGRACAO. Retorna True se já existe
    (idempotente — não insere de novo se já estiver marcada).
    
    """
    sql = """
        SELECT * FROM sapiens.E000NFC
        WHERE numnfc = :numnfc 
          AND STANFV IN (3)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        # Só :numnfc existe no texto do SQL — passar codemp/codfil/codfor como
        # bind (sem placeholder correspondente na query) fazia o Oracle
        # rejeitar a chamada com ORA-01036 (illegal variable name/number) em
        # TODA execução, sempre capturado silenciosamente pelo try/except de
        # quem chama — a checagem de idempotência nunca rodava de verdade.
        cur.execute(sql, numnfc=numnfc)
        return cur.fetchone() is not None
    finally:
        conn.close()

def oc_situacao(codemp: str, codfil: str, numocp: str) -> tuple:
    """
    (SITOCP, SITAPR) da OC como string (ex: ('5', 'APR')), ou (None, None) se a
    OC não existe pra essa empresa/filial. Consulta enxuta pros pontos que só
    precisam saber se a OC ainda está viva — diferente de verificar_oc_aprovada
    (que traz agregados) e de _buscar_oc_por_numocp_conn (que traz todos os
    itens). SITOCP 4 (liquidado) e 5 (cancelada) = OC morta: um vínculo que
    aponta pra uma dessas não pode mais contar como cobertura.
    """
    sql = "SELECT SITOCP, SITAPR FROM sapiens.E420OCP WHERE CODEMP = :codemp AND CODFIL = :codfil AND NUMOCP = :numocp"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfil=codfil, numocp=numocp)
        row = cur.fetchone()
    finally:
        conn.close()
    return (
        str(row[0]).strip() if row and row[0] is not None else None,
        str(row[1]).strip() if row and row[1] is not None else None,
    )


def verificar_oc_aprovada(codemp: str, codfil: str, numocp: str) -> dict | None:
    """
    Reconferência (caminho "OC nova → aguardando aprovação"): checa se a OC
    específica (NUMOCP salvo em `referencia`) já foi de fato APROVADA no
    Sapiens (E420OCP.SITAPR = 'APR') e traz, dessa OC, três agregados via
    subquery:
      • qtd_itens_oc = quantos itens (linhas E420IPO) a OC tem
      • total_qtd_oc = Σ QTDABE
      • saldo_qtd    = Σ (QTDABE − USU_CODANA)   ← saldo já descontando reserva

    Retorna esse dict se `SITAPR='APR'` E a OC ainda está viva
    (`SITOCP IN (1, 2, 9)` — aberta total/parcial/não fechada); retorna None
    se ainda não foi aprovada OU se a OC foi cancelada (SITOCP=5) / liquidada
    (SITOCP=4) depois de aprovada. Sem o filtro de SITOCP, uma OC aprovada e
    depois cancelada continuava contando como "aprovada" e deixava a NF
    presa/liberada em cima de uma OC morta.

    ANTES essa função (chamada `verificar_oc_fechada`) checava
    `SITOCP NOT IN (1, 2)` — achando que "OC não está mais aberta" queria
    dizer "resolvida". Errado: confirmado num teste real que TODA OC nova
    criada por este webservice (`GravarOrdensCompra_8`, tanto pelo caminho
    "já aprovada" quanto pelo "pendente" — não existe diferença real entre
    os dois no Sapiens, só o texto do `obsOcp`) nasce com `SITOCP=9`
    ("Não Fechado") e `SITAPR='ANA'` (Em análise) — e `9 NOT IN (1,2)` é
    verdadeiro, então a checagem antiga achava que a OC já tinha "fechado"
    segundos depois de criada, sem o Junior nunca ter aprovado nada de
    verdade. Isso gerava OCs duplicadas a cada rodada (cada uma achando que
    a anterior já tinha resolvido). `SITAPR='APR'` é o sinal correto —
    confirmado com o usuário.
    """
    sql = """
        SELECT
            o.NUMOCP,
            (
                SELECT COUNT(*)
                FROM sapiens.E420IPO i
                WHERE i.CODEMP = o.CODEMP
                  AND i.CODFIL = o.CODFIL
                  AND i.NUMOCP = o.NUMOCP
            ) AS QTD_ITENS_OC,
            (
                SELECT SUM(NVL(i.QTDABE, 0))
                FROM sapiens.E420IPO i
                WHERE i.CODEMP = o.CODEMP
                  AND i.CODFIL = o.CODFIL
                  AND i.NUMOCP = o.NUMOCP
            ) AS TOTAL_QTD_OC,
            (
                SELECT SUM(NVL(i.QTDABE, 0) - NVL(i.USU_CODANA, 0))
                FROM sapiens.E420IPO i
                WHERE i.CODEMP = o.CODEMP
                  AND i.CODFIL = o.CODFIL
                  AND i.NUMOCP = o.NUMOCP
            ) AS SALDO_QTD
        FROM sapiens.E420OCP o
        WHERE o.CODEMP = :codemp
          AND o.CODFIL = :codfil
          AND o.NUMOCP = :numocp
          AND o.SITAPR = 'APR'
          AND o.SITOCP IN (1, 2, 9)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfil=codfil, numocp=numocp)
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None
    return {
        "numocp":        row[0],
        "qtd_itens_oc":  int(row[1] or 0),
        "total_qtd_oc":  float(row[2] or 0),
        "saldo_qtd":     float(row[3] or 0),
    }

def _buscar_oc_por_numocp_conn(conn, codemp: str, codfil: str, numocp: str, seqipos=None) -> list[dict]:
    """Busca uma OC específica pelo NUMOCP, com seus itens.

    `seqipos` (opcional): coleção de SEQIPO que interessam — quando informada,
    devolve só essas linhas da OC, porque uma OC pode ter dezenas de
    sequências e a NF usar duas (ex: OC 24706, seq 4 e 33). Sem ela (None), ou
    se nenhuma das sequências informadas existir na OC, devolve a OC inteira.
    Filtro só de exibição — não altera conferência.
    """
    sql = """
        SELECT
            o.CODEMP, o.CODFIL, o.NUMOCP, o.CODFOR, o.DATEMI,
            o.SITOCP, o.SITAPR, o.OBSOCP,
            i.SEQIPO, i.CODPRO, i.CODDER, i.CPLIPO, i.PROFOR,
            i.QTDORI, i.QTDREC, i.QTDABE, i.UNIMED, i.PREUNI, i.SITIPO
        FROM sapiens.E420OCP o
        JOIN sapiens.E420IPO i
          ON i.CODEMP = o.CODEMP AND i.CODFIL = o.CODFIL AND i.NUMOCP = o.NUMOCP
        WHERE o.CODEMP = :codemp AND o.CODFIL = :codfil AND o.NUMOCP = :numocp
        ORDER BY i.SEQIPO
    """
    cur = conn.cursor()
    cur.execute(sql, codemp=codemp, codfil=codfil, numocp=numocp)
    rows = rows_to_dict(cur)
    if not rows:
        return []
    # Reduz às sequências que a NF usa. Comparação por str pros dois lados
    # (o SEQIPO pode vir como número do Oracle e como int/str de quem chama).
    # Se o filtro não casar com nada, mantém a OC inteira em vez de devolver
    # uma OC vazia na tela.
    if seqipos:
        alvo = {str(s) for s in seqipos if s is not None}
        rows_filtradas = [r for r in rows if str(r["SEQIPO"]) in alvo]
        if rows_filtradas:
            rows = rows_filtradas
    oc = {
        "numocp": numocp, "codemp": rows[0]["CODEMP"], "codfil": rows[0]["CODFIL"],
        "codfor": rows[0]["CODFOR"], "datemi": rows[0]["DATEMI"],
        "sitocp": rows[0]["SITOCP"], "sitapr": rows[0]["SITAPR"],
        "obsocp": rows[0]["OBSOCP"], "itens": [],
    }
    for row in rows:
        if row["SEQIPO"] is not None:
            oc["itens"].append({
                "seqipo": row["SEQIPO"], "codpro": row["CODPRO"], "codder": row["CODDER"],
                "cplipo": row["CPLIPO"], "profor": row["PROFOR"],
                "qtdori": float(row["QTDORI"] or 0), "qtdrec": float(row["QTDREC"] or 0),
                "qtdabe": float(row["QTDABE"] or 0), "unimed": row["UNIMED"],
                "preuni": float(row["PREUNI"] or 0), "sitipo": row["SITIPO"],
            })
    return [oc]


# ─────────────────────────────────────────────────────────────────────────────
# BUSCAR OCs abertas por fornecedor
# ─────────────────────────────────────────────────────────────────────────────

def buscar_ocs_abertas(codemp: str, codfil: str, codfor: str) -> list[dict]:
    """
    Retorna todas as OCs abertas (SITOCP 1 ou 2) do fornecedor,
    já com os itens expandidos (E420IPO).
    """
    sql = """
        SELECT
            o.CODEMP,
            o.CODFIL,
            o.NUMOCP,
            o.CODFOR,
            o.DATEMI,
            o.SITOCP,
            o.SITAPR,
            o.OBSOCP,
            i.SEQIPO,
            i.CODPRO,
            i.CODDER,
            i.CPLIPO,
            i.PROFOR,
            i.QTDORI,
            i.QTDREC,
            i.QTDABE,
            i.UNIMED,
            i.PREUNI,
            i.SITIPO
        FROM sapiens.E420OCP o
        JOIN sapiens.E420IPO i
          ON i.CODEMP = o.CODEMP
         AND i.CODFIL = o.CODFIL
         AND i.NUMOCP = o.NUMOCP
        WHERE o.CODEMP   = :codemp
          AND o.CODFIL   = :codfil
          AND o.CODFOR   = :codfor
          AND o.SITOCP  IN (1, 2)
          AND i.QTDABE   > 0
          AND i.TNSPRO <> '90403'
        ORDER BY o.NUMOCP, i.SEQIPO
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, codemp=codemp, codfil=codfil, codfor=codfor)
        rows = rows_to_dict(cur)
    finally:
        conn.close()

    # Agrupa itens por OC
    ocs: dict[str, dict] = {}
    for row in rows:
        numocp = str(row["NUMOCP"])
        if numocp not in ocs:
            ocs[numocp] = {
                "numocp":  numocp,
                "codemp":  row["CODEMP"],
                "codfil":  row["CODFIL"],
                "codfor":  row["CODFOR"],
                "datemi":  row["DATEMI"],
                "sitocp":  row["SITOCP"],
                "sitapr":  row["SITAPR"],
                "obsocp":  row["OBSOCP"],
                "itens":   []
            }
        ocs[numocp]["itens"].append({
            "seqipo": row["SEQIPO"],
            "codpro": row["CODPRO"],
            "codder": row["CODDER"],
            "cplipo": row["CPLIPO"],
            "profor": row["PROFOR"],
            "qtdori": float(row["QTDORI"] or 0),
            "qtdrec": float(row["QTDREC"] or 0),
            "qtdabe": float(row["QTDABE"] or 0),
            "unimed": row["UNIMED"],
            "preuni": float(row["PREUNI"] or 0),
            "sitipo": row["SITIPO"],
        })

    return list(ocs.values())


# ─────────────────────────────────────────────────────────────────────────────
# SITUAÇÃO da OC (E420OCP.SITOCP) — usada no relatório de OCs geradas
# ─────────────────────────────────────────────────────────────────────────────

# Descrições do domínio E420OCP.SITOCP no Sapiens (mesmos rótulos do
# dashboard_nf.html → SITOCP_INFO, pra bater com o que a tela mostra).
SITOCP_DESCR = {
    "1": "Aberta — saldo total",
    "2": "Aberta — saldo parcial",
    "4": "Liquidado",
    "5": "Cancelada",
    "9": "Não Fechado",
}


def descricao_sitocp(sitocp) -> str:
    """'1' -> '1 - Aberta — saldo total'.  Código desconhecido volta cru."""
    if sitocp is None or str(sitocp).strip() == "":
        return ""
    cod = str(sitocp).strip()
    descr = SITOCP_DESCR.get(cod)
    return f"{cod} - {descr}" if descr else cod


def buscar_situacao_ocs(chaves) -> dict:
    """
    Recebe um iterável de (codemp, codfil, numocp) e devolve
    {(codemp, codfil, numocp): "<SITOCP> - <descrição>"} lido de E420OCP.
    Chaves não encontradas simplesmente não aparecem no dict.
    Uma OC que falhe individualmente não derruba as outras.
    """
    chaves = {(str(e or ""), str(f or ""), str(o or "")) for e, f, o in chaves}
    chaves.discard(("", "", ""))
    if not chaves:
        return {}

    sql = """
        SELECT SITOCP FROM sapiens.E420OCP
        WHERE CODEMP = :codemp AND CODFIL = :codfil AND NUMOCP = :numocp
    """
    conn = get_connection()
    resultado: dict = {}
    try:
        cur = conn.cursor()
        for codemp, codfil, numocp in chaves:
            try:
                cur.execute(sql, codemp=codemp, codfil=codfil, numocp=numocp)
                row = cur.fetchone()
            except Exception:
                row = None
            if row and row[0] is not None:
                resultado[(codemp, codfil, numocp)] = descricao_sitocp(row[0])
    finally:
        conn.close()
    return resultado


def buscar_oc_detalhe(codemp: str, codfil: str, numocp: str) -> dict | None:
    """OC única (cabeçalho + itens) pra exibição no modal de detalhes do alerta."""
    conn = get_connection()
    try:
        ocs = _buscar_oc_por_numocp_conn(conn, str(codemp), str(codfil), str(numocp))
    finally:
        conn.close()
    return ocs[0] if ocs else None


# SITOCP que contam como "não fechada" — pedido do usuário: aberta total (1),
# aberta parcial (2) e "Não Fechado" (9). Fica de fora Liquidado (4) e
# Cancelada (5).
SITOCP_NAO_FECHADA = (1, 2, 9)


def ocs_geradas_nao_fechadas(chaves, dias: int = 3) -> list[dict]:
    """
    Das OCs informadas em `chaves` (iterável de (codemp, codfil, numocp) —
    as OCs geradas pelo fluxo), devolve as que continuam:
      • aprovadas   → SITAPR = 'APR'
      • sem fechar  → SITOCP IN (1, 2, 9)
      • emitidas há mais de `dias` dias (E420OCP.DATEMI < TRUNC(SYSDATE) - dias)

    Cada item: numocp, codemp, codfil, codfor, nomfor, datemi (ISO), sitocp,
    sitocp_descr, dias_aberto, itens_abertos, valor_aberto, obsocp.
    Ordenado da OC mais antiga (mais dias em aberto) pra mais nova.
    Uma OC que falhe individualmente não derruba as outras.
    """
    chaves = {(str(e or ""), str(f or ""), str(o or "")) for e, f, o in chaves}
    chaves.discard(("", "", ""))
    if not chaves:
        return []

    sql = """
        SELECT
            o.NUMOCP, o.CODEMP, o.CODFIL, o.CODFOR, o.DATEMI, o.SITOCP, o.OBSOCP,
            f.NOMFOR,
            TRUNC(SYSDATE) - TRUNC(o.DATEMI) AS DIAS_ABERTO,
            (SELECT COUNT(*) FROM sapiens.E420IPO i
              WHERE i.CODEMP = o.CODEMP AND i.CODFIL = o.CODFIL AND i.NUMOCP = o.NUMOCP
                AND NVL(i.QTDABE, 0) > 0) AS ITENS_ABERTOS,
            (SELECT NVL(SUM(NVL(i.QTDABE, 0) * NVL(i.PREUNI, 0)), 0) FROM sapiens.E420IPO i
              WHERE i.CODEMP = o.CODEMP AND i.CODFIL = o.CODFIL AND i.NUMOCP = o.NUMOCP) AS VALOR_ABERTO
        FROM sapiens.E420OCP o
        LEFT JOIN sapiens.E095FOR f
          ON f.CODEMP = o.CODEMP AND f.CODFOR = o.CODFOR
        WHERE o.CODEMP = :codemp AND o.CODFIL = :codfil AND o.NUMOCP = :numocp
          AND o.SITAPR = 'APR'
          AND o.SITOCP IN (1, 2, 9)
          AND TRUNC(SYSDATE) - TRUNC(o.DATEMI) > :dias
    """
    conn = get_connection()
    itens: list[dict] = []
    try:
        cur = conn.cursor()
        for codemp, codfil, numocp in chaves:
            try:
                cur.execute(sql, codemp=codemp, codfil=codfil, numocp=numocp, dias=dias)
                rows = rows_to_dict(cur)
            except Exception:
                rows = []
            for r in rows:
                itens.append({
                    "numocp":        str(r["NUMOCP"]),
                    "codemp":        str(r["CODEMP"]),
                    "codfil":        str(r["CODFIL"]),
                    "codfor":        str(r["CODFOR"] or ""),
                    "nomfor":        (r["NOMFOR"] or "").strip(),
                    "datemi":        r["DATEMI"],
                    "sitocp":        r["SITOCP"],
                    "sitocp_descr":  descricao_sitocp(r["SITOCP"]),
                    "dias_aberto":   int(r["DIAS_ABERTO"] or 0),
                    "itens_abertos": int(r["ITENS_ABERTOS"] or 0),
                    "valor_aberto":  float(r["VALOR_ABERTO"] or 0),
                    "obsocp":        (r["OBSOCP"] or "").strip(),
                })
    finally:
        conn.close()
    itens.sort(key=lambda x: x["dias_aberto"], reverse=True)
    return itens


# ─────────────────────────────────────────────────────────────────────────────
# WEBSERVICES de OC — Senior G5 GravarOrdensCompra_8 (webservice_oc.py).
# São 3 integrações:
#   1. gerar_oc_aprovada_via_webservice   — cria uma OC JÁ aprovada (caso
#      Juntar_OC: a quantidade já estava aprovada em OCs anteriores, então a
#      consolidada nasce aprovada e a NF pode integrar).
#   2. gerar_oc_para_aprovacao            — cria uma OC PENDENTE de aprovação
#      (caso OC_Pendente: falta saldo, o Junior precisa aprovar).
#   3. cancelar_item_oc_via_webservice    — altera (Alteração, não Cancelamento
#      da ordem) a quantidade cancelada de UM item de OC de origem. Em
#      Juntar_OC cancela só o consumido (limitado ao QtdRec); em OC_Pendente
#      cancela o saldo inteiro do item. Nunca fecha/cancela a OC inteira.
#
# Não existe campo de "OC já aprovada" no webservice (GravarOrdensCompra_8) —
# as duas funções de geração fazem a MESMA Inclusão (tipoProcessamento=1); a
# diferença é só o texto de obsOcp e o que o fluxo faz depois de cada uma.
# Implementação real em webservice_oc.py — credenciais em backend/.env.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# CONFERÊNCIA DE INTEGRAÇÃO — classificação fiscal (TNSPRO/CODSNF/SOMFRE/
# CTARED/CODCCU) da coluna AGUARDANDO_INTEGRACAO. Hoje é calculada na mão
# (fora do sistema) antes do UPDATE que libera a NF pra importação nativa do
# Sapiens — implementado aqui só pra tirar esse passo manual da frente do
# usuário. Não muda em nada o restante do fluxo: a NF continua saindo da
# coluna só quando stanfv_atual() virar '3' de verdade (importação real do
# Sapiens, fora deste sistema).
# ─────────────────────────────────────────────────────────────────────────────

_SQL_CONFERENCIA_INTEGRACAO = """
    select f.codemp, f.codfil, n.chvnel, c.codfor, n.numnfc, l.codpro, i.seqipc, i.noppro,
    RTRIM(
      case when f.sigufs='PR' and p.codtst in ('001','002','010','005','006') and c.sigufs<>f.sigufs then '2403' else '' end ||
      case when f.sigufs='PR' and p.codtst in ('001','002','010','005','006') and c.sigufs=f.sigufs then '1403' else '' end ||
      case when f.sigufs='PR' and p.codtst in ('SEM') and c.sigufs<>f.sigufs   then '2102' else '' end ||
      case when f.sigufs='PR' and p.codtst in ('SEM') and c.sigufs=f.sigufs   then '1102' else '' end ||
      case when f.sigufs='PR' and p.codtst in ('003') and c.sigufs<>f.sigufs  then '2652' else '' end ||
      case when f.sigufs='PR' and p.codtst in ('003') and c.sigufs=f.sigufs  then '1652' else '' end ||
      case when f.sigufs='SP' and p.codtst in ('002','010','005','006') and c.sigufs<>f.sigufs then '2403' else '' end ||
      case when f.sigufs='SP' and p.codtst in ('002','010','005','006') and c.sigufs=f.sigufs then '1403' else '' end ||
      case when f.sigufs='SP' and p.codtst in ('SEM','001') and c.sigufs<>f.sigufs   then '2102' else '' end ||
      case when f.sigufs='SP' and p.codtst in ('SEM','001') and c.sigufs=f.sigufs   then '1102' else '' end ||
      case when f.sigufs='SP' and p.codtst in ('003') and c.sigufs<>f.sigufs  then '2652' else '' end ||
      case when f.sigufs='SP' and p.codtst in ('003') and c.sigufs=f.sigufs  then '1652' else '' end
    ) as tnspro,
    case when i.noppro is not null then 'NFE' else '' end as codsnf,
    case when n.ciffob='F' then 'S' else 'N' end as somfre,
    1121 as ctared,
    1 as ccusto
    from sapiens.e000nfc n
    join sapiens.e000ipc i on n.cgcfil=i.cgcfil and n.chvnel=i.chvnel
    join sapiens.e070fil f on n.cgcfil=f.numcgc
    join sapiens.e095for c on i.cgcfor=c.cgccpf
    join (
        -- E403FPR pode ter mais de um cadastro de portfolio pro mesmo
        -- (codemp, codfor, profor) — achado real na NF 194033 (2 linhas
        -- pro mesmo fornecedor+produto, uma com DESFOR/SIGUFS preenchidos
        -- e outra em branco). JOIN direto multiplicava o item na
        -- conferência (35 linhas pra 30 itens), disparando "incompleta"
        -- sem ser falta de cadastro de verdade. Fica só com a linha mais
        -- recentemente atualizada (DATATU/HORATU) por combinação.
        SELECT codemp, codfor, profor, codpro,
               ROW_NUMBER() OVER (
                   PARTITION BY codemp, codfor, profor
                   ORDER BY datatu DESC, horatu DESC
               ) AS rn
        FROM sapiens.e403fpr
    ) l ON f.codemp=l.codemp and c.codfor=l.codfor and i.profor=l.profor and l.rn = 1
    join sapiens.e075pro p on f.codemp=p.codemp and l.codpro=p.codpro
    where n.chvnel = :chvnel
      and n.cgcfor = :cgcfor
      and n.cgcfil = :cgcfil
"""


def buscar_conferencia_integracao(chvnel: str, cgcfor: str, cgcfil: str) -> dict:
    """
    Roda a classificação fiscal (mesma query, sem gravar nada) pra exibir na
    tela antes da confirmação. CGCFOR/CGCFIL entram no WHERE junto com
    CHVNEL pra garantir que é a MESMA nota mostrada no card do dashboard
    (E000NFC não é unicamente identificada só por CHVNEL).

    `completo=False` quando nem todo item da NF apareceu no resultado
    (produto fora do portfólio E403FPR ou sem E075PRO, por exemplo) — mostra
    assim mesmo, mas confirmar_conferencia_integracao trava nesse caso
    (mesma filosofia das Travas do nf_processor: nunca gravar parcial).
    """
    total_itens = len(listar_seqipc_nf(chvnel))

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_SQL_CONFERENCIA_INTEGRACAO, chvnel=chvnel, cgcfor=cgcfor, cgcfil=cgcfil)
        linhas = rows_to_dict(cur)
    finally:
        conn.close()

    itens = [{
        "seqipc": l["SEQIPC"], "codpro": l["CODPRO"], "tnspro": l["TNSPRO"],
        "ctared": l["CTARED"], "ccusto": l["CCUSTO"],
    } for l in linhas]

    cabecalho = None
    if linhas:
        primeira = linhas[0]
        cabecalho = {"codsnf": primeira["CODSNF"], "somfre": primeira["SOMFRE"]}

    return {
        "itens": itens,
        "cabecalho": cabecalho,
        "completo": bool(itens) and len(itens) == total_itens,
        "total_itens_nf": total_itens,
    }


def confirmar_conferencia_integracao(chvnel: str, cgcfor: str, cgcfil: str) -> dict:
    """
    Confirma a conferência: calcula e grava na MESMA conexão/transação —
    antes, buscar_conferencia_integracao() rodava o SELECT numa conexão,
    fechava, e só depois confirmar_conferencia_integracao() abria OUTRA
    conexão pra gravar os UPDATEs. Isso deixava uma janela real (por menor
    que fosse) entre calcular os valores e gravá-los, onde o cadastro
    (E403FPR/E075PRO/etc.) podia mudar sem o UPDATE perceber — não confia em
    valor vindo do client nem de uma consulta anterior, recalcula e já grava
    ali, sem round-trip a mais no meio:
      • E000IPC.TNSPRO/CTARED/CODCCU — um UPDATE por item (SEQIPC)
      • E000NFC.CODSNF/SOMFRE        — um UPDATE só (cabeçalho da NF)
    Só dá commit se todos os UPDATEs derem certo (rollback em qualquer
    falha), pelo mesmo motivo das Travas do nf_processor: nunca deixar a NF
    com só parte dos campos gravados.
    """
    total_itens = len(listar_seqipc_nf(chvnel))

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_SQL_CONFERENCIA_INTEGRACAO, chvnel=chvnel, cgcfor=cgcfor, cgcfil=cgcfil)
        linhas = rows_to_dict(cur)

        itens = [{
            "seqipc": l["SEQIPC"], "codpro": l["CODPRO"], "tnspro": l["TNSPRO"],
            "ctared": l["CTARED"], "ccusto": l["CCUSTO"],
        } for l in linhas]

        completo = bool(itens) and len(itens) == total_itens
        if not completo:
            raise ValueError(
                f"Conferência incompleta: {len(itens)} de "
                f"{total_itens} item(ns) da NF entraram no cálculo "
                f"(produto fora do portfólio/cadastro fiscal?) — nada foi gravado."
            )

        for item in itens:
            cur.execute("""
                UPDATE sapiens.E000IPC
                   SET TNSPRO = :tnspro, CTARED = :ctared, CODCCU = :ccusto
                 WHERE CHVNEL = :chvnel AND SEQIPC = :seqipc
            """, tnspro=item["tnspro"], ctared=item["ctared"], ccusto=item["ccusto"],
                 chvnel=chvnel, seqipc=item["seqipc"])

        cabecalho = {"codsnf": linhas[0]["CODSNF"], "somfre": linhas[0]["SOMFRE"]}
        cur.execute("""
            UPDATE sapiens.E000NFC
               SET CODSNF = :codsnf, SOMFRE = :somfre
             WHERE CHVNEL = :chvnel AND CGCFOR = :cgcfor AND CGCFIL = :cgcfil
        """, codsnf=cabecalho["codsnf"], somfre=cabecalho["somfre"],
             chvnel=chvnel, cgcfor=cgcfor, cgcfil=cgcfil)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    resultado = {"itens": itens, "cabecalho": cabecalho, "completo": completo, "total_itens_nf": total_itens}

    return resultado


from webservice_oc import (
    gerar_oc_aprovada_via_webservice,
    gerar_oc_para_aprovacao,
    cancelar_item_oc_via_webservice,
)