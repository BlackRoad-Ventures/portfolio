"""
BlackRoad Ventures — Portfolio Metrics Dashboard
FastAPI app serving portfolio KPIs, funding rounds, market data,
Stripe payment webhooks, and Pi fleet health.
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json
import os

app = FastAPI(title="BlackRoad Ventures Dashboard", version="0.2.0")

PORTFOLIO = [
    {"id": "blackroad-os", "name": "BlackRoad OS", "stage": "Series A", "arr": 1_200_000,
     "mrr": 100_000, "agents": 30000, "status": "active", "founded": 2023},
    {"id": "lucidia",      "name": "Lucidia",      "stage": "Seed",     "arr": 180_000,
     "mrr": 15_000,  "agents": 7500,  "status": "active", "founded": 2024},
    {"id": "blackbox",     "name": "Blackbox",      "stage": "Pre-seed", "arr": 60_000,
     "mrr": 5_000,   "agents": 1000,  "status": "active", "founded": 2024},
]

METRICS_SUMMARY = {
    "total_portfolio_companies": len(PORTFOLIO),
    "total_arr": sum(c["arr"] for c in PORTFOLIO),
    "total_mrr": sum(c["mrr"] for c in PORTFOLIO),
    "total_agents_deployed": sum(c["agents"] for c in PORTFOLIO),
    "average_mrr": sum(c["mrr"] for c in PORTFOLIO) // len(PORTFOLIO),
}


@app.get("/")
def root():
    return {"service": "BlackRoad Ventures Dashboard", "version": "0.1.0", "routes": ["/portfolio", "/metrics", "/portfolio/{id}"]}


@app.get("/portfolio")
def list_portfolio(stage: str | None = None):
    items = PORTFOLIO if not stage else [c for c in PORTFOLIO if c["stage"].lower() == stage.lower()]
    return {"companies": items, "count": len(items)}


@app.get("/portfolio/{company_id}")
def get_company(company_id: str):
    c = next((c for c in PORTFOLIO if c["id"] == company_id), None)
    if not c:
        raise HTTPException(404, f"Company '{company_id}' not found")
    return c


@app.get("/metrics")
def get_metrics():
    return METRICS_SUMMARY


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return f"""<!DOCTYPE html>
<html><head><meta charset=UTF-8><title>BlackRoad Ventures</title>
<style>body{{background:#000;color:#fff;font-family:SF Pro,sans-serif;padding:40px;max-width:1000px;margin:0 auto}}
h1{{background:linear-gradient(135deg,#F5A623,#FF1D6C 38.2%,#9C27B0 61.8%,#2979FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-size:2.5rem}}
.metric{{background:#0a0a0a;border:1px solid #222;border-radius:12px;padding:20px;margin:8px 0}}
.metric h3{{color:#888;font-size:.85rem;text-transform:uppercase;letter-spacing:1px;margin:0 0 4px}}
.metric p{{font-size:2rem;font-weight:700;margin:0;color:#FF1D6C}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin:24px 0}}
</style></head><body>
<h1>Ventures Dashboard</h1>
<div class=grid>
  <div class=metric><h3>Total ARR</h3><p>${METRICS_SUMMARY['total_arr']:,}</p></div>
  <div class=metric><h3>Total MRR</h3><p>${METRICS_SUMMARY['total_mrr']:,}</p></div>
  <div class=metric><h3>Agents Deployed</h3><p>{METRICS_SUMMARY['total_agents_deployed']:,}</p></div>
  <div class=metric><h3>Companies</h3><p>{METRICS_SUMMARY['total_portfolio_companies']}</p></div>
</div>
<h2 style="color:#9C27B0">Portfolio</h2>
{''.join(f'<div class=metric><h3>{c["name"]} — {c["stage"]}</h3><p>${c["mrr"]:,}/mo MRR</p></div>' for c in PORTFOLIO)}
</body></html>"""


# ─── Health endpoint (for Pi fleet monitoring) ────────────────────────────────


@app.get("/health")
def health():
    return {"status": "healthy", "service": "portfolio-api", "version": "0.2.0"}


@app.get("/api/health")
def api_health():
    return {"status": "ok", "service": "blackroad-ventures-dashboard"}


# ─── Stripe webhook endpoint ─────────────────────────────────────────────────


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Receive and process Stripe webhook events."""
    try:
        from stripe_integration import PaymentService
    except ImportError:
        raise HTTPException(500, "Stripe integration not available")

    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    svc = PaymentService(stripe_client=None)
    try:
        ok, msg = svc.handle_webhook(body, sig_header, webhook_secret)
        if ok:
            return {"status": "ok", "message": msg}
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    finally:
        svc.close()


# ─── Pi fleet status endpoint ────────────────────────────────────────────────


@app.get("/api/pi-fleet")
def pi_fleet_status():
    """Return Pi fleet node status."""
    try:
        sys_path = os.path.join(os.path.dirname(__file__), "..", "deploy")
        import sys as _sys
        _sys.path.insert(0, sys_path)
        from pi_deploy import PiFleetManager
    except ImportError:
        return {"nodes": [], "message": "Pi fleet manager not available"}

    mgr = PiFleetManager()
    try:
        nodes = mgr.store.list_nodes()
        return {
            "nodes": [
                {
                    "id": n.id,
                    "hostname": n.hostname,
                    "ip": n.ip_address,
                    "port": n.port,
                    "role": n.role,
                    "status": n.status,
                    "services": n.services,
                }
                for n in nodes
            ],
            "routes": mgr.get_routing_table(),
        }
    finally:
        mgr.close()
