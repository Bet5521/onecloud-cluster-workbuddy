// OneCloud Cluster Panel - 前端逻辑

let autoRefreshTimer = null;
const REFRESH_INTERVAL = 10000;
let currentNodes = [];
let currentNet = {mode: "mixed", wg_enabled: true, label: ""};

/**
 * 统一的请求封装。
 *
 * 关键点 (见 docs/audit-2026-09.md C-1):
 *   - credentials: "same-origin" 让会话 cookie 随请求发送。原先每处 fetch 都不带
 *     凭据, 而后端 require_auth 保护了所有 /api/*, 导致浏览器里 100% 返回 401,
 *     面板开箱即不可用。
 *   - X-Requested-With 供后端 require_csrf_header 校验 (状态变更接口)。
 *   - 401 时自动跳转登录页, 而不是静默失败。
 */
async function apiFetch(path, options) {
    options = options || {};
    const headers = Object.assign(
        {"X-Requested-With": "OneCloudPanel"},
        options.headers || {}
    );
    const res = await fetch(path, Object.assign({}, options, {
        headers: headers,
        credentials: "same-origin",
    }));
    if (res.status === 401) {
        window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
        throw new Error("未登录");
    }
    return res;
}

async function refresh() {
    try {
        const res = await apiFetch("/api/status");
        const data = await res.json();
        renderStatus(data);
    } catch (e) {
        console.error("获取状态失败:", e);
        const el = document.getElementById("updateTime");
        if (el) el.textContent = "最后更新: 错误";
    }
}

function renderStatus(data) {
    currentNodes = data.nodes || [];
    currentNet = {
        mode: data.network_mode || "mixed",
        wg_enabled: data.wg_enabled !== false,
        label: data.network_mode_label || "",
    };
    document.getElementById("clusterName").textContent = data.cluster_name;
    const modeEl = document.getElementById("netMode");
    if (modeEl) modeEl.textContent = currentNet.label ? `· ${currentNet.label}` : "";
    document.getElementById("updateTime").textContent = `更新: ${data.timestamp}`;

    const nodesEl = document.getElementById("nodes");
    nodesEl.innerHTML = data.nodes.map(node => renderNode(node)).join("");

    renderTopology(data.nodes);
    updateExecNodeSelect(data.nodes);
}

/** 三态判定: 未安装 / 已停止 / 运行中 */
function serviceState(svc) {
    if (!svc.installed) {
        if (svc.install === "manual")   return {cls: "notinstalled", text: "待手动安装"};
        if (svc.install === "external") return {cls: "notinstalled", text: "未启用"};
        return {cls: "notinstalled", text: "未安装"};
    }
    return svc.running ? {cls: "running", text: "运行中"}
                       : {cls: "stopped", text: "已停止"};
}

/** 未安装服务只给提示按钮, 不提供启停 (启停会对不存在的单元报错) */
function serviceButtons(node, svc, st) {
    if (!svc.installed) {
        const hint = svc.install === "manual"
            ? "该组件需手动安装, 详见 docs/sd-tools.md 与官方下载页"
            : "该组件未安装, 请在节点执行: ./scripts/install-services.sh <服务名>";
        return `<button onclick="showToast('${hint.replace(/'/g, "\\'")}', 6000)">? 说明</button>`;
    }
    return st.cls === "running"
        ? `<button onclick="serviceAction('${node.name}','${svc.name}','restart')">↻</button>
           <button onclick="serviceAction('${node.name}','${svc.name}','stop')">■</button>
           <button onclick="serviceAction('${node.name}','${svc.name}','logs')">📋</button>`
        : `<button onclick="serviceAction('${node.name}','${svc.name}','start')">▶</button>
           <button onclick="serviceAction('${node.name}','${svc.name}','logs')">📋</button>`;
}

function renderNode(node) {
    const statusClass = node.online ? "online" : "offline";
    const statusText = node.online ? "在线" : "离线";

    const sys = node.system || {};
    const load = sys.LOAD || "-";
    const mem = sys.MEM || "-";
    const disk = sys.DISK || "-";
    const uptime = sys.UPTIME || "-";

    const servicesHtml = (node.services || []).map(svc => {
        const st = serviceState(svc);
        return `
        <div class="service-item">
            <span class="service-name">
                <span class="status-dot ${st.cls}"></span>
                ${svc.display}
                <span class="muted">(${svc.type}${svc.port ? ' · ' + svc.port : ''})</span>
                <span class="service-state muted">${st.text}</span>
            </span>
            <span class="service-actions">
                ${serviceButtons(node, svc, st)}
            </span>
        </div>`;
    }).join("");

    // 汇总: 分母只算"应当安装"的服务, 未安装的不计入
    const installedN = node.services_installed != null
        ? node.services_installed
        : (node.services || []).filter(s => s.installed).length;
    const runningN = node.services_running != null
        ? node.services_running
        : (node.services || []).filter(s => s.running).length;
    const totalN = node.services_total || (node.services || []).length;
    const summary = `已装 ${installedN}/${totalN} · 运行 ${runningN}`;

    // LAN 模式不展示 WG 行, 避免"没装却显示 10.8.0.x"的误导
    const addr = (currentNet.wg_enabled && node.wg_ip)
        ? `${node.name} · ${node.ip} (WG: ${node.wg_ip})`
        : `${node.name} · ${node.ip}`;

    return `
        <div class="node-card" style="border-top: 4px solid ${node.color}">
            <div class="node-header">
                <h2>${node.display_name}</h2>
                <span class="node-badge ${statusClass}">${statusText}</span>
            </div>
            <div class="node-body">
                <div class="node-ip">${addr}</div>
                <div class="node-summary muted">${summary}</div>
                <div class="system-stats">
                    <div class="stat-item">
                        <span class="stat-label">负载</span>
                        <span class="stat-value">${load}</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-label">运行</span>
                        <span class="stat-value" style="font-size:0.75rem">${uptime}</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-label">内存</span>
                        <span class="stat-value" style="font-size:0.75rem">${mem}</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-label">磁盘</span>
                        <span class="stat-value" style="font-size:0.75rem">${disk}</span>
                    </div>
                </div>
                <div class="services-list">
                    ${servicesHtml || '<div class="muted">无注册服务</div>'}
                </div>
            </div>
            <div class="node-footer">
                <button onclick="nodeAction('${node.name}','docker_restart')">🔄 重启容器</button>
                <button onclick="nodeAction('${node.name}','docker_up')">▶️ 启动</button>
                <button onclick="nodeAction('${node.name}','docker_down')">⏹️ 停止</button>
                <button onclick="nodeAction('${node.name}','reboot')" style="color:#f5576c">↻ 重启</button>
            </div>
        </div>
    `;
}

function renderTopology(nodes) {
    const topoEl = document.getElementById("topoNodes");
    topoEl.innerHTML = nodes.map(n => `
        <div class="topo-node ${n.online ? 'online' : 'offline'}" style="border-color: ${n.color}">
            <div style="font-weight: 600; color: ${n.color}">${n.display_name}</div>
            <div class="topo-role">${n.name}</div>
            <div class="topo-ip">${n.ip}</div>
            <div class="topo-online">${n.online ? '● ONLINE' : '○ OFFLINE'}</div>
        </div>
    `).join("");

    // 组网方式: lan 模式完全不出现 WireGuard 字样
    const modeEl = document.getElementById("topoMode");
    if (modeEl) {
        if (currentNet.mode === "lan") {
            modeEl.textContent = "局域网直连 (" + (currentNet.lan_subnet || "LAN") + ")";
            modeEl.className = "topo-mesh topo-lan";
        } else if (currentNet.mode === "wireguard") {
            modeEl.textContent = currentNet.label || "WireGuard 组网";
            modeEl.className = "topo-mesh topo-wg";
        } else {
            modeEl.textContent = currentNet.label || "混合组网";
            modeEl.className = "topo-mesh topo-mixed";
        }
    }
}

async function doLogout() {
    try {
        await fetch("/logout", {method: "POST", credentials: "same-origin"});
    } catch (e) { /* 忽略 */ }
    window.location.href = "/login";
}

function updateExecNodeSelect(nodes) {
    const select = document.getElementById("execNode");
    select.innerHTML = '<option value="">选择节点...</option>' +
        nodes.map(n => `<option value="${n.name}">${n.display_name} (${n.ip})</option>`).join("");
}

async function serviceAction(node, svc, action) {
    const res = await apiFetch(`/api/service/${node}/${svc}/${action}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"}
    });
    const data = await res.json();
    refresh();
    const output = Array.isArray(data.output) ? data.output.join("\n") : (data.output || "");
    if (!res.ok && data.error) {
        showToast(`[${action}] ${svc}: ${data.error}`, 5000);
        return;
    }
    if (action === "logs") {
        const el = document.getElementById("execOutput");
        if (el) {
            el.textContent = (output || "(无日志输出)") + (data.error ? "\n[错误] " + data.error : "");
            if (el.scrollIntoView) el.scrollIntoView({behavior: "smooth", block: "nearest"});
        }
        showToast(`[日志] ${svc}: 已输出到下方命令输出区`, 3000);
        return;
    }
    if (output || data.error) {
        showToast(`[${action}] ${svc}: ${data.ok ? '成功' : '失败'}`);
    }
}

async function nodeAction(node, action) {
    if (action === "reboot" || action === "shutdown") {
        if (!confirm(`确定要对 ${node} 执行 ${action} 吗?`)) return;
    }
    const res = await apiFetch(`/api/node/${node}/action`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action, confirm: true})
    });
    const data = await res.json();
    refresh();
    showToast(`${node}: ${action} ${data.ok ? '✓' : '✗ ' + (data.error || '')}`);
}

async function clusterAction(action) {
    if (action === "health_check") {
        showToast("正在执行健康检查...");
        try {
            const res = await apiFetch("/api/status");
            const data = await res.json();
            let summary = [];
            summary.push(`组网模式: ${data.network_mode_label || data.network_mode}`);
            for (const node of data.nodes) {
                const status = node.online ? "✅ 在线" : "❌ 离线";
                const inst = node.services_installed != null ? node.services_installed : 0;
                const run = node.services_running != null ? node.services_running : 0;
                const total = node.services_total || (node.services || []).length;
                const load = (node.system || {}).LOAD || "-";
                summary.push(`${node.display_name}: ${status} | 已装 ${inst}/${total} 运行 ${run} | 负载 ${load}`);
            }
            showToast(summary.join(" \n "), 6000);
        } catch (e) {
            showToast("健康检查失败: " + e.message);
        }
        return;
    }
    if (action === "backup") {
        showToast("备份操作需在节点上执行: ./scripts/backup.sh all");
        return;
    }
    if (action === "update") {
        showToast("更新操作需在节点上执行: ./scripts/update-all.sh");
        return;
    }
    // docker_up / docker_down / docker_pull: 对所有节点逐个执行
    if (action.indexOf("docker_") === 0) {
        if (action === "docker_down") {
            if (!confirm("确定要停止所有节点上的全部容器吗?")) return;
        }
        let nodes = currentNodes || [];
        if (!nodes.length) {
            const res = await apiFetch("/api/status").catch(() => null);
            const d = res ? await res.json().catch(() => ({})) : {};
            nodes = d.nodes || [];
        }
        if (!nodes.length) {
            showToast("未获取到节点列表, 无法执行集群操作");
            return;
        }
        showToast(`正在对 ${nodes.length} 个节点执行 ${action}...`);
        const results = [];
        for (const node of nodes) {
            const res = await apiFetch(`/api/node/${node.name}/action`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({action, confirm: true})
            }).catch(() => null);
            const d = res ? await res.json().catch(() => ({})) : {};
            results.push(`${node.display_name || node.name}: ${d.ok ? "✓" : "✗"}`);
        }
        showToast(`${action} 执行完毕\n` + results.join("\n"), 5000);
        refresh();
        return;
    }
    showToast(`集群操作 '${action}' 已触发`);
    refresh();
}

async function execCmd() {
    const node = document.getElementById("execNode").value;
    const cmd = document.getElementById("execCmd").value;
    if (!node || !cmd) {
        showToast("请选择节点并输入命令");
        return;
    }
    const res = await apiFetch("/api/exec", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({node, command: cmd})
    });
    const data = await res.json();
    const outputEl = document.getElementById("execOutput");
    outputEl.textContent = ((data.output || "") + (data.error ? "\n[错误] " + data.error : "")) || "(无输出)";
}

function showToast(msg, duration) {
    duration = duration || 2500;
    const toast = document.createElement("div");
    toast.style.cssText = `
        position: fixed; bottom: 30px; right: 30px;
        background: rgba(0,0,0,0.85); color: #fff;
        padding: 12px 20px; border-radius: 8px;
        font-size: 0.9rem; z-index: 9999;
        max-width: 400px; white-space: pre-line;
        animation: fadeIn 0.3s ease;
    `;
    toast.textContent = msg;
    document.body.appendChild(toast);
    const fadeAt = duration - 500;
    setTimeout(() => { toast.style.opacity = "0"; toast.style.transition = "0.3s"; }, fadeAt > 0 ? fadeAt : 1000);
    setTimeout(() => toast.remove(), duration);
}

// 自动刷新
document.getElementById("autoRefresh").addEventListener("change", (e) => {
    if (e.target.checked) {
        autoRefreshTimer = setInterval(refresh, REFRESH_INTERVAL);
    } else {
        clearInterval(autoRefreshTimer);
    }
});

// 初始化
refresh();
autoRefreshTimer = setInterval(refresh, REFRESH_INTERVAL);

// 动画
const style = document.createElement("style");
style.textContent = `@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }`;
document.head.appendChild(style);
