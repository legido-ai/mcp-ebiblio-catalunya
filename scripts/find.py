import unicodedata
from urllib.parse import urlencode
from odilo_ebiblio.server import get_client

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return s

c = get_client()
c.login()

def fetch(q, limit=100, offset=0, order="fechaModifica_dt:desc"):
    params = {"limit": limit, "offset": offset, "query": q, "order": order,
              "faceted": "true", "availability": "true", "lists": "true",
              "showExperiences": "true", "enableBiblioFacet": "true", "limitFacetValues": 16}
    return c._request("GET", "/records?" + urlencode(params)).json()

for q in ["cañas y barro", "barro", "cañas y barro blasco ibañez"]:
    total = None
    hits = []
    seen = 0
    for off in range(0, 500, 100):
        p = fetch(q, offset=off)
        total = p.get("total")
        rows = p.get("records") or []
        if not rows:
            break
        seen += len(rows)
        for r in rows:
            t = norm(r.get("title"))
            if "canas" in t and "barro" in t:
                hits.append((r.get("id"), r.get("title"), r.get("resourceTypes")))
        if seen >= (total or 0):
            break
    print(f"q={q!r} total={total} scanned={seen} hits={hits}")
