// OneCloud Cluster Panel - 前端逻辑

let autoRefreshTimer = null;
const REFRESH_INTERVAL = 10000;

async function refresh() {
    try {
        const res = await fetch("/api/status");
        const data = await res.json();
        renderStatus(data);
    } catch (e) {
        console.error("获取状态失败:", e);
        document.getElementById("updateTime").textContent = "最后更新: 错误";
    }
}

function renderStatus(data) {
    document.getElementById("clusterName").textContent = data.cluster_name;
    document.getElementById("updateTime").textContent = `更新: ${data.timestamp}`;

    const nodesEl = document.getElementById("nodes");
    nodesEl.innerHTML = data.nodes.map(node => renderNode(node)).join("");

    renderTopology(data.nodes);
    updateExecNodeSelect(data.nodes);
}

function renderNode(node) {
    const statusClass = node.online ? "online" : "offline";
    const statusText = node.online ? "在线" : "离线";

    const sys = node.system || {};
    const load = sys.LOAD || "-";
    const mem = sys.MEM || "-";
    const disk = sys.DISK || "-";
    const uptime = sys.UPTIME || "-";

    const servicesHtml = (node.services || []).map(svc => `
        <div class="service-item">
            <span class="service-name">
                <span class="status-dot ${svc.running ? 'running' : 'stopped'}"></span>
                ${svc.display}
                <span class="muted">(${svc.type})</span>
            </span>
            <span class="service-actions">
                ${svc.running
                    ? `<button onclick="serviceAction('${node.name}','${svc.name}','restart')">↻</button>
                       <button onclick="serviceAction('${node.name}','${svc.name}','stop')">■</button>`
                    : `<button onclick="serviceAction('${node.name}','${svc.name}','start')">▶</button>`
                }
                <button onclick="serviceAction('${node.name}','${svc.name}','logs')">📋</button>
            </span>
        </div>
    `).join("");

    return `
        <div class="node-card" style="border-top: 4px solid ${node.color}">
            <div class="node-header">
                <h2>${node.display_name}</h2>
                <span class="node-badge ${statusClass}">${statusText}</span>
            </div>
            <div class="node-body">
                <div class="node-ip">${node.name} · ${node.ip} (WG: ${node.wg_ip})</div>
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
}

function updateExecNodeSelect(nodes) {
    const select = document.getElementById("execNode");
    select.innerHTML = '<option value="">选择节点...</option>' +
        nodes.map(n => `<option value="${n.name}">${n.display_name} (${n.ip})</option>`).join("");
}

async function serviceAction(node, svc, action) {
    const res = await fetch(`/api/service/${node}/${svc}/${action}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"}
    });
    const data = await res.json();
    refresh();
    if (data.output || data.error) {
        showToast(`[${action}] ${svc}: ${data.ok ? '成功' : '失败'}`);
    }
}

async function nodeAction(node, action) {
    if (action === "reboot" || action === "shutdown") {
        if (!confirm(`确定要对 ${node} 执行 ${action} 吗?`)) return;
    }
    const res = await fetch(`/api/node/${node}/action`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action})
    });
    const data = await res.json();
    refresh();
    showToast(`${node}: ${action} ${data.ok ? '✓' : '✗'}`);
}

async function clusterAction(action) {
    if (action === "health_check") {
        showToast("正在执行健康检查...");
        try {
            const res = await fetch("/api/status");
            const data = await res.json();
            let summary = [];
            for (const node of data.nodes) {
                const status = node.online ? "✅ 在线" : "❌ 离线";
                const svcRunning = (node.services || []).filter(s => s.running).length;
                const svcTotal = (node.services || []).length;
                const load = (node.system || {}).LOAD || "-";
                summary.push(`${node.display_name}: ${status} | 服务 ${svcRunning}/${svcTotal} | 负载 ${load}`);
            }
            showToast(summary.join(" \n "), 5000);
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
    const res = await fetch("/api/exec", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({node, command: cmd})
    });
    const data = await res.json();
    const outputEl = document.getElementById("execOutput");
    outputEl.textContent = (data.output || "") + (data.error ? "\n[错误] " + data.error : "") || "(无输出)";
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
