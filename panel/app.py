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
CORS(app, origins=[f"http://localhost:{os.environ.get('PANEL_PORT', '9000')}", "http://127.0.0.1:*"])

CONFIG_PATH = os.environ.get("PANEL_CONFIG", os.path.join(os.path.dirname(__file__), "config.json"))

# ---- 认证配置 ----
# 通过环境变量 PANEL_USER/PANEL_PASS 设置账号密码
PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASS = os.environ.get("PANEL_PASS", "changeme")

# 白名单命令前缀（只允许执行这些开头的命令）
ALLOWED_CMD_PREFIXES = (
    "free", "df", "ls", "cat /proc", "uptime", "hostname",
    "docker ps", "docker stats", "docker inspect", "docker logs",
    "systemctl status", "systemctl is-active",
    "ip a", "ip addr", "ss -", "netstat",
    "cat /etc/os-release", "uname", "whoami", "date",
)

def is_command_safe(command: str) -> bool:
    """白名单校验：只允许预定义的安全命令"""
    cmd = command.strip().lower()

    # 禁止 shell 元字符: 白名单只匹配前缀, 若不拦住连接符,
    # "free; cat /etc/shadow" 这类命令会以 free 开头而整条被放行
    for meta in (";", "&&", "||", "|", "`", "$(", "\n", "\r", ">"):
        if meta in cmd:
            return False

    # 先做基础黑名单拦截（双保险）
    blocked = ("rm -rf", "mkfs", "dd if=", "shutdown", "reboot", "poweroff",
              ":(){", "fork bomb", "wget http", "curl http", ">/dev/sd")
    if any(b in cmd for b in blocked):
        return False
    # 白名单匹配
    return any(cmd.startswith(p) for p in ALLOWED_CMD_PREFIXES)

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
            ["ssh", "-o", f"ConnectTimeout={timeout}",
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

def _check_auth():
    """简单 Basic Auth 校验"""
    auth = request.authorization
    if not auth or auth.username != PANEL_USER or auth.password != PANEL_PASS:
        return False
    return True

def require_auth(f):
    """装饰器: 要求 API 请求必须通过 Basic Auth 认证"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_auth():
            return jsonify({"ok": False, "error": "未授权: 需要 Basic Auth 认证"}), 401
        return f(*args, **kwargs)
    return decorated

# ---- 路由 ----

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
@require_auth
def api_status():
    return jsonify(collect_all_status())

@app.route("/api/node/<node_name>/action", methods=["POST"])
@require_auth
def node_action(node_name):
    data = request.json or {}
    action = data.get("action", "")
    config = load_config()
    node = next((n for n in config["nodes"] if n["name"] == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "节点未找到"}), 404

    # 危险操作二次确认
    dangerous_actions = ["reboot", "shutdown"]
    if action in dangerous_actions:
        confirm = data.get("confirm", False)
        if not confirm:
            return jsonify({
                "ok": False,
                "confirm_required": True,
                "error": f"危险操作 '{action}' 需要二次确认, 请设置 confirm=true"
            }), 400

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

@app.route("/api/service/<node_name>/<svc_name>/<action>", methods=["POST"])
@require_auth
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
@require_auth
def exec_command():
    data = request.json or {}
    node_name = data.get("node")
    command = data.get("command", "")

    config = load_config()
    node = next((n for n in config["nodes"] if n["name"] == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "节点未找到"}), 404

    # 安全限制: 白名单机制（只允许预定义的安全命令）
    if not is_command_safe(command):
        return jsonify({"ok": False, "error": "命令不在白名单中, 已拒绝 (仅允许: free/df/ls/docker ps/uptime 等只读命令)"}), 403

    result = run_ssh(node["ip"], command, timeout=30)
    return jsonify({"ok": result["ok"], "output": result["stdout"], "error": result["stderr"]})

@app.route("/api/topology")
@require_auth
def topology():
    config = load_config()
    return jsonify(config)

if __name__ == "__main__":
    port = int(os.environ.get("PANEL_PORT", 9000))
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    print(f"OneCloud Cluster Panel 启动中...")
    print(f"  http://{host}:{port}")
    app.run(host=host, port=port, debug=False)