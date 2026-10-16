import json
import time

from db_local import get_local_connection

# Regra de oc ainda não fechada.
DIAS_LIMITE = 3

# Só utiliza nf dessa janela de intervalo.
JANELA_DIAS = 180

_TTL_SEG = 300
_cache = {"em": 0.0, "dados": None}

def _coletar_chaves_ocs_geradas() -> set:
    """(codemp, codfil, numocp) de toda OC que o fluxo gerou (chave apr/pen)."""
    conn = get_local_connection()
    try:
        rows = conn.execute(
            """
            SELECT codemp, codfil, divergencias
            FROM nf_processamento
            WHERE status = 'AGUARDANDO_APROVACAO_OC'
              AND criado_em >= date('now', ?, 'localtime')
            """,
            (f"-{JANELA_DIAS} day",),
        ).fetchall()
    finally:
        conn.close()

    chaves = set()
    for codemp, codfil, div in rows:
        try:
            ref = (json.loads(div or "[]") or [{}])[0].get("referencia") or {}
        except Exception:
            ref = {}
        if not isinstance(ref, dict):
            continue
        for chave in ("apr", "pen"):
            numocp = ref.get(chave)
            if numocp:
                chaves.add((str(codemp or ""), str(codfil or ""), str(numocp)))
    return chaves

def ocs_nao_fechadas(force: bool = False) -> list[dict]:
    agora = time.time()
    if not force and _cache["dados"] is not None and (agora - _cache["em"]) < _TTL_SEG:
        return _cache["dados"]

    try:
        from oc_service import ocs_geradas_nao_fechadas
        dados = ocs_geradas_nao_fechadas(_coletar_chaves_ocs_geradas(), DIAS_LIMITE)
    except Exception as e:
        print(f"[alertas_oc] falha ao consultar OCs não fechadas: {e!r}")
        dados = _cache["dados"] or []
    _cache["dados"] = dados
    _cache["em"] = agora
    return dados
