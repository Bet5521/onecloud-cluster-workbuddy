#!/usr/bin/env python3
# ============================================================
#  OneCloud Cluster Control Panel
#  Flask Web 应用 - 集群状态监控和快捷操作
# ============================================================

import os
import sys
import json
import re
import secrets
import subprocess
import threading
import time
from functools import wraps
from flask import (Flask, jsonify, render_template, request, session,
                   redirect, url_for)
from flask_cors import CORS

app = Flask(__name__)

# ---- 会话密钥: 用于登录态签名 ----
# 优先读 PANEL_SECRET; 未设置时生成随机值 (重启后旧会话失效, 需重新登录)。
# 生产环境建议在 /etc/onecloud/panel.env 里固定一个值。
app.secret_key = os.environ.get("PANEL_SECRET") or secrets.token_hex(32)

CONFIG_PATH = os.environ.get("PANEL_CONFIG", os.path.join(os.path.dirname(__file__), "config.json"))

# ---- 服务名白名单 ----
# config.json 是磁盘上的可写文件, 往里拼 shell 命令前必须先校验服务名,
# 否则篡改配置文件就等于拿到远程命令执行入口。
_SAFE_SVC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# ---- 认证配置 ----
# 通过环境变量 PANEL_USER/PANEL_PASS 设置账号密码
PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASS = os.environ.get("PANEL_PASS", "")

# 未显式配置密码时: 生成一次性随机密码并打印到 stderr,
# 避免"默认 admin/changeme"这种开箱即被扫的弱口令。
PANEL_PASS_GENERATED = False
if not PANEL_PASS:
    PANEL_PASS = secrets.token_urlsafe(12)
    PANEL_PASS_GENERATED = True

# ---- CORS ----
# 面板的设计用途是从同网段其它机器访问, 因此 origins 必须包含实际访问地址。
# 只放行 localhost/127.0.0.1 会让经隧道域名或 WireGuard IP 访问的浏览器
# 在发出请求前就被拦掉 (CORS 预检失败)。
_local_port = os.environ.get("PANEL_PORT", "9000")
_origins = [
    f"http://localhost:{_local_port}",
    f"http://127.0.0.1:{_local_port}",
]
# 运行时实际访问地址 (由 install-service.sh 写入 PANEL_URL_HOST/PORT)
_url_host = os.environ.get("PANEL_URL_HOST", "").strip()
_url_port = os.environ.get("PANEL_URL_PORT", _local_port).strip()
if _url_host:
    _origins.append(f"http://{_url_host}:{_url_port}")
    _origins.append(f"https://{_url_host}:{_url_port}")
    _origins.append(f"http://{_url_host}")
    _origins.append(f"https://{_url_host}")
# 额外来源 (逗号分隔), 供 Cloudflare Tunnel 域名等场景补充
for _o in os.environ.get("PANEL_CORS_ORIGINS", "").split(","):
    _o = _o.strip()
    if _o:
        _origins.append(_o)
CORS(app, origins=_origins, supports_credentials=True)

# ---- 安全响应头 ----
@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

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
            "version": "1.6.0",
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

def get_system_info(ip, data_root="/mnt/sd"):
    # data_root 来自面板配置 (该节点 /etc/onecloud/install.conf 的 DATA_ROOT),
    # 不再硬编码 /mnt/sd —— 无 SD 卡回退 /opt/onecloud 的节点否则会显示错的容量
    droot = data_root or "/mnt/sd"
    result = run_ssh(ip, """
        echo "LOAD=$(cat /proc/loadavg | cut -d' ' -f1)"
        echo "UPTIME=$(uptime -p)"
        echo "MEM=$(free -m | awk 'NR==2{print $3"/"$2}')"
        echo "SWAP=$(free -m | awk 'NR==3{print $3"/"$2}')"
        echo "DISK=$(df -h '""" + droot + """' 2>/dev/null | awk 'NR==2{print $4"/"$2}' || df -h / | awk 'NR==2{print $4"/"$2}')"
        echo "DATA_ROOT=""" + droot + """
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
    """返回服务的安装态与运行态

    关键: installed 表示"应当安装"(清单声明 + 组网模式判定 + 安装方式),
    不是"探测到进程"。未安装的服务**不做任何 SSH 探测** —— 否则一个不存在
    的 systemd 单元/容器必然返回 false, 会被误报成"离线", 正是"不装
    WireGuard 时面板显示错误"的根因。
    """
    svc_type = "container" if service.get("container") else "native"
    if not service.get("installed", True):
        return {"installed": False, "running": False, "type": svc_type,
                "install": service.get("install", "")}

    # 服务名来自 config.json, 但仍按白名单过滤后再拼进 shell ——
    # 面板配置文件是可写文件, 不做校验就等于把远程命令执行入口留在配置里。
    name = str(service.get("name", ""))
    if not _SAFE_SVC_NAME.match(name):
        return {"installed": True, "running": False, "type": svc_type,
                "install": "", "error": "服务名非法"}

    if service.get("container"):
        result = run_ssh(ip, f"docker inspect --format='{{{{.State.Status}}}}' '{name}' 2>/dev/null")
        return {"installed": True, "running": result["ok"] and result["stdout"] == "running",
                "type": "container", "install": ""}
    result = run_ssh(ip, f"systemctl is-active '{name}' 2>/dev/null")
    if result["ok"] and result["stdout"] == "active":
        return {"installed": True, "running": True, "type": "systemd", "install": ""}
    result2 = run_ssh(ip, f"pgrep -f '{name}' 2>/dev/null")
    return {"installed": True, "running": result2["ok"] and result2["stdout"] != "",
            "type": "native", "install": ""}

def collect_all_status():
    config = load_config()
    status = {
        "cluster_name": config.get("cluster_name", "OneCloud Cluster"),
        "version": config.get("version", "unknown"),
        "network_mode": config.get("network_mode", "mixed"),
        "network_mode_label": config.get("network_mode_label", ""),
        "wg_enabled": config.get("wg_enabled", True),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "nodes": []
    }

    for node in config["nodes"]:
        node_status = {
            "name": node["name"],
            "display_name": node["display_name"],
            "role": node["role"],
            "ip": node["ip"],
            "wg_ip": node.get("wg_ip", ""),
            "color": node.get("color", "#9E9E9E"),
            "data_root": node.get("data_root", "/mnt/sd"),
            "online": False,
            "system": {},
            "containers": [],
            "services": [],
            "services_installed": 0,
            "services_total": 0,
            "services_running": 0,
        }

        # 检查节点在线
        try:
            ssh_result = run_ssh(node["ip"], "echo ok")
            node_status["online"] = ssh_result["ok"]
        except Exception:
            node_status["online"] = False

        if node_status["online"]:
            node_status["system"] = get_system_info(node["ip"], node_status["data_root"])
            node_status["containers"] = get_docker_containers(node["ip"])

        # 服务状态: 即使节点离线也照常计算安装态
        # (安装态来自清单, 不依赖 SSH; 这样离线节点的"未安装"仍能正确显示)
        for svc in node.get("services", []):
            if node_status["online"]:
                svc_status = get_service_status(node["ip"], svc)
            else:
                svc_status = {"installed": svc.get("installed", True), "running": False,
                              "type": "container" if svc.get("container") else "native",
                              "install": svc.get("install", "")}
            node_status["services"].append({
                "name": svc["name"],
                "display": svc.get("display", svc["name"]),
                "container": svc.get("container", True),
                "installed": svc_status["installed"],
                "running": svc_status["running"],
                "type": svc_status["type"],
                "install": svc_status.get("install", ""),
                "port": svc.get("port", 0),
            })
            node_status["services_total"] += 1
            if svc_status["installed"]:
                node_status["services_installed"] += 1
            if svc_status["running"]:
                node_status["services_running"] += 1

        status["nodes"].append(node_status)

    return status

def _check_auth():
    """认证校验: 优先看会话 (登录页写入), 兼容 HTTP Basic (脚本/curl 调用)"""
    if session.get("auth") is True:
        return True
    auth = request.authorization
    if auth and auth.username == PANEL_USER and auth.password == PANEL_PASS:
        return True
    return False

def require_auth(f):
    """装饰器: 要求 API 请求必须已登录

    未登录时返回 401 + JSON (API) 或重定向到登录页 (页面/浏览器直接访问)。
    前端 fetch 显式带 credentials, 因此会话 cookie 会随请求发送;
    即便会话过期返回 401, 前端也会跳转登录页而不是静默失败。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_auth():
            # API 调用 (fetch/XHR) 返回 JSON, 浏览器直接访问返回登录页
            wants_json = (
                request.path.startswith("/api/")
                or request.accept_mimetypes.best == "application/json"
                or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            )
            if wants_json:
                return jsonify({"ok": False, "error": "未授权: 请先登录",
                                "login_required": True}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

def require_csrf_header(f):
    """状态变更接口要求自定义头, 配合 SameSite=Lax 会话 cookie 防 CSRF

    跨站表单/图片/链接发起的请求无法设置自定义头, 因此在宽松的跨站场景下
    也能挡住非预期的状态变更。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-Requested-With") != "OneCloudPanel":
            return jsonify({"ok": False,
                            "error": "缺少 X-Requested-With 头, 已拒绝状态变更请求"}), 403
        return f(*args, **kwargs)
    return decorated

# ---- 路由 ----

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if _check_auth():
            return redirect(url_for("index"))
        return render_template("login.html", error=None,
                               version=load_config().get("version", "unknown"))
    # POST
    user = (request.form.get("username") or "").strip()
    pwd = request.form.get("password") or ""
    if user == PANEL_USER and pwd == PANEL_PASS:
        session["auth"] = True
        session.permanent = False
        nxt = request.args.get("next") or request.form.get("next") or "/"
        # 只允许站内跳转, 防止开放重定向
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        return redirect(nxt)
    return render_template("login.html", error="用户名或密码错误",
                           version=load_config().get("version", "unknown")), 401

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("auth", None)
    return redirect(url_for("login"))

@app.route("/")
@require_auth
def index():
    # 版本号由后端注入, 模板里不再硬编码 (避免改了版本忘了改页脚)
    return render_template("index.html", version=load_config().get("version", "unknown"))

@app.route("/api/status")
@require_auth
def api_status():
    return jsonify(collect_all_status())

@app.route("/api/node/<node_name>/action", methods=["POST"])
@require_auth
@require_csrf_header
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

    # docker 相关操作在节点数据根下执行 (来自面板配置, 不再硬编码 /mnt/sd)
    nroot = node.get("data_root", "/mnt/sd")
    compose_dir = f"{nroot}/srv/{node_name}"
    actions = {
        "reboot": "reboot",
        "shutdown": "shutdown -h now",
        "docker_restart": f"cd {compose_dir} && docker-compose restart || docker compose restart",
        "docker_up": f"cd {compose_dir} && docker-compose up -d || docker compose up -d",
        "docker_down": f"cd {compose_dir} && docker-compose down || docker compose down",
        "docker_pull": f"cd {compose_dir} && docker-compose pull || docker compose pull",
    }

    if action in actions:
        result = run_ssh(node["ip"], actions[action], timeout=10)
        return jsonify({"ok": result["ok"], "output": result["stdout"], "error": result["stderr"]})

    return jsonify({"ok": False, "error": f"未知操作: {action}"}), 400

@app.route("/api/service/<node_name>/<svc_name>/<action>", methods=["POST"])
@require_auth
@require_csrf_header
def service_action(node_name, svc_name, action):
    config = load_config()
    node = next((n for n in config["nodes"] if n["name"] == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "节点未找到"}), 404

    # 未安装的服务不允许启停 —— 否则会对一个不存在的单元执行 systemctl,
    # 报出让人误解的 "Unit not found"
    svc = next((s for s in node.get("services", []) if s["name"] == svc_name), None)
    if svc is None:
        return jsonify({"ok": False, "error": f"节点 {node_name} 未注册服务 {svc_name}"}), 404
    if not svc.get("installed", True):
        imode = svc.get("install", "")
        hint = ("该组件无自动安装实现, 请按文档手动安装"
                if imode == "manual" else "该组件未安装, 请先在节点上安装")
        return jsonify({"ok": False, "error": f"{svc_name} 未安装: {hint}",
                        "installed": False}), 409

    is_container = bool(svc.get("container", True))

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
@require_csrf_header
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

@app.route("/api/network", methods=["GET"])
@require_auth
def api_network():
    """组网模式与各节点的探测地址 (供拓扑展示与排障使用)"""
    config = load_config()
    mode = config.get("network_mode", "mixed")
    nodes = []
    for n in config.get("nodes", []):
        # 按模式决定首选探测地址: lan 用 LAN IP, wireguard 用 wg IP, mixed 先 LAN
        if mode == "lan":
            primary = n.get("ip", "")
        elif mode == "wireguard":
            primary = n.get("wg_ip", "") or n.get("ip", "")
        else:
            primary = n.get("ip", "")
        nodes.append({
            "name": n["name"],
            "display_name": n.get("display_name", n["name"]),
            "ip": n.get("ip", ""),
            "wg_ip": n.get("wg_ip", ""),
            "primary_addr": primary,
            "fallback_addr": n.get("wg_ip", "") if mode == "mixed" else "",
        })
    return jsonify({
        "ok": True,
        "network_mode": mode,
        "network_mode_label": config.get("network_mode_label", ""),
        "wg_enabled": config.get("wg_enabled", True),
        "wg_subnet": config.get("wg_subnet", ""),
        "lan_subnet": config.get("lan_subnet", ""),
        "nodes": nodes,
    })

def resolve_bind_host(raw):
    """校验 PANEL_HOST: 提前拒绝必然 bind 失败的取值, 并说明该怎么改。

    这里是最末端的一道防线 —— 直接运行 app.py 时会绕过 init.sh 的校验。
    监听地址必须是本机某张网卡的地址: 填成网段地址 (192.168.1.0) 或回环网段的
    网络地址 (127.0.0.0) 只会以 "Cannot assign requested address" 收场, 报错
    信息里看不出真正原因。
    """
    host = (raw or "0.0.0.0").strip()
    parts = host.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        print(f"[ERROR] PANEL_HOST={host} 不是合法的 IPv4 地址", file=sys.stderr)
        print("        请填写本机网卡地址 (如 192.168.1.101), 或 0.0.0.0 监听全部网卡", file=sys.stderr)
        sys.exit(2)
    octets = [int(p) for p in parts]
    if host == "0.0.0.0" or host == "127.0.0.1":
        return host
    if octets[0] == 0 or octets[0] == 127 or octets[0] >= 224:
        print(f"[ERROR] PANEL_HOST={host} 不是可用监听地址 (保留段 / 回环网段 / 组播段)", file=sys.stderr)
        print("        仅本机访问请用 127.0.0.1; 同网段访问请填本机局域网地址", file=sys.stderr)
        sys.exit(2)
    if octets[3] in (0, 255):
        kind = "网络地址 (整个网段)" if octets[3] == 0 else "广播地址"
        print(f"[ERROR] PANEL_HOST={host} 是{kind}, 面板只能绑定到某台主机的地址", file=sys.stderr)
        print("        如本机地址为 192.168.1.101 就填 192.168.1.101", file=sys.stderr)
        sys.exit(2)
    return host


if __name__ == "__main__":
    port = int(os.environ.get("PANEL_PORT", 9000))
    host = resolve_bind_host(os.environ.get("PANEL_HOST", "0.0.0.0"))
    cfg = load_config()
    print(f"OneCloud Cluster Panel 启动中...")
    print(f"  http://{host}:{port}")
    print(f"  组网模式: {cfg.get('network_mode', 'mixed')} "
          f"(WireGuard {'启用' if cfg.get('wg_enabled', True) else '未启用'})")
    if host == "0.0.0.0":
        print("  (监听全部网卡, 请确保已用防火墙限制来源)")
    # 未显式设置密码时提示一次性随机密码, 避免"默认弱口令"开箱即被扫
    if PANEL_PASS_GENERATED:
        print("", file=sys.stderr)
        print("=" * 62, file=sys.stderr)
        print(" [重要] 未设置 PANEL_PASS, 已生成本次运行的临时密码:", file=sys.stderr)
        print(f"         用户名: {PANEL_USER}", file=sys.stderr)
        print(f"         密  码: {PANEL_PASS}", file=sys.stderr)
        print("         建议在 /etc/onecloud/panel.env 固定 PANEL_USER/PANEL_PASS",
              file=sys.stderr)
        print("         (未固定时服务重启会换新密码)", file=sys.stderr)
        print("=" * 62, file=sys.stderr)
        print("", file=sys.stderr)
    app.run(host=host, port=port, debug=False)