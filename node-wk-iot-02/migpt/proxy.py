#!/usr/bin/env python3
# migpt 轻量代理方案 - 如果原生 binary 不可用
# Flask 实现 AI API 代理 Web 界面

import os
import json
import requests
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

def load_config():
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except:
        return {
            "ai": {
                "api_key": os.environ.get("MIGPT_API_KEY", ""),
                "base_url": os.environ.get("MIGPT_BASE_URL", "https://api.openai.com/v1"),
                "model": os.environ.get("MIGPT_MODEL", "gpt-4o-mini"),
            }
        }

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OneCloud AI 助手</title>
    <style>
        body { font-family: system-ui, sans-serif; max-width: 800px; margin: 20px auto; padding: 0 20px; }
        #chat { height: 400px; overflow-y: auto; border: 1px solid #ddd; padding: 10px; border-radius: 8px; }
        .msg { margin: 8px 0; padding: 8px 12px; border-radius: 12px; }
        .user { background: #007bff; color: white; margin-left: 20%; }
        .ai { background: #f1f1f1; margin-right: 20%; white-space: pre-wrap; }
        #input { width: 70%; padding: 10px; border-radius: 8px; border: 1px solid #ddd; }
        #send { width: 25%; padding: 10px; border-radius: 8px; background: #007bff; color: white; border: none; cursor: pointer; }
    </style>
</head>
<body>
    <h1>OneCloud AI 助手</h1>
    <div id="chat"></div>
    <br>
    <input id="input" placeholder="输入消息..." onkeydown="if(event.key==='Enter')send()">
    <button id="send" onclick="send()">发送</button>
    <script>
        let history = [];
        function append(role, text) {
            const div = document.createElement('div');
            div.className = 'msg ' + role;
            div.textContent = text;
            document.getElementById('chat').appendChild(div);
            document.getElementById('chat').scrollTop = document.getElementById('chat').scrollHeight;
        }
        async function send() {
            const input = document.getElementById('input');
            const text = input.value.trim();
            if (!text) return;
            append('user', text);
            history.push({role: 'user', content: text});
            input.value = '';
            append('ai', '思考中...');
            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({messages: history})
                });
                const data = await res.json();
                const reply = data.reply || '无响应';
                history.push({role: 'assistant', content: reply});
                document.querySelectorAll('.ai').forEach(el => {
                    if (el.textContent === '思考中...') el.textContent = reply;
                });
            } catch(e) {
                document.querySelectorAll('.ai').forEach(el => {
                    if (el.textContent === '思考中...') el.textContent = '错误: ' + e.message;
                });
            }
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/chat", methods=["POST"])
def chat():
    cfg = load_config()
    ai_cfg = cfg.get("ai", {})
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "https://api.openai.com/v1")
    model = ai_cfg.get("model", "gpt-4o-mini")

    if not api_key:
        return jsonify({"reply": "API Key 未配置!"}), 400

    messages = request.json.get("messages", [])

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": ai_cfg.get("max_tokens", 2048),
                "temperature": ai_cfg.get("temperature", 0.7),
            },
            timeout=60,
        )
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"错误: {str(e)}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("MIGPT_PORT", 8082))
    app.run(host="127.0.0.1", port=port, debug=False)
