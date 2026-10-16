import os

import oracledb


def _ler_env(chave: str, default: str = "") -> str:
    """Lê backend/.env (mesmo padrão simples usado em webservice_oc.py/app.py — sem lib externa)."""
    caminho = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha.startswith(f"{chave}="):
                    return linha.split("=", 1)[1].strip()
    return os.environ.get(chave, default)


# ORACLE_CLIENT_LIB_DIR aponta pra pasta do Oracle Instant Client (modo
# thick) — necessário quando a senha do usuário do Oracle usa um verificador
# antigo (pré-11g) que o modo thin (puro Python, sem client nenhum) não
# suporta: erro DPY-3015 "password verifier type ... is not supported by
# python-oracledb in thin mode". Sem essa variável, init_oracle_client() só
# acha o client se ele já estiver no PATH do sistema — SEM lib_dir, ele NÃO
# procura em pastas arbitrárias, mesmo que o Instant Client esteja instalado
# ali do lado (foi exatamente esse o problema: client baixado, mas não
# encontrado, e a falha ficava muda por causa do except/pass abaixo).
_lib_dir = _ler_env("ORACLE_CLIENT_LIB_DIR")
try:
    if _lib_dir:
        oracledb.init_oracle_client(lib_dir=_lib_dir)
    else:
        oracledb.init_oracle_client()
except Exception as e:
    # Não é fatal — cai pro modo thin (funciona OK se a senha do Oracle usar
    # um verificador novo). Mas agora pelo menos avisa no console em vez de
    # falhar em silêncio, pra não repetir o mesmo mistério do DPY-3015.
    # SEM emoji/acento aqui de propósito: este print roda na importação do
    # módulo, antes de qualquer coisa do Flask — em console Windows com
    # codepage padrão (cp1252), caractere fora da tabela vira
    # UnicodeEncodeError e MATA o processo inteiro nesse ponto, trocando um
    # aviso inofensivo por um crash na inicialização.
    print("  [AVISO] Oracle Instant Client (thick mode) nao inicializado - caindo pro modo thin. Detalhe: " + repr(e))

DB_USER     = "sapiens"
DB_PASSWORD = "sapiens"
DB_DSN      = "192.168.10.227:1521/dbprod"

# Pool thread-safe: evita que múltiplos threads disputem oracledb.connect() ao mesmo tempo.
# conn.close() devolve ao pool (não fecha de verdade).
_pool = oracledb.create_pool(
    user=DB_USER,
    password=DB_PASSWORD,
    dsn=DB_DSN,
    min=1,
    max=10,
    increment=1,
    getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
    wait_timeout=15000,
)

def get_connection():
    conn = _pool.acquire()
    conn.call_timeout = 60000  # ms — encerra query se Oracle não responder em 60s
    return conn

def buscar_empresas() -> list[str]:
    """Retorna os códigos de todas as empresas cadastradas na E070EMP."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT CODEMP FROM sapiens.E070EMP")
        return [str(row[0]) for row in cur.fetchall()]
    finally:
        conn.close()

def rows_to_dict(cursor):
    import datetime
    cols = [d[0].upper() for d in cursor.description]
    result = []
    for row in cursor.fetchall():
        record = {}
        for col, val in zip(cols, row):
            if isinstance(val, (datetime.datetime, datetime.date)):
                record[col] = val.isoformat()
            else:
                record[col] = val
        result.append(record)
    return result
