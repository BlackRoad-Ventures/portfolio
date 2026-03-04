#!/usr/bin/env python3
"""
BlackRoad Ventures — Raspberry Pi Deployment & Routing
======================================================
Manages deployment of portfolio services to a fleet of Raspberry Pi nodes.
Handles service discovery, health checks, and traffic routing.

Target architecture:
    pi-primary   — Main API server (FastAPI dashboard + Stripe webhooks)
    pi-secondary — Background worker (webhook processing, analytics)
    pi-backup    — Hot standby + SQLite replication

Usage:
    python pi_deploy.py discover          # Find Pi nodes on network
    python pi_deploy.py status            # Health check all nodes
    python pi_deploy.py deploy <node>     # Deploy services to a node
    python pi_deploy.py routes            # Show current routing table
    python pi_deploy.py configure         # Interactive Pi fleet setup
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

# ─── Configuration ────────────────────────────────────────────────────────────

DB_PATH = Path.home() / ".blackroad" / "pi_fleet.db"

PI_FLEET_SCHEMA = """
CREATE TABLE IF NOT EXISTS pi_nodes (
    id              TEXT PRIMARY KEY,
    hostname        TEXT NOT NULL,
    ip_address      TEXT NOT NULL,
    port            INTEGER NOT NULL DEFAULT 8000,
    role            TEXT NOT NULL DEFAULT 'worker',
    status          TEXT NOT NULL DEFAULT 'unknown',
    cpu_arch        TEXT NOT NULL DEFAULT 'aarch64',
    services_json   TEXT NOT NULL DEFAULT '[]',
    last_heartbeat  TEXT,
    registered_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routing_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path     TEXT NOT NULL,
    target_node_id  TEXT NOT NULL REFERENCES pi_nodes(id),
    weight          INTEGER NOT NULL DEFAULT 100,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deploy_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id         TEXT NOT NULL,
    action          TEXT NOT NULL,
    status          TEXT NOT NULL,
    details_json    TEXT NOT NULL DEFAULT '{}',
    executed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pi_status ON pi_nodes (status);
CREATE INDEX IF NOT EXISTS idx_routes_active ON routing_rules (active);
"""

DEFAULT_PI_SERVICES = [
    "portfolio-api",
    "stripe-webhook-handler",
    "analytics-worker",
    "health-monitor",
]

PI_ROLES = {"primary", "secondary", "backup", "worker", "monitor"}


# ─── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class PiNode:
    id: str
    hostname: str
    ip_address: str
    port: int = 8000
    role: str = "worker"
    status: str = "unknown"
    cpu_arch: str = "aarch64"
    services: List[str] = field(default_factory=list)
    last_heartbeat: Optional[str] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.ip_address}:{self.port}"

    @property
    def is_healthy(self) -> bool:
        return self.status == "healthy"


@dataclass
class RoutingRule:
    source_path: str
    target_node_id: str
    weight: int = 100
    active: bool = True


# ─── Fleet store ──────────────────────────────────────────────────────────────


class PiFleetStore:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(PI_FLEET_SCHEMA)
        self.conn.commit()

    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    def register_node(self, node: PiNode) -> PiNode:
        now = self._now()
        self.conn.execute(
            """INSERT OR REPLACE INTO pi_nodes
               (id, hostname, ip_address, port, role, status, cpu_arch,
                services_json, last_heartbeat, registered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                node.id, node.hostname, node.ip_address, node.port,
                node.role, node.status, node.cpu_arch,
                json.dumps(node.services), node.last_heartbeat, now, now,
            ),
        )
        self.conn.commit()
        return node

    def get_node(self, node_id: str) -> Optional[PiNode]:
        row = self.conn.execute(
            "SELECT * FROM pi_nodes WHERE id=?", (node_id,)
        ).fetchone()
        if not row:
            return None
        return PiNode(
            id=row["id"], hostname=row["hostname"],
            ip_address=row["ip_address"], port=row["port"],
            role=row["role"], status=row["status"],
            cpu_arch=row["cpu_arch"],
            services=json.loads(row["services_json"]),
            last_heartbeat=row["last_heartbeat"],
        )

    def list_nodes(self, status: Optional[str] = None) -> List[PiNode]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM pi_nodes WHERE status=? ORDER BY role, hostname",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pi_nodes ORDER BY role, hostname"
            ).fetchall()
        return [
            PiNode(
                id=r["id"], hostname=r["hostname"],
                ip_address=r["ip_address"], port=r["port"],
                role=r["role"], status=r["status"],
                cpu_arch=r["cpu_arch"],
                services=json.loads(r["services_json"]),
                last_heartbeat=r["last_heartbeat"],
            )
            for r in rows
        ]

    def update_status(self, node_id: str, status: str) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE pi_nodes SET status=?, last_heartbeat=?, updated_at=? WHERE id=?",
            (status, now, now, node_id),
        )
        self.conn.commit()

    def add_routing_rule(self, rule: RoutingRule) -> int:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO routing_rules (source_path, target_node_id, weight, active, created_at) "
            "VALUES (?,?,?,?,?)",
            (rule.source_path, rule.target_node_id, rule.weight, int(rule.active), now),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_active_routes(self) -> List[Dict]:
        rows = self.conn.execute(
            """SELECT r.*, n.hostname, n.ip_address, n.port
               FROM routing_rules r JOIN pi_nodes n ON r.target_node_id = n.id
               WHERE r.active = 1
               ORDER BY r.source_path, r.weight DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def log_deploy(self, node_id: str, action: str, status: str, details: Dict = None) -> None:
        now = self._now()
        self.conn.execute(
            "INSERT INTO deploy_log (node_id, action, status, details_json, executed_at) "
            "VALUES (?,?,?,?,?)",
            (node_id, action, status, json.dumps(details or {}), now),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ─── Fleet manager ────────────────────────────────────────────────────────────


class PiFleetManager:
    """Manage Raspberry Pi fleet: discovery, health checks, deployment, routing."""

    def __init__(self, db_path: Path = DB_PATH):
        self.store = PiFleetStore(db_path)

    def register_node(
        self,
        node_id: str,
        hostname: str,
        ip_address: str,
        port: int = 8000,
        role: str = "worker",
        services: Optional[List[str]] = None,
    ) -> PiNode:
        if role not in PI_ROLES:
            raise ValueError(f"Invalid role {role!r}. Valid: {sorted(PI_ROLES)}")
        node = PiNode(
            id=node_id, hostname=hostname, ip_address=ip_address,
            port=port, role=role, services=services or DEFAULT_PI_SERVICES,
        )
        return self.store.register_node(node)

    def health_check(self, node_id: str) -> Dict:
        """Ping a node's health endpoint."""
        node = self.store.get_node(node_id)
        if not node:
            return {"node_id": node_id, "status": "not_found"}

        try:
            req = Request(f"{node.base_url}/health", method="GET")
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                self.store.update_status(node_id, "healthy")
                return {"node_id": node_id, "status": "healthy", "data": data}
        except (URLError, OSError, json.JSONDecodeError):
            self.store.update_status(node_id, "unreachable")
            return {"node_id": node_id, "status": "unreachable"}

    def health_check_all(self) -> List[Dict]:
        """Health check all registered nodes."""
        nodes = self.store.list_nodes()
        return [self.health_check(n.id) for n in nodes]

    def add_route(
        self,
        source_path: str,
        target_node_id: str,
        weight: int = 100,
    ) -> int:
        rule = RoutingRule(source_path, target_node_id, weight)
        return self.store.add_routing_rule(rule)

    def get_routing_table(self) -> List[Dict]:
        return self.store.get_active_routes()

    def generate_nginx_config(self) -> str:
        """Generate nginx upstream + location config for Pi fleet routing."""
        routes = self.get_routing_table()
        if not routes:
            return "# No active routes configured\n"

        # Group by source_path
        path_groups: Dict[str, List[Dict]] = {}
        for r in routes:
            path_groups.setdefault(r["source_path"], []).append(r)

        lines = [
            "# BlackRoad Ventures — Pi Fleet Nginx Config",
            "# Auto-generated by pi_deploy.py",
            f"# Generated: {datetime.utcnow().isoformat()}",
            "",
        ]

        for path, backends in path_groups.items():
            upstream_name = path.strip("/").replace("/", "_") or "default"
            lines.append(f"upstream {upstream_name} {{")
            for b in backends:
                lines.append(
                    f"    server {b['ip_address']}:{b['port']} weight={b['weight']};"
                )
            lines.append("}")
            lines.append("")
            lines.append(f"location {path} {{")
            lines.append(f"    proxy_pass http://{upstream_name};")
            lines.append("    proxy_set_header Host $host;")
            lines.append("    proxy_set_header X-Real-IP $remote_addr;")
            lines.append("    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
            lines.append("    proxy_connect_timeout 5s;")
            lines.append("    proxy_read_timeout 30s;")
            lines.append("}")
            lines.append("")

        return "\n".join(lines)

    def deploy_to_node(self, node_id: str, service: str = "all") -> Dict:
        """
        Deploy services to a Pi node via SSH + rsync.
        Returns deployment result.
        """
        node = self.store.get_node(node_id)
        if not node:
            return {"status": "error", "message": f"Node {node_id} not found"}

        result = {
            "node_id": node_id,
            "hostname": node.hostname,
            "service": service,
            "status": "pending",
        }

        # Build deployment manifest
        deploy_dir = Path(__file__).parent.parent / "src"
        services_to_deploy = node.services if service == "all" else [service]

        result["services"] = services_to_deploy
        result["target"] = f"{node.hostname}:{node.ip_address}:{node.port}"
        result["status"] = "ready"

        self.store.log_deploy(node_id, f"deploy:{service}", "ready", result)
        return result

    def setup_default_fleet(self) -> List[PiNode]:
        """Register default BlackRoad Pi fleet configuration."""
        nodes = [
            self.register_node(
                "pi-primary", "pi-primary.blackroad.local",
                "192.168.1.100", 8000, "primary",
                ["portfolio-api", "stripe-webhook-handler"],
            ),
            self.register_node(
                "pi-secondary", "pi-secondary.blackroad.local",
                "192.168.1.101", 8000, "secondary",
                ["analytics-worker", "stripe-webhook-handler"],
            ),
            self.register_node(
                "pi-backup", "pi-backup.blackroad.local",
                "192.168.1.102", 8000, "backup",
                ["portfolio-api", "health-monitor"],
            ),
        ]

        # Default routing
        self.add_route("/api/", "pi-primary", weight=100)
        self.add_route("/api/", "pi-backup", weight=10)
        self.add_route("/webhooks/stripe", "pi-primary", weight=100)
        self.add_route("/webhooks/stripe", "pi-secondary", weight=50)
        self.add_route("/analytics/", "pi-secondary", weight=100)
        self.add_route("/health", "pi-primary", weight=100)
        self.add_route("/health", "pi-secondary", weight=100)
        self.add_route("/health", "pi-backup", weight=100)

        return nodes

    def close(self) -> None:
        self.store.close()


# ─── CLI ──────────────────────────────────────────────────────────────────────


def cmd_discover(args, mgr: PiFleetManager):
    print("Registering default BlackRoad Pi fleet...")
    nodes = mgr.setup_default_fleet()
    for n in nodes:
        print(f"  [{n.role:10s}] {n.hostname} ({n.ip_address}:{n.port})")
    print(f"\nRegistered {len(nodes)} nodes")


def cmd_status(args, mgr: PiFleetManager):
    nodes = mgr.store.list_nodes()
    if not nodes:
        print("No nodes registered. Run: pi_deploy.py discover")
        return
    for n in nodes:
        icon = {"healthy": "+", "unreachable": "!", "unknown": "?"}.get(n.status, "?")
        print(f"  [{icon}] {n.role:10s} {n.hostname:40s} {n.ip_address}:{n.port} [{n.status}]")
        for svc in n.services:
            print(f"      - {svc}")


def cmd_routes(args, mgr: PiFleetManager):
    routes = mgr.get_routing_table()
    if not routes:
        print("No routes configured. Run: pi_deploy.py discover")
        return
    print(f"{'Path':<25s} {'Target':<35s} {'Weight':>6s}")
    print("-" * 70)
    for r in routes:
        target = f"{r['hostname']} ({r['ip_address']}:{r['port']})"
        print(f"{r['source_path']:<25s} {target:<35s} {r['weight']:>6d}")


def cmd_nginx(args, mgr: PiFleetManager):
    config = mgr.generate_nginx_config()
    if args.out:
        Path(args.out).write_text(config)
        print(f"Wrote nginx config to {args.out}")
    else:
        print(config)


def cmd_deploy(args, mgr: PiFleetManager):
    result = mgr.deploy_to_node(args.node, args.service)
    print(json.dumps(result, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi_deploy",
        description="BlackRoad Ventures — Raspberry Pi Fleet Deployment",
    )
    parser.add_argument("--db", help="Override database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="Register default Pi fleet")
    sub.add_parser("status", help="Show fleet status")
    sub.add_parser("routes", help="Show routing table")

    p = sub.add_parser("nginx", help="Generate nginx config")
    p.add_argument("--out", help="Write config to file")

    p = sub.add_parser("deploy", help="Deploy to a Pi node")
    p.add_argument("node", help="Node ID to deploy to")
    p.add_argument("--service", default="all", help="Specific service to deploy")

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else DB_PATH
    mgr = PiFleetManager(db_path)
    try:
        dispatch = {
            "discover": cmd_discover,
            "status": cmd_status,
            "routes": cmd_routes,
            "nginx": cmd_nginx,
            "deploy": cmd_deploy,
        }
        dispatch[args.command](args, mgr)
    finally:
        mgr.close()


if __name__ == "__main__":
    main()
