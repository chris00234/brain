#!/opt/homebrew/bin/python3
"""Backfill service dependency graph from docker-compose + nginx configs.

Parses:
  - docker-compose.yml: service names, ports, depends_on, networks
  - nginx conf.d/*.conf: server_name → proxy_pass mappings

Creates Service nodes with DEPENDS_ON and PROXIES edges in Neo4j.

Usage:
  backfill_services.py              # dry-run
  backfill_services.py --apply      # write to Neo4j
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain_core"))

SERVER_DIR = Path("/Users/chrischo/server")
NGINX_CONF_DIR = SERVER_DIR / "nginx" / "conf.d"

# Native services (not in Docker)
NATIVE_SERVICES = {
    "chromadb": {"port": 8000, "host": "127.0.0.1"},
    "ollama": {"port": 11434, "host": "127.0.0.1"},
    "neo4j": {"port": 7687, "host": "127.0.0.1"},
    "brain-server": {"port": 8791, "host": "127.0.0.1"},
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_docker_compose(path: Path) -> list[dict]:
    """Extract services, ports, depends_on from a docker-compose.yml."""
    try:
        import yaml
    except ImportError:
        # Fallback: regex parsing for simple cases
        return _parse_compose_regex(path)

    try:
        data = yaml.safe_load(path.read_text())
    except Exception:
        return _parse_compose_regex(path)

    services = []
    svc_dict = data.get("services", {})
    if not svc_dict:
        return []

    dir_name = path.parent.name
    for name, config in svc_dict.items():
        if not isinstance(config, dict):
            continue
        ports = []
        for p in config.get("ports", []):
            port_str = str(p).split(":")[0]
            try:
                ports.append(int(port_str))
            except ValueError:
                pass

        depends = config.get("depends_on", [])
        if isinstance(depends, dict):
            depends = list(depends.keys())

        services.append(
            {
                "name": name,
                "dir": dir_name,
                "image": config.get("image", ""),
                "ports": ports,
                "depends_on": depends,
                "networks": list(config.get("networks", {}).keys())
                if isinstance(config.get("networks"), dict)
                else config.get("networks", []),
                "container": True,
            }
        )
    return services


def _parse_compose_regex(path: Path) -> list[dict]:
    """Regex fallback for docker-compose parsing (no PyYAML)."""
    text = path.read_text()
    services = []
    dir_name = path.parent.name

    # Find service blocks (indented under services:)
    in_services = False
    current_service = None
    current_ports = []
    current_depends = []
    in_depends_on = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        # Top-level service name (2-space indent, no dash)
        if re.match(r"^  [a-zA-Z]", line) and ":" in stripped:
            if current_service:
                services.append(
                    {
                        "name": current_service,
                        "dir": dir_name,
                        "image": "",
                        "ports": current_ports,
                        "depends_on": current_depends,
                        "networks": [],
                        "container": True,
                    }
                )
            current_service = stripped.rstrip(":").strip()
            current_ports = []
            current_depends = []
            in_depends_on = False
        # Track depends_on section
        if stripped == "depends_on:":
            in_depends_on = True
            continue
        if re.match(r"^    [a-z]", line) and ":" in stripped and not stripped.startswith("-"):
            in_depends_on = False
        # Port mapping
        port_match = re.match(r'\s*-\s*["\']?(\d+):\d+', line)
        if port_match:
            current_ports.append(int(port_match.group(1)))
            in_depends_on = False
        # depends_on entry
        if in_depends_on:
            dep_match = re.match(r"\s*-\s*(\w[\w-]*)", line)
            if dep_match:
                current_depends.append(dep_match.group(1))

    if current_service:
        services.append(
            {
                "name": current_service,
                "dir": dir_name,
                "image": "",
                "ports": current_ports,
                "depends_on": current_depends,
                "networks": [],
                "container": True,
            }
        )
    return services


def parse_nginx_configs(conf_dir: Path) -> list[dict]:
    """Extract proxy_pass targets from nginx server blocks."""
    proxies = []
    for conf in conf_dir.glob("*.conf"):
        text = conf.read_text()
        server_names = re.findall(r"server_name\s+([^;]+);", text)
        proxy_targets = re.findall(r"proxy_pass\s+https?://([^/;:\s]+)", text)

        for target in proxy_targets:
            # Clean target — could be container name or IP
            target_clean = target.strip().rstrip("/")
            hostname = server_names[0].strip().split()[0] if server_names else conf.stem

            proxies.append(
                {
                    "hostname": hostname,
                    "target": target_clean,
                    "conf_file": conf.name,
                }
            )
    return proxies


def backfill(apply: bool = False):
    # Collect all services
    all_services = {}

    # Docker services
    for compose_file in SERVER_DIR.rglob("docker-compose.yml"):
        if compose_file.parent == SERVER_DIR / "rag":
            continue  # ChromaDB/Ollama are native now
        for svc in parse_docker_compose(compose_file):
            all_services[svc["name"]] = svc

    # Native services
    for name, info in NATIVE_SERVICES.items():
        all_services[name] = {
            "name": name,
            "dir": name,
            "image": "",
            "ports": [info["port"]],
            "depends_on": [],
            "networks": [],
            "container": False,
        }

    # Nginx proxies
    proxies = parse_nginx_configs(NGINX_CONF_DIR) if NGINX_CONF_DIR.exists() else []

    print(f"Found {len(all_services)} services, {len(proxies)} nginx proxy rules")

    if not apply:
        print("\n[DRY RUN] Services:")
        for name, svc in sorted(all_services.items()):
            deps = svc.get("depends_on", [])
            ports = svc.get("ports", [])
            native = " (native)" if not svc.get("container") else ""
            print(f"  {name}{native}: ports={ports} depends_on={deps}")
        print("\nProxy rules:")
        for p in proxies:
            print(f"  {p['hostname']} → {p['target']} ({p['conf_file']})")
        print("\nRun with --apply to write to Neo4j")
        return

    from neo4j_client import run_write

    now = _now_iso()

    # Create Service nodes
    for name, svc in all_services.items():
        port = svc["ports"][0] if svc["ports"] else 0
        run_write(
            "MERGE (s:Entity {name: $name}) "
            "ON CREATE SET s.id = 'svc_' + left(randomUUID(), 12), "
            "  s.entity_type = 'service', s.first_seen_at = $now, "
            "  s.last_seen_at = $now, s.mention_count = 1, "
            "  s.memory_class = 'permanent', s.port = $port, "
            "  s.container = $container "
            "ON MATCH SET s.last_seen_at = $now, "
            "  s.entity_type = 'service', s.memory_class = 'permanent', "
            "  s.port = $port, s.container = $container, "
            "  s.mention_count = s.mention_count + 1",
            {"name": name, "now": now, "port": port, "container": svc.get("container", True)},
        )

    # Create DEPENDS_ON edges
    dep_count = 0
    for name, svc in all_services.items():
        for dep in svc.get("depends_on", []):
            if dep in all_services:
                run_write(
                    "MATCH (s:Entity {name: $src}), (t:Entity {name: $tgt}) "
                    "MERGE (s)-[r:RELATES_TO {relationship: 'depends_on'}]->(t) "
                    "ON CREATE SET r.weight = 0.8, r.co_occurrence_count = 1, "
                    "  r.created_at = $now "
                    "ON MATCH SET r.co_occurrence_count = r.co_occurrence_count + 1",
                    {"src": name, "tgt": dep, "now": now},
                )
                dep_count += 1

    # Create PROXIES edges from nginx
    proxy_count = 0
    for p in proxies:
        target = p["target"]
        hostname = p["hostname"]
        if target in all_services:
            run_write(
                "MATCH (n:Entity {name: 'nginx'}), (t:Entity {name: $target}) "
                "MERGE (n)-[r:RELATES_TO {relationship: 'proxies'}]->(t) "
                "ON CREATE SET r.weight = 0.9, r.co_occurrence_count = 1, "
                "  r.created_at = $now, r.hostname = $hostname "
                "ON MATCH SET r.co_occurrence_count = r.co_occurrence_count + 1, "
                "  r.hostname = $hostname",
                {"target": target, "hostname": hostname, "now": now},
            )
            proxy_count += 1

    print(
        f"Created {len(all_services)} Service nodes, {dep_count} DEPENDS_ON edges, {proxy_count} PROXIES edges"
    )


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv)
