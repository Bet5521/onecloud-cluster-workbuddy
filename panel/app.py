#!/usr/bin/env python3
# ============================================================
#  OneCloud Cluster Control Panel
#  Flask Web 应用 - 集群状态监控和快捷操作
# ============================================================

import os
import json
import subprocess
import threading
import time
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CONFIG_PATH = os.environ.get("PANEL_CONFIG", os.path.join(os.path.dirname(__file__), "config.json"))

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return {
            "cluster_name": "OneCloud Cluster",
            "version": "1.0.0",
            "nodes": []
        }

def run_ssh(ip, command, timeout=3):
    try:
        result = subprocess.run(
            ["ssh", "-o", f"ConnectTimeout={timeout}", "-o", "StrictHostKeyChecking=no",
             f"root@{ip}", command],
            capture_output=True, text=True, timeout=timeout + 2
        )
        return {"ok": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}

def get_system_info(ip):
    result = run_ssh(ip, """
        echo "LOAD=$(cat /proc/loadavg | cut -d' ' -f1)"
        echo "UPTIME=$(uptime -p)"
        echo "MEM=$(free -m | awk 'NR==2{print $3"/"$2}')"
        echo "SWAP=$(free -m | awk 'NR==3{print $3"/"$2}')"
        echo "DISK=$(df -h /mnt/sd 2>/dev/null | awk 'NR==2{print $4"/"$2}' || df -h / | awk 'NR==2{print $4"/"$2}')"
    """)
    info = {}
    if result["ok"]:
        for line in result["stdout"].split("\n"):
            if "=" in line:
                key, val = line.split("=", 1)
                info[key.strip()] = val.strip()
    return info

def get_docker_containers(ip):
    result = run_ssh(ip, "docker ps -a --format '{{.Names}}|{{.Status}}|{{.RunningFor}}|{{.Ports}}' 2>/dev/null")
    containers = []
    if result["ok"]:
        for line in result["stdout"].split("\n"):
            if line.strip() and "|" in line:
                parts = line.split("|", 3)
                containers.append({
                    "name": parts[0],
                    "status": parts[1],
                    "running": "Up" in parts[1],
                    "since": parts[2],
                    "ports": parts[3] if len(parts) > 3 else ""
                })
    return containers

def get_service_status(ip, service):
    if service.get("container"):
        result = run_ssh(ip, f"docker inspect --format='{{{{.State.Status}}}}' {service['name']} 2>/dev/null")
        return {"running": result["ok"] and result["stdout"] == "running", "type": "container"}
    else:
        result = run_ssh(ip, f"systemctl is-active {service['name']} 2>/dev/null")
        if result["ok"] and result["stdout"] == "active":
            return {"running": True, "type": "systemd"}
        result2 = run_ssh(ip, f"pgrep -f '{service['name']}' 2>/dev/null")
        return {"running": result2["ok"] and result2["stdout"] != "", "type": "native"}

def collect_all_status():
    config = load_config()
    status = {
        "cluster_name": config["cluster_name"],
        "version": config["version"],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "nodes": []
    }

    for node in config["nodes"]:
        node_status = {
            "name": node["name"],
            "display_name": node["display_name"],
            "role": node["role"],
            "ip": node["ip"],
            "wg_ip": node["wg_ip"],
            "color": node["color"],
            "online": False,
            "system": {},
            "containers": [],
            "services": []
        }

        # 检查节点在线
        ssh_result = run_ssh(node["ip"], "echo ok")
        node_status["online"] = ssh_result["ok"]

        if node_status["online"]:
            node_status["system"] = get_system_info(node["ip"])
            node_status["containers"] = get_docker_containers(node["ip"])

            for svc in node.get("services", []):
                svc_status = get_service_status(node["ip"], svc)
                node_status["services"].append({
                    "name": svc["name"],
                    "display": svc.get("display", svc["name"]),
                    "running": svc_status["running"],
                    "type": svc_status["type"]
                })

        status["nodes"].append(node_status)

    return status

# ---- 路由 ----

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify(collect_all_status())

@app.route("/api/node/<node_name>/action", methods=["POST"])
def node_action(node_name):
    data = request.json or {}
    action = data.get("action", "")
    config = load_config()
    node = next((n for n in config["nodes"] if n["name"] == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "节点未找到"}), 404

    actions = {
        "reboot": f"reboot",
        "shutdown": f"shutdown -h now",
        "docker_restart": f"cd /mnt/sd/srv/{node_name} && docker-compose restart || docker compose restart",
        "docker_up": f"cd /mnt/sd/srv/{node_name} && docker-compose up -d || docker compose up -d",
        "docker_down": f"cd /mnt/sd/srv/{node_name} && docker-compose down || docker compose down",
        "docker_pull": f"cd /mnt/sd/srv/{node_name} && docker-compose pull || docker compose pull",
    }

    if action in actions:
        result = run_ssh(node["ip"], actions[action], timeout=10)
        return jsonify({"ok": result["ok"], "output": result["stdout"], "error": result["stderr"]})

    return jsonify({"ok": False, "error": f"未知操作: {action}"}), 400

@app.route("/api/service/<node_name>/<svc_name>/<action>")
def service_action(node_name, svc_name, action):
    config = load_config()
    node = next((n for n in config["nodes"] if n["name"] == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "节点未找到"}), 404

    is_container = any(s["name"] == svc_name and s.get("container", True) for s in node.get("services", []))

    commands = {
        "start": f"docker start {svc_name}" if is_container else f"systemctl start {svc_name}",
        "stop": f"docker stop {svc_name}" if is_container else f"systemctl stop {svc_name}",
        "restart": f"docker restart {svc_name}" if is_container else f"systemctl restart {svc_name}",
        "logs": f"docker logs --tail 50 {svc_name}" if is_container else f"journalctl -u {svc_name} --no-pager -n 50",
    }

    if action in commands:
        result = run_ssh(node["ip"], commands[action], timeout=10)
        return jsonify({"ok": result["ok"], "output": result["stdout"].split("\n")[-20:], "error": result["stderr"].split("\n")[-10:]})

    return jsonify({"ok": False, "error": f"未知操作: {action}"}), 400

@app.route("/api/exec", methods=["POST"])
def exec_command():
    data = request.json or {}
    node_name = data.get("node")
    command = data.get("command", "")

    config = load_config()
    node = next((n for n in config["nodes"] if n["name"] == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "节点未找到"}), 404

    # 安全限制: 只允许非破坏性命令
    dangerous = ["rm -rf", "mkfs", "dd if=", "shutdown", "reboot", "poweroff"]
    for d in dangerous:
        if d in command.lower():
            return jsonify({"ok": False, "error": "危险命令被拒绝"}), 403

    result = run_ssh(node["ip"], command, timeout=30)
    return jsonify({"ok": result["ok"], "output": result["stdout"], "error": result["stderr"]})

@app.route("/api/topology")
def topology():
    config = load_config()
    return jsonify(config)

if __name__ == "__main__":
    port = int(os.environ.get("PANEL_PORT", 9000))
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    print(f"OneCloud Cluster Panel 启动中...")
    print(f"  http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
