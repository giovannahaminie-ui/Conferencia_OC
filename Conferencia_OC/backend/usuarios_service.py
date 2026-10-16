"""
usuarios_service.py
─────────────────────────────────────────────────────────────────────────────
Login, atribuição de NF e notificações do dashboard — funcionalidade nova,
sem nenhuma relação com o fluxo de conferência (nf_processor/oc_service).

Login: qualquer codusu pode entrar. Se não existe ainda em nf_usuarios, é
criado na hora (nível "comum" por padrão). Sem senha cadastrada (senha_hash
NULL) → é o primeiro acesso, a tela pede pra criar senha + escolher cor antes
de liberar o dashboard.

Dois níveis: admin (só ADMIN_CODUSUS, ver abaixo) e comum (todo o resto).
Pra promover alguém a admin depois, é só gravar is_admin=1 direto em
nf_usuarios (SQLite) ou adicionar o codusu em ADMIN_CODUSUS.
"""

import json

from werkzeug.security import generate_password_hash, check_password_hash

from db_local import get_local_connection

# Codusu que nasce admin automaticamente no primeiro login. 675 = temporário
# (pedido explícito) até o codusu real da Jéssica ser conhecido — trocar
# aqui, ou promover via UPDATE nf_usuarios SET is_admin=1 direto no SQLite.
ADMIN_CODUSUS = {"675"}

# Ids das colunas do board (mesmos ids de COLUNAS no dashboard_nf.html) —
# usado só pra validar o que o admin manda gravar em colunas_visiveis.
COLUNAS_BOARD = {
    "pendente-importacao-xml",
    "verificacao-cadastral", "aguardando-ligacao", "verificacao-oc",
    "aguardando-aprovacao-oc", "aguardando-integracao",
}


def _row_usuario(row) -> dict:
    colunas_raw = row["colunas_visiveis"]
    return {
        "codusu":   row["codusu"],
        "nomusu":   row["nomusu"],
        "cor":      row["cor"],
        "is_admin": bool(row["is_admin"]),
        "precisa_criar_senha": row["senha_hash"] is None,
        # None = sem restrição (vê todas as colunas) — padrão pra quem nunca foi configurado.
        "colunas_visiveis": json.loads(colunas_raw) if colunas_raw else None,
    }


def buscar_usuario(codusu: str) -> dict | None:
    conn = get_local_connection()
    try:
        row = conn.execute(
            "SELECT * FROM nf_usuarios WHERE codusu = ?", (str(codusu),)
        ).fetchone()
        return _row_usuario(row) if row else None
    finally:
        conn.close()


def obter_ou_criar_usuario(codusu: str, nomusu: str = "") -> dict:
    """
    Login — passo 1 (informar codusu). Se o usuário já existe, devolve como
    está (com precisa_criar_senha indicando se falta definir senha). Se não
    existe, cria agora mesmo (comum, exceto se estiver em ADMIN_CODUSUS),
    sem senha ainda — precisa_criar_senha=True.
    """
    codusu = str(codusu).strip()
    conn = get_local_connection()
    try:
        row = conn.execute(
            "SELECT * FROM nf_usuarios WHERE codusu = ?", (codusu,)
        ).fetchone()
        if row:
            return _row_usuario(row)

        is_admin = 1 if codusu in ADMIN_CODUSUS else 0
        conn.execute(
            "INSERT INTO nf_usuarios (codusu, nomusu, is_admin) VALUES (?, ?, ?)",
            (codusu, nomusu, is_admin),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM nf_usuarios WHERE codusu = ?", (codusu,)
        ).fetchone()
        return _row_usuario(row)
    finally:
        conn.close()


def definir_senha(codusu: str, senha: str, cor: str) -> dict:
    """
    Login — passo 2, só no primeiro acesso (senha_hash ainda NULL). Define
    senha e cor de uma vez; chamadas seguintes (senha já definida) são
    rejeitadas — pra trocar senha depois seria uma tela separada, não pedida.
    """
    codusu = str(codusu).strip()
    conn = get_local_connection()
    try:
        row = conn.execute(
            "SELECT senha_hash FROM nf_usuarios WHERE codusu = ?", (codusu,)
        ).fetchone()
        if not row:
            raise ValueError("Usuário não encontrado.")
        if row["senha_hash"] is not None:
            raise ValueError("Senha já definida para este usuário.")

        conn.execute(
            "UPDATE nf_usuarios SET senha_hash = ?, cor = ? WHERE codusu = ?",
            (generate_password_hash(senha), cor, codusu),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM nf_usuarios WHERE codusu = ?", (codusu,)
        ).fetchone()
        return _row_usuario(row)
    finally:
        conn.close()


def atualizar_cor(codusu: str, cor: str) -> dict:
    """
    Troca a cor pessoal do usuário logado (usada na inicial do avatar e nos
    pontinhos de atribuição de NF). Só a cor — não mexe em senha/nível.
    """
    codusu = str(codusu).strip()
    cor = str(cor or "").strip()
    if not cor:
        raise ValueError("Informe uma cor.")
    conn = get_local_connection()
    try:
        row = conn.execute("SELECT codusu FROM nf_usuarios WHERE codusu = ?", (codusu,)).fetchone()
        if not row:
            raise ValueError("Usuário não encontrado.")
        conn.execute("UPDATE nf_usuarios SET cor = ? WHERE codusu = ?", (cor, codusu))
        conn.commit()
        row = conn.execute("SELECT * FROM nf_usuarios WHERE codusu = ?", (codusu,)).fetchone()
        return _row_usuario(row)
    finally:
        conn.close()


def validar_login(codusu: str, senha: str) -> dict | None:
    """Confere senha de um usuário que já passou pelo primeiro acesso."""
    conn = get_local_connection()
    try:
        row = conn.execute(
            "SELECT * FROM nf_usuarios WHERE codusu = ?", (str(codusu).strip(),)
        ).fetchone()
        if not row or row["senha_hash"] is None:
            return None
        if not check_password_hash(row["senha_hash"], senha):
            return None
        return _row_usuario(row)
    finally:
        conn.close()


def listar_usuarios() -> list[dict]:
    """
    Pra popular a lista de 'atribuir a...' e o painel de configuração de
    colunas (só admin) — codusu/nomusu/cor/is_admin/colunas_visiveis, sem
    dado sensível (sem senha_hash).
    """
    conn = get_local_connection()
    try:
        rows = conn.execute(
            "SELECT codusu, nomusu, cor, is_admin, colunas_visiveis "
            "FROM nf_usuarios WHERE senha_hash IS NOT NULL ORDER BY nomusu"
        ).fetchall()
        resultado = []
        for r in rows:
            d = dict(r)
            colunas_raw = d.pop("colunas_visiveis")
            d["colunas_visiveis"] = json.loads(colunas_raw) if colunas_raw else None
            resultado.append(d)
        return resultado
    finally:
        conn.close()


def definir_colunas_visiveis(codusu: str, colunas: list[str] | None) -> dict:
    """
    Define quais colunas do board o usuário comum pode ver.
    `colunas=None` remove a restrição (volta ao padrão — vê todas). Uma lista
    (mesmo vazia) grava exatamente esse conjunto; lista vazia esconde o board
    inteiro pra esse usuário — é uma escolha válida do admin, não um erro.
    """
    codusu = str(codusu).strip()
    conn = get_local_connection()
    try:
        row = conn.execute("SELECT codusu FROM nf_usuarios WHERE codusu = ?", (codusu,)).fetchone()
        if not row:
            raise ValueError("Usuário não encontrado.")

        valor = None
        if colunas is not None:
            invalidas = set(colunas) - COLUNAS_BOARD
            if invalidas:
                raise ValueError(f"Coluna(s) inválida(s): {', '.join(sorted(invalidas))}")
            valor = json.dumps(colunas)

        conn.execute("UPDATE nf_usuarios SET colunas_visiveis = ? WHERE codusu = ?", (valor, codusu))
        conn.commit()
        row = conn.execute("SELECT * FROM nf_usuarios WHERE codusu = ?", (codusu,)).fetchone()
        return _row_usuario(row)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Atribuição de NF
# ─────────────────────────────────────────────────────────────────────────

def atribuicao_atual(chvnel: str) -> dict | None:
    conn = get_local_connection()
    try:
        row = conn.execute("""
            SELECT a.*, u.nomusu, u.cor
            FROM nf_atribuicoes a
            JOIN nf_usuarios u ON u.codusu = a.codusu
            WHERE a.chvnel = ? AND a.ativo = 1
            ORDER BY a.id DESC LIMIT 1
        """, (chvnel,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def atribuicoes_ativas() -> dict:
    """Todas as atribuições vigentes, indexadas por chvnel — pra montar o board inteiro numa consulta só."""
    conn = get_local_connection()
    try:
        rows = conn.execute("""
            SELECT a.chvnel, a.codusu, u.nomusu, u.cor
            FROM nf_atribuicoes a
            JOIN nf_usuarios u ON u.codusu = a.codusu
            WHERE a.ativo = 1
        """).fetchall()
        return {r["chvnel"]: dict(r) for r in rows}
    finally:
        conn.close()


def atribuir_nf(chvnel: str, numnfc: str, codusu_destino: str, atribuido_por: str) -> None:
    """
    Atribui a NF a um usuário (substitui a atribuição vigente, se houver) e
    gera notificação persistente pro destinatário. `atribuido_por` é o codusu
    de quem está atribuindo (sempre um admin — checado na rota).
    """
    conn = get_local_connection()
    try:
        conn.execute(
            "UPDATE nf_atribuicoes SET ativo = 0 WHERE chvnel = ? AND ativo = 1",
            (chvnel,),
        )
        conn.execute(
            "INSERT INTO nf_atribuicoes (chvnel, numnfc, codusu, atribuido_por) VALUES (?, ?, ?, ?)",
            (chvnel, numnfc, codusu_destino, atribuido_por),
        )
        nome_admin = conn.execute(
            "SELECT nomusu FROM nf_usuarios WHERE codusu = ?", (atribuido_por,)
        ).fetchone()
        nome_admin = nome_admin["nomusu"] if nome_admin else atribuido_por
        conn.execute(
            "INSERT INTO nf_notificacoes (destinatario, chvnel, numnfc, mensagem) VALUES (?, ?, ?, ?)",
            (codusu_destino, chvnel, numnfc,
             f"{nome_admin} atribuiu a nota {numnfc} a você."),
        )
        conn.commit()
    finally:
        conn.close()


def desatribuir_nf(chvnel: str) -> None:
    conn = get_local_connection()
    try:
        conn.execute(
            "UPDATE nf_atribuicoes SET ativo = 0 WHERE chvnel = ? AND ativo = 1",
            (chvnel,),
        )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Notificações
# ─────────────────────────────────────────────────────────────────────────

def listar_notificacoes(codusu: str, limite: int = 50) -> list[dict]:
    conn = get_local_connection()
    try:
        rows = conn.execute("""
            SELECT id, chvnel, numnfc, mensagem, lida, criado_em
            FROM nf_notificacoes
            WHERE destinatario = ?
            ORDER BY id DESC LIMIT ?
        """, (codusu, limite)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def contar_notificacoes_nao_lidas(codusu: str) -> int:
    conn = get_local_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM nf_notificacoes WHERE destinatario = ? AND lida = 0",
            (codusu,),
        ).fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


def marcar_notificacoes_lidas(codusu: str) -> None:
    conn = get_local_connection()
    try:
        conn.execute(
            "UPDATE nf_notificacoes SET lida = 1 WHERE destinatario = ? AND lida = 0",
            (codusu,),
        )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Desempenho por usuário (painel admin em /crm)
# ─────────────────────────────────────────────────────────────────────────

def desempenho_usuarios(desde: str | None = None) -> dict:
    """
    Painel de desempenho (admin). Por usuário:
      • atribuidas   — nº de NFs distintas já atribuídas a ele (histórico total)
      • finalizadas  — quantas dessas chegaram ao estágio INTEGRADA
      • tempo_*_seg  — segundos entre a ATRIBUIÇÃO e o INTEGRADA (só finalizadas)
    `desde` (ISO 'YYYY-MM-DDTHH:MM:SS') filtra as FINALIZADAS pela data de
    integração; None = tudo. Se uma NF foi reatribuída, vale o ÚLTIMO dono.
    """
    from datetime import datetime

    conn = get_local_connection()
    try:
        atrib_todas = conn.execute(
            "SELECT chvnel, codusu FROM nf_atribuicoes"
        ).fetchall()

        # último dono de cada NF + quando ele a recebeu
        atrib_ultima = conn.execute("""
            SELECT a.chvnel, a.codusu, a.criado_em
            FROM nf_atribuicoes a
            JOIN (SELECT chvnel, MAX(id) AS mid FROM nf_atribuicoes GROUP BY chvnel) u
              ON u.chvnel = a.chvnel AND u.mid = a.id
        """).fetchall()
        dono_de      = {r["chvnel"]: r["codusu"]    for r in atrib_ultima}
        atribuida_em = {r["chvnel"]: r["criado_em"] for r in atrib_ultima}

        # 1ª vez que cada NF chegou a INTEGRADA
        integradas = conn.execute("""
            SELECT chvnel, MIN(criado_em) AS integrada_em
            FROM nf_processamento
            WHERE status = 'INTEGRADA'
            GROUP BY chvnel
        """).fetchall()

        def _parse(s):
            try: return datetime.fromisoformat(s)
            except Exception: return None

        stats = {}
        def _u(cod):
            return stats.setdefault(cod, {
                "atribuidas": set(), "finalizadas": 0,
                "duracoes": [], "ultima_finalizada": None,
            })

        for r in atrib_todas:
            _u(r["codusu"])["atribuidas"].add(r["chvnel"])

        finalizadas_total = 0
        for r in integradas:
            chvnel, quando_int = r["chvnel"], r["integrada_em"]
            if desde and quando_int < desde:
                continue
            cod = dono_de.get(chvnel)
            if not cod:
                continue  # integrou sem nunca ter sido atribuída → não credita
            finalizadas_total += 1
            info = _u(cod)
            info["finalizadas"] += 1
            if not info["ultima_finalizada"] or quando_int > info["ultima_finalizada"]:
                info["ultima_finalizada"] = quando_int
            t0, t1 = _parse(atribuida_em.get(chvnel)), _parse(quando_int)
            if t0 and t1 and t1 >= t0:
                info["duracoes"].append((t1 - t0).total_seconds())

        usuarios_map = {u["codusu"]: u for u in listar_usuarios()}
        linhas = []
        for cod, info in stats.items():
            durs = info["duracoes"]
            linhas.append({
                "codusu": cod,
                "nomusu": (usuarios_map.get(cod) or {}).get("nomusu", cod),
                "cor":    (usuarios_map.get(cod) or {}).get("cor") or "#94a3b8",
                "atribuidas":      len(info["atribuidas"]),
                "finalizadas":     info["finalizadas"],
                "tempo_medio_seg": (sum(durs) / len(durs)) if durs else None,
                "tempo_min_seg":   min(durs) if durs else None,
                "tempo_max_seg":   max(durs) if durs else None,
                "ultima_finalizada": info["ultima_finalizada"],
            })
        linhas.sort(key=lambda x: (-x["finalizadas"], -x["atribuidas"], x["nomusu"]))

        todas_durs = [d for l in stats.values() for d in l["duracoes"]]
        return {
            "geral": {
                "atribuidas_total":  len({r["chvnel"] for r in atrib_todas}),
                "finalizadas_total": finalizadas_total,
                "tempo_medio_seg":   (sum(todas_durs) / len(todas_durs)) if todas_durs else None,
            },
            "usuarios": linhas,
        }
    finally:
        conn.close()
