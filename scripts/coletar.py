#!/usr/bin/env python3
"""Coleta convênios e parcerias celebrados pelo Estado do RS (Portal de Convênios e Parcerias / FPE).

Usa a consulta pública "Convênios Celebrados" e "Parcerias Celebradas", sem login. Cada consulta
devolve no máximo 1.000 linhas; quando o teto é atingido, a coleta se divide por município.
Atualiza docs/data.json, detecta instrumentos novos e grava docs/novos.json e docs/meta.json.

Uso:
  python scripts/coletar.py --auto                  # ano corrente (+ anterior aos domingos)
  python scripts/coletar.py --anos 2023 2024 2025 2026
"""
import argparse, concurrent.futures as cf, datetime as dt, html, http.cookiejar, json, os, re, sys, time
import urllib.parse, urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
from areas import classificar

HOST = "https://portalfpe.sefaz.rs.gov.br"
APL = HOST + "/SefaConv/APL"
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(RAIZ, "docs")
AGORA = dt.datetime.now(ZoneInfo("America/Sao_Paulo"))
TETO = 1000
TIPOS = {"Convênio": "ConvenioFirmado", "Parceria": "ParceriaFirmada"}
CAMPOS = ["f_CodConvenio", "f_CodExercicioConvenio", "f_SituacaoVigencia", "f_CodExercicio", "f_Ex",
          "f_CodOrgao", "f_Municipio", "f_Convenente", "f_CnpjConvenente", "f_Objeto", "f_CodBco",
          "f_CodAgBanc", "f_NroCtaCor"]


class Sessao:
    def __init__(self, tipo):
        self.tipo, self.tela = tipo, TIPOS[tipo]
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [("User-Agent", "painel-convenios-rs/1.0 (consulta diaria a dados publicos; "
                                             "github.com/gsouza15000/painel-convenios-rs)")]

    def req(self, url, dados=None, tentativas=4, espera=15):
        ultimo = None
        for i in range(tentativas):
            try:
                corpo = urllib.parse.urlencode(dados).encode() if dados is not None else None
                with self.op.open(urllib.request.Request(url, data=corpo), timeout=300) as r:
                    return r.read().decode("utf-8", "replace")
            except Exception as e:  # noqa
                ultimo = e
                print(f"  tentativa {i+1} falhou: {e}", file=sys.stderr)
                time.sleep(espera * (i + 1))
        raise RuntimeError(f"falha em {url}: {ultimo}")

    def abrir(self):
        """Reproduz o caminho do navegador (moldura -> transporte -> formulário) para obter a sessão."""
        pag = f"/SefaConv/APL/ProgramasNet/FPE-{self.tela}-PESQUISAR_OUT.aspx"
        self.req(f"{APL}/FPE-Transacao.aspx?{pag}")
        self.req(f"{APL}/FPE-SoeTransporte.ASP?f_NomePag={urllib.parse.quote(pag, safe='')}&")
        form = self.req(f"{APL}/ProgramasNet/FPE-{self.tela}-PESQUISAR_OUT.aspx?f_NomePag={urllib.parse.quote(pag, safe='')}&")
        sel = re.search(r'<select[^>]*name="f_Municipio"[^>]*>(.*?)</select>', form, re.S)
        mun = []
        if sel:
            for v in re.findall(r'<option[^>]*>(.*?)</option>', sel.group(1), re.S):
                v = html.unescape(re.sub(r"<[^>]+>", "", v)).strip()
                if v and v not in mun:
                    mun.append(v)
        self.municipios = mun
        return mun

    def pesquisar(self, **filtros):
        dados = {c: "" for c in CAMPOS if c != ("f_Ex" if self.tipo == "Convênio" else "f_CodExercicio")}
        dados.update({"f_resumoCriterios": "NULL", "fpcJanPopup": ""})
        dados.update(filtros)
        return self.req(f"{APL}/ProgramasNet/FPE-{self.tela}-Pesquisar_in.aspx", dados)


def limpa(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " ")).strip()


def dinheiro(t):
    try:
        return round(float(t.replace(".", "").replace(",", ".")), 2)
    except ValueError:
        return 0.0


def linhas(pagina, tipo):
    """Extrai as linhas da lista de resultados."""
    saida = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", pagina, re.S):
        m = re.search(r'href="([^"]*Editar_Out\.aspx\?CHAVE=([^"]+))"', tr)
        if not m:
            continue
        cel = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cel) < 12:
            continue
        c = [limpa(x) for x in cel]
        if not re.match(r"^\d{4}/\d+", c[0]):
            continue
        ano, num = c[0].split("/")
        vig = re.match(r"(\d{2}/\d{2}/\d{4}) at[ée] (\d{2}/\d{2}/\d{4})", c[10])
        saida.append({
            "k": f"{'C' if tipo == 'Convênio' else 'P'} {ano}/{num}", "t": tipo, "a": int(ano), "n": c[0],
            "cc": c[1], "cv": c[2], "m": c[3], "vc": dinheiro(c[4]), "vp": dinheiro(c[5]), "va": dinheiro(c[6]),
            "vi": vig.group(1) if vig else "", "vf": vig.group(2) if vig else "", "o": c[11],
            "ch": m.group(2)})
    return saida


def total_linhas(pagina):
    m = re.search(r"Linhas de (\d+) a (\d+)", pagina)
    return int(m.group(2)) if m else 0


def coletar_ano(sess, ano):
    """Todos os instrumentos de um ano. Divide por município se bater no teto de 1.000 linhas."""
    pag = sess.pesquisar(f_CodExercicioConvenio=str(ano))
    base = linhas(pag, sess.tipo)
    if total_linhas(pag) < TETO:
        return base
    print(f"  {sess.tipo} {ano}: teto de {TETO} linhas, dividindo por município ({len(sess.municipios)})")
    achados = {x["k"]: x for x in base}

    def um(mun):
        p = sess.pesquisar(f_CodExercicioConvenio=str(ano), f_Municipio=mun)
        r = linhas(p, sess.tipo)
        if total_linhas(p) >= TETO:
            print(f"  ATENÇÃO: {mun} também atingiu o teto", file=sys.stderr)
        return r

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for r in ex.map(um, sess.municipios):
            for x in r:
                achados[x["k"]] = x
    return list(achados.values())


def campo_input(h, nome):
    m = re.search(r'<input[^>]*name="%s"[^>]*>' % nome, h)
    if not m:
        return ""
    v = re.search(r'value="([^"]*)"', m.group(0))
    return html.unescape(v.group(1)).strip() if v else ""


def campo_select(h, nome):
    m = re.search(r'<select[^>]*name="%s"[^>]*>(.*?)</select>' % nome, h, re.S)
    if not m:
        return ""
    o = re.search(r"<option[^>]*selected[^>]*>(.*?)</option>", m.group(1), re.S)
    return limpa(o.group(1)) if o else ""


def detalhar(sess, x):
    """Busca data de assinatura, situação e objeto completo na tela de detalhe."""
    url = f"{APL}/ProgramasNet/FPE-{sess.tela}-Editar_Out.aspx?CHAVE={x['ch']}"
    h = sess.req(url, tentativas=2, espera=5)
    ass = campo_input(h, "f_DtAssinatura")
    if ass:
        x["as"] = ass
    sit = campo_select(h, "f_CodSitProcAplic")
    if sit:
        x["sit"] = sit
    tp = campo_select(h, "f_TipoTransf")
    if tp:
        x["tp"] = tp
    prog = campo_input(h, "f_TitPrgGov")
    if prog:
        x["pg"] = re.sub(r"\s+", " ", prog)
    mo = re.search(r"<textarea[^>]*name=\"f_TxtObj\"[^>]*>(.*?)</textarea>", h, re.S)
    if mo:
        obj = limpa(mo.group(1))
        if obj and len(obj) >= len(x["o"].rstrip(".")):
            x["o"] = obj
    x["det"] = 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anos", nargs="*", type=int)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--detalhes", type=int, default=400, help="máximo de consultas de detalhe por execução")
    args = ap.parse_args()
    if args.anos:
        anos = args.anos
    else:
        anos = [AGORA.year] + ([AGORA.year - 1] if AGORA.weekday() == 6 else [])

    cam = os.path.join(DOCS, "data.json")
    antigos = json.load(open(cam, encoding="utf-8")) if os.path.exists(cam) else []
    base_vazia = len(antigos) == 0
    por_chave = {x["k"]: x for x in antigos}
    novos = []

    sessoes = {}
    for tipo in TIPOS:
        s = Sessao(tipo)
        s.abrir()
        print(f"{tipo}: sessão aberta, {len(s.municipios)} municípios")
        sessoes[tipo] = s
        for ano in anos:
            print(f"coletando {tipo} {ano}...")
            lista = coletar_ano(s, ano)
            ja = sum(1 for x in por_chave.values() if x["t"] == tipo and x["a"] == ano)
            if ja and len(lista) < ja * 0.85:
                raise RuntimeError(f"{tipo} {ano}: o portal devolveu {len(lista)} e já temos {ja}; "
                                   "abortando para não perder dados")
            for x in lista:
                antigo = por_chave.get(x["k"])
                if antigo is None:
                    x["dt"] = "" if base_vazia else AGORA.strftime("%Y-%m-%d")
                    if not base_vazia:
                        novos.append(x)
                else:
                    for c in ("as", "sit", "tp", "pg", "det", "dt"):
                        if c in antigo:
                            x[c] = antigo[c]
                    if antigo.get("det") and len(antigo["o"]) > len(x["o"]):
                        x["o"] = antigo["o"]
                por_chave[x["k"]] = x
            print(f"  {len(lista)} instrumentos")

    # detalhes: novos primeiro, depois os que ainda não têm (mais recentes antes)
    fila = [x for x in novos] + sorted((x for x in por_chave.values() if not x.get("det") and x not in novos),
                                       key=lambda x: (x["a"], x["n"]), reverse=True)
    feitos = 0
    for x in fila[:args.detalhes]:
        try:
            detalhar(sessoes[x["t"]], x)
            feitos += 1
        except Exception as e:  # noqa
            print(f"  detalhe de {x['k']} falhou: {e}", file=sys.stderr)
        time.sleep(0.3)
    print(f"detalhes consultados: {feitos} (faltam {max(0, len(fila) - feitos)})")

    for x in por_chave.values():
        x["ar"] = classificar(x["o"] + " " + x.get("pg", ""))

    todos = sorted(por_chave.values(), key=lambda x: (x["a"], x["n"], x["t"]))
    with open(cam, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in todos) + "\n]\n")

    cam_n = os.path.join(DOCS, "novos.json")
    ant = json.load(open(cam_n, encoding="utf-8")).get("novos", []) if os.path.exists(cam_n) else []
    corte = (AGORA - dt.timedelta(days=30)).strftime("%Y-%m-%d")
    ks = {x["k"] for x in novos}
    mantidos = [x for x in ant if x.get("dt", "") >= corte and x["k"] not in ks]
    reg = [{c: x.get(c, "") for c in ("k", "t", "cc", "cv", "m", "vc", "vi", "vf", "o", "ar", "as", "tp", "dt")}
           for x in novos]
    with open(cam_n, "w", encoding="utf-8") as f:
        json.dump({"gerado_em": AGORA.isoformat(timespec="minutes"), "novos": reg + mantidos}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DOCS, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"atualizado_em": AGORA.isoformat(timespec="minutes"), "total": len(todos),
                   "novos_nesta_coleta": len(novos), "base_inicial": base_vazia}, f, ensure_ascii=False)
    print(f"total {len(todos)}; novos {len(novos)}{' (carga inicial: nada notificado)' if base_vazia else ''}")


if __name__ == "__main__":
    main()
