import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "local.db")


def get_local_connection() -> sqlite3.Connection:
    """
    Retorna uma conexão com o banco SQLite local.
    Garante que todas as tabelas necessárias existam antes de devolver a conexão.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """
    Cria as tabelas do banco local caso ainda não existam.
    Executado automaticamente a cada conexão (sem custo quando já existe).
    """
    conn.executescript("""
        -- ─────────────────────────────────────────────────────────────────────
        -- Registro de conferências de NF — uma linha por estágio atual da NF
        -- (fluxo de 5 etapas: VERIFICACAO_CADASTRAL, AGUARDANDO_LIGACAO,
        -- VERIFICACAO_OC, AGUARDANDO_APROVACAO_OC, AGUARDANDO_INTEGRACAO).
        -- ─────────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS nf_processamento (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Identificadores da NF
            chvnel       TEXT    NOT NULL,          -- Chave de 44 dígitos (NF-e / CT-e)
            numnfc       TEXT    NOT NULL,          -- Número da NF
            codfor       TEXT    NOT NULL,          -- Código do fornecedor (E095FOR)

            -- Estágio atual da NF — mesmo valor de divergencias[0].tipo
            -- (VERIFICACAO_CADASTRAL | AGUARDANDO_LIGACAO | VERIFICACAO_OC |
            -- AGUARDANDO_APROVACAO_OC | AGUARDANDO_INTEGRACAO). Gravado aqui
            -- também (não só dentro do JSON) pra dar pra consultar/filtrar
            -- direto por SQL sem precisar abrir o JSON.
            status       TEXT    NOT NULL,

            -- Estágio atual (array JSON com 1 elemento):
            --   tipo       : VERIFICACAO_CADASTRAL | AGUARDANDO_LIGACAO |
            --                VERIFICACAO_OC | AGUARDANDO_APROVACAO_OC |
            --                AGUARDANDO_INTEGRACAO
            --   detalhe    : mensagem descritiva pra exibição
            --   referencia : valor específico que a reconferência daquele
            --                estágio usa (lista de PROFOR pendentes em
            --                AGUARDANDO_LIGACAO, ou o NUMOCP em
            --                AGUARDANDO_APROVACAO_OC — preenchido manualmente
            --                até o webservice existir)
            divergencias TEXT    NOT NULL DEFAULT '[]',  -- JSON

            -- Auditoria
            criado_em    TEXT    NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
        );

        -- Índices para as consultas mais comuns
        CREATE INDEX IF NOT EXISTS idx_nfproc_chvnel
            ON nf_processamento (chvnel);

        CREATE INDEX IF NOT EXISTS idx_nfproc_numnfc_codfor
            ON nf_processamento (numnfc, codfor);

        CREATE INDEX IF NOT EXISTS idx_nfproc_status
            ON nf_processamento (status);

        -- ─────────────────────────────────────────────────────────────────────
        -- Log de auditoria — cada ação realizada durante a conferência de uma
        -- NF (Etapa 1, portfólio, item a item, notificações, liberação),
        -- em ordem cronológica, pra dar pra comparar o que o sistema fez
        -- passo a passo (diferente de nf_processamento, que só guarda o
        -- resultado final de cada rodada).
        -- ─────────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS nf_log_acoes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            numnfc      TEXT    NOT NULL,
            chvnel      TEXT    NOT NULL DEFAULT '',
            etapa       TEXT    NOT NULL,   -- VERIFICACAO_CADASTRAL | AGUARDANDO_LIGACAO | VERIFICACAO_OC | AGUARDANDO_APROVACAO_OC | AGUARDANDO_INTEGRACAO | NOTIFICACAO
            acao        TEXT    NOT NULL,   -- descrição curta da ação
            detalhe     TEXT    NOT NULL DEFAULT '',
            seqipc      TEXT,               -- item específico da NF, quando aplicável
            criado_em   TEXT    NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_nf_log_numnfc
            ON nf_log_acoes (numnfc);

        -- ─────────────────────────────────────────────────────────────────────
        -- Usuários do dashboard (login por codusu — dois níveis: admin/comum).
        -- senha_hash e cor ficam NULL até o primeiro acesso (usuário escolhe
        -- senha e cor na hora); codusu ADMIN_CODUSU (ver app.py) nasce admin.
        -- ─────────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS nf_usuarios (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            codusu      TEXT    NOT NULL UNIQUE,
            nomusu      TEXT    NOT NULL DEFAULT '',
            senha_hash  TEXT,
            cor         TEXT,
            is_admin    INTEGER NOT NULL DEFAULT 0,
            criado_em   TEXT    NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
        );

        -- ─────────────────────────────────────────────────────────────────────
        -- Atribuição de NF (só as em AGUARDANDO_INTEGRACAO) a um usuário —
        -- "ativo=1" é a atribuição vigente; desatribuir zera ativo em vez de
        -- apagar a linha, preservando histórico de quem tratou o quê.
        -- ─────────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS nf_atribuicoes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chvnel        TEXT    NOT NULL,
            numnfc        TEXT    NOT NULL,
            codusu        TEXT    NOT NULL,
            atribuido_por TEXT    NOT NULL,
            ativo         INTEGER NOT NULL DEFAULT 1,
            criado_em     TEXT    NOT NULL
                              DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_nfatrib_chvnel_ativo
            ON nf_atribuicoes (chvnel, ativo);

        -- ─────────────────────────────────────────────────────────────────────
        -- Notificações persistentes por usuário (sino) — ex: "Jéssica atribuiu
        -- a nota X a você". lida=0 até o usuário abrir/marcar como lida.
        -- ─────────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS nf_notificacoes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            destinatario TEXT    NOT NULL,
            chvnel       TEXT,
            numnfc       TEXT,
            mensagem     TEXT    NOT NULL,
            lida         INTEGER NOT NULL DEFAULT 0,
            criado_em    TEXT    NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_nfnotif_destinatario_lida
            ON nf_notificacoes (destinatario, lida);
    """)

    # Migração: campos adicionados depois que a tabela já existia em produção
    # — CREATE TABLE IF NOT EXISTS não adiciona colunas em tabela
    # pré-existente, então checa e adiciona manualmente.
    #
    # codemp/codfil/cgcfor/cgcfil: necessários pra rodar as reconferências por
    # etapa (portfólio/OC) sem depender do /api/nf/dashboard continuar
    # trazendo a NF (ela some de lá assim que SITNFC=6 é gravado).
    # nomfor/datemi/vlrfin/qtdite: dados de exibição do card, salvos aqui na
    # primeira vez que a NF é vista — sem isso, o dashboard não teria como
    # montar o card de uma NF que já saiu da lista do Oracle.
    # referencia: valor específico da reconferência do estágio atual (lista
    # de PROFOR pendentes, ou o NUMOCP aguardando aprovação).
    # ocp_apr/ocp_pen/ocp_can: acumuladores da Verificação OC persistidos, pra
    # os webservices de OC consumirem depois (formato desenhado pelo usuário):
    #   Ocp_Apr = "CodPro;QtdRec/..."          (gerar OC aprovada — Juntar_OC)
    #   Ocp_Pen = "CodPro;QtdRec;QtdOcp/..."    (gerar OC pendente — OC_Pendente)
    #   Ocp_Can = "NumOcp;SeqIpo;Qtd/..."       (cancelar itens de OC de origem)
    # notificado: 1 assim que o WhatsApp da etapa atual já foi enviado — trava
    # reenvio nas reconferências (1min) enquanto a NF ficar parada na mesma
    # etapa. Some (volta a 0) quando o estágio muda, porque _registrar_estagio
    # insere linha nova nesse caso.
    colunas_existentes = {row[1] for row in conn.execute("PRAGMA table_info(nf_processamento)")}
    novas_colunas = {
        "codemp":     "TEXT NOT NULL DEFAULT ''",
        "codfil":     "TEXT NOT NULL DEFAULT ''",
        "cgcfor":     "TEXT NOT NULL DEFAULT ''",
        "cgcfil":     "TEXT NOT NULL DEFAULT ''",
        "nomfor":     "TEXT NOT NULL DEFAULT ''",
        "datemi":     "TEXT NOT NULL DEFAULT ''",
        "vlrfin":     "REAL NOT NULL DEFAULT 0",
        "qtdite":     "INTEGER NOT NULL DEFAULT 0",
        "referencia": "TEXT",
        "ocp_apr":    "TEXT NOT NULL DEFAULT ''",
        "ocp_pen":    "TEXT NOT NULL DEFAULT ''",
        "ocp_can":    "TEXT NOT NULL DEFAULT ''",
        "notificado": "INTEGER NOT NULL DEFAULT 0",
    }
    for coluna, ddl in novas_colunas.items():
        if coluna not in colunas_existentes:
            conn.execute(f"ALTER TABLE nf_processamento ADD COLUMN {coluna} {ddl}")

    # colunas_visiveis: quais colunas do board (VERIFICACAO_CADASTRAL,
    # AGUARDANDO_LIGACAO, ...) esse usuário comum pode ver — JSON com a lista
    # de ids, ou NULL (padrão) pra "sem restrição, vê tudo". Configurado pelo
    # admin (ver /api/usuarios/<codusu>/colunas em app.py).
    colunas_usuarios = {row[1] for row in conn.execute("PRAGMA table_info(nf_usuarios)")}
    if "colunas_visiveis" not in colunas_usuarios:
        conn.execute("ALTER TABLE nf_usuarios ADD COLUMN colunas_visiveis TEXT")

    conn.commit()

    _migrar_status_para_estagio(conn)


def _migrar_status_para_estagio(conn: sqlite3.Connection) -> None:
    """
    Migração: a tabela antiga tinha CHECK (status IN ('APROVADA','DIVERGENCIA'))
    — status colapsado em só duas categorias. Agora `status` grava o nome
    exato do estágio (igual ao divergencias[0].tipo). SQLite não deixa
    alterar um CHECK existente com ALTER TABLE, então recria a tabela quando
    detecta o constraint antigo, preservando todas as linhas.
    """
    import json as _json

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='nf_processamento'"
    ).fetchone()
    if not row or not row[0] or "CHECK" not in row[0] or "APROVADA" not in row[0]:
        return  # já migrada (ou tabela nova, sem o CHECK antigo)

    print("  🔧 Migrando nf_processamento: status passa a gravar o estágio exato...")

    colunas = [r[1] for r in conn.execute("PRAGMA table_info(nf_processamento)")]
    linhas = [dict(zip(colunas, r)) for r in conn.execute("SELECT * FROM nf_processamento").fetchall()]

    conn.execute("ALTER TABLE nf_processamento RENAME TO nf_processamento_migracao_status")
    conn.execute("""
        CREATE TABLE nf_processamento (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chvnel       TEXT    NOT NULL,
            numnfc       TEXT    NOT NULL,
            codfor       TEXT    NOT NULL,
            status       TEXT    NOT NULL,
            divergencias TEXT    NOT NULL DEFAULT '[]',
            criado_em    TEXT    NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
            codemp       TEXT    NOT NULL DEFAULT '',
            codfil       TEXT    NOT NULL DEFAULT '',
            cgcfor       TEXT    NOT NULL DEFAULT '',
            cgcfil       TEXT    NOT NULL DEFAULT '',
            nomfor       TEXT    NOT NULL DEFAULT '',
            datemi       TEXT    NOT NULL DEFAULT '',
            vlrfin       REAL    NOT NULL DEFAULT 0,
            qtdite       INTEGER NOT NULL DEFAULT 0,
            referencia   TEXT
        )
    """)

    for linha in linhas:
        try:
            divs = _json.loads(linha.get("divergencias") or "[]")
            tipo = divs[0].get("tipo") if divs else None
        except Exception:
            tipo = None
        status_novo = tipo or linha.get("status") or "VERIFICACAO_CADASTRAL"

        conn.execute("""
            INSERT INTO nf_processamento
                (id, chvnel, numnfc, codfor, status, divergencias, criado_em,
                 codemp, codfil, cgcfor, cgcfil, nomfor, datemi, vlrfin, qtdite, referencia)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            linha.get("id"), linha.get("chvnel"), linha.get("numnfc"), linha.get("codfor"),
            status_novo, linha.get("divergencias"), linha.get("criado_em"),
            linha.get("codemp", ""), linha.get("codfil", ""), linha.get("cgcfor", ""), linha.get("cgcfil", ""),
            linha.get("nomfor", ""), linha.get("datemi", ""), linha.get("vlrfin", 0) or 0,
            linha.get("qtdite", 0) or 0, linha.get("referencia"),
        ))

    conn.execute("DROP TABLE nf_processamento_migracao_status")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_nfproc_chvnel ON nf_processamento (chvnel)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nfproc_numnfc_codfor ON nf_processamento (numnfc, codfor)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nfproc_status ON nf_processamento (status)")

    conn.commit()
    print(f"  ✅ Migração concluída — {len(linhas)} linha(s) preservada(s).")


def registrar_log_acao(numnfc: str, chvnel: str, etapa: str, acao: str,
                        detalhe: str = "", seqipc=None) -> None:
    """
    Grava uma linha no log de auditoria (nf_log_acoes) — uma ação por
    chamada, em ordem cronológica. Usado por nf_processor.py pra registrar
    cada passo real da conferência, pra dar pra comparar depois no modal
    do dashboard.
    """
    conn = get_local_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO nf_log_acoes (numnfc, chvnel, etapa, acao, detalhe, seqipc)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (str(numnfc), str(chvnel or ""), etapa, acao, detalhe, str(seqipc) if seqipc is not None else None))
    conn.commit()
    conn.close()