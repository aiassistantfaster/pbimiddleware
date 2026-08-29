"""
Faster Cars — Power BI middleware for the respond.io AI Agent.
GET /api/cars?model=patrol  ->  rows from the Power BI cars table (filtered).

Env vars (set on Railway):
  TENANT_ID       Azure tenant id
  CLIENT_ID       app registration client id
  CLIENT_SECRET   app registration secret
  PBI_DATASET_ID  7984d771-686f-4ad3-a442-e46ff08f5f1f
  PBI_TABLE_CARS  exact table name in the dataset, e.g. Cars
  API_KEY         any long random string; respond.io must send it as x-api-key
"""
import os, time, re
import requests
from fastapi import FastAPI, Header, HTTPException, Query

TENANT_ID      = os.environ["TENANT_ID"]
CLIENT_ID      = os.environ["CLIENT_ID"]
CLIENT_SECRET  = os.environ["CLIENT_SECRET"]
PBI_DATASET_ID = os.environ["PBI_DATASET_ID"]
PBI_TABLE_CARS = os.environ.get("PBI_TABLE_CARS", "Cars")
API_KEY        = os.environ["API_KEY"]

TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
QUERY_URL = f"https://api.powerbi.com/v1.0/myorg/datasets/{PBI_DATASET_ID}/executeQueries"

app = FastAPI(title="Faster PBI middleware")

# ---- Azure AD token (cached) -------------------------------------------------
_token: dict = {"value": None, "exp": 0.0}

def get_token() -> str:
    if _token["value"] and time.time() < _token["exp"] - 120:
        return _token["value"]
    r = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://analysis.windows.net/powerbi/api/.default",
    }, timeout=20)
    r.raise_for_status()
    j = r.json()
    _token["value"] = j["access_token"]
    _token["exp"] = time.time() + int(j.get("expires_in", 3600))
    return _token["value"]

# ---- Dataset rows (cached 60s) ----------------------------------------------
_rows: dict = {"data": None, "exp": 0.0}

def fetch_cars() -> list[dict]:
    if _rows["data"] is not None and time.time() < _rows["exp"]:
        return _rows["data"]
    dax = f"EVALUATE TOPN(1000, '{PBI_TABLE_CARS}')"
    r = requests.post(
        QUERY_URL,
        headers={"Authorization": f"Bearer {get_token()}"},
        json={"queries": [{"query": dax}],
              "serializerSettings": {"includeNulls": True}},
        timeout=30,
    )
    if r.status_code != 200:
        raise HTTPException(502, f"Power BI error {r.status_code}: {r.text[:300]}")
    raw = r.json()["results"][0]["tables"][0].get("rows", [])
    # keys arrive as "Table[Column]" -> strip to "Column"
    rows = [{re.sub(r"^.*\[|\]$", "", k): v for k, v in row.items()} for row in raw]
    _rows["data"], _rows["exp"] = rows, time.time() + 60
    return rows

# ---- Endpoints ---------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/api/cars")
def cars(
    x_api_key: str = Header(default=""),
    model: str = Query(default="", description="car name/model to search for"),
    limit: int = Query(default=5, le=20),
):
    if x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")
    rows = fetch_cars()
    if model:
        terms = [t for t in model.lower().split() if t]
        def match(row: dict) -> bool:
            blob = " ".join(str(v).lower() for v in row.values())
            return all(t in blob for t in terms)
        rows = [r for r in rows if match(r)]
    if not rows:
        return {"found": 0, "cars": [],
                "note": "No match in fleet data. Tell the customer the team will confirm availability."}
    return {"found": len(rows), "cars": rows[:limit]}
