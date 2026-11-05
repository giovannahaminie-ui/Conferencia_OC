"""
sapiens_auth.py
─────────────────────────────────────────────────────────────────────────────
Login do dashboard usando o usuário/senha que a pessoa já tem no Sapiens.

Como funciona:
  1. A pessoa digita o NOMUSU (nome de login do Sapiens, ex: "nome.sobrenome")
     e a senha.
  2. Validamos no conector SID do Sapiens (ACAO=EXESENHA). Sucesso = o
     conector devolve só o código de conexão (número); falha = "ERRO: ...".
  3. Com a senha validada, buscamos o CODUSU numérico em R999USU — é ele que
     o resto do sistema usa como identificador (ex: 675 = admin).

A senha só trafega daqui pro conector (POST, nunca em query string/URL) e
NUNCA é gravada nem logada — o dashboard não guarda senha nenhuma.

Config (opcional) em backend/.env:
    SAPIENS_CONECTOR_URL=http://192.168.10.235:8000/sapiensweb/conector
"""

import os
import re
import time
import threading

import requests

from db_oracle import get_connection

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
_URL_PADRAO = "http://192.168.10.235:8000/sapiensweb/conector"
_TIMEOUT = 15  # segundos

# Só aceita login com caracteres que um NOMUSU do Sapiens pode ter — evita
# mandar lixo/caractere especial pro conector.
_RE_USUARIO = re.compile(r"^[A-Za-z0-9._\-@]{1,60}$")


def _url_conector() -> str:
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if linha.startswith("SAPIENS_CONECTOR_URL="):
                    valor = linha.split("=", 1)[1].strip()
                    if valor:
                        return valor.split("?")[0]
    return os.environ.get("SAPIENS_CONECTOR_URL", _URL_PADRAO).split("?")[0]


# ─────────────────────────────────────────────────────────────────────────────
# Limite de tentativas: o Sapiens pode bloquear o usuário depois de várias
# senhas erradas, então não deixamos ninguém ficar martelando daqui.
# ─────────────────────────────────────────────────────────────────────────────
_MAX_FALHAS = 6
_JANELA_S   = 300
_falhas: dict[str, list[float]] = {}
_lock = threading.Lock()


def _bloqueado(chave: str) -> bool:
    agora = time.time()
    with _lock:
        recentes = [t for t in _falhas.get(chave, []) if agora - t < _JANELA_S]
        _falhas[chave] = recentes
        return len(recentes) >= _MAX_FALHAS


def _registrar_falha(chave: str) -> None:
    with _lock:
        _falhas.setdefault(chave, []).append(time.time())


def _limpar_falhas(chave: str) -> None:
    with _lock:
        _falhas.pop(chave, None)


# ─────────────────────────────────────────────────────────────────────────────

def _validar_no_conector(usuario: str, senha: str) -> tuple[bool, str]:
    """
    (True, "") se o Sapiens aceitou; (False, motivo) caso contrário.
    Levanta requests.RequestException se o conector estiver fora do ar.
    """
    resp = requests.post(
        _url_conector(),
        data={
            "SIS": "CO", "LOGIN": "SID", "ACAO": "EXESENHA",
            "NOMUSU": usuario, "SENUSU": senha,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    texto = resp.content.decode("iso-8859-1", errors="replace").strip()
    # Sucesso: só o código de conexão (dígitos). Qualquer outra coisa é erro.
    if re.fullmatch(r"\d{5,}", texto):
        return True, ""
    if texto.upper().startswith("ERRO"):
        return False, texto
    return False, "Resposta inesperada do Sapiens."


def _buscar_codusu(usuario: str) -> tuple[str, str] | None:
    """(codusu, nomusu) do R999USU pelo nome de login, sem diferenciar maiúscula/minúscula."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT CODUSU, NOMUSU FROM sapiens.r999usu WHERE UPPER(NOMUSU) = UPPER(:u)",
            {"u": usuario},
        )
        row = cur.fetchone()
        return (str(row[0]), str(row[1])) if row else None
    finally:
        conn.close()


def autenticar(usuario: str, senha: str, origem: str = "") -> dict:
    """
    Devolve {"ok": True, "codusu": "675", "nomusu": "giovanna.ferreira"} ou
    {"ok": False, "erro": "...", "status": <http>}.
    """
    usuario = (usuario or "").strip()
    senha = senha or ""
    if not usuario or not senha:
        return {"ok": False, "erro": "Informe usuário e senha do Sapiens.", "status": 400}
    if not _RE_USUARIO.match(usuario):
        return {"ok": False, "erro": "Usuário ou senha inválidos.", "status": 401}

    chave = f"{usuario.lower()}|{origem}"
    if _bloqueado(chave):
        return {"ok": False, "status": 429,
                "erro": "Muitas tentativas erradas. Aguarde alguns minutos e tente de novo."}

    try:
        ok, motivo = _validar_no_conector(usuario, senha)
    except requests.RequestException:
        return {"ok": False, "status": 503,
                "erro": "Não consegui falar com o Sapiens agora. Tente de novo em instantes."}

    if not ok:
        _registrar_falha(chave)
        return {"ok": False, "erro": "Usuário ou senha inválidos.", "status": 401}

    try:
        achado = _buscar_codusu(usuario)
    except Exception:
        return {"ok": False, "status": 503,
                "erro": "Senha confere, mas não consegui consultar o cadastro de usuários do Sapiens."}
    if not achado:
        return {"ok": False, "status": 403,
                "erro": "Usuário validado, mas não encontrado no cadastro de usuários do Sapiens."}

    _limpar_falhas(chave)
    codusu, nomusu = achado
    return {"ok": True, "codusu": codusu, "nomusu": nomusu}
