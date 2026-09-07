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
import subprocess
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

# 节点IP映射（硬编码fallback验证用）
NODE_IP_MAP = {
    "wk-edge-01": "192.168.1.101",
    "wk-iot-02": "192.168.1.102",
    "wk-storage-03": "192.168.1.103",
}

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
            if name in NODE_IP_MAP:
                expected_ip = NODE_IP_MAP[name]
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
            elif node not in NODE_IP_MAP:
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
        inv_node_names = set(NODE_IP_MAP.keys())
        
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
            
            # 检查 shebang
            if content.startswith("#!/bin/bash") or content.startswith("#!/bin/sh"):
                log_pass(f"{script.name} 有 shebang")
            else:
                log_warn(f"{script.name} 缺少 shebang")
            
            # 检查 set -e 或 set -u
            if "set -e" in content or "set -u" in content:
                log_pass(f"{script.name} 有错误处理 (set -e/-u)")
            else:
                log_warn(f"{script.name} 缺少 set -e/-u")
            
            # 检查基本结构
            functions = re.findall(r'^(\w+)\(\)\s*\{', content, re.MULTILINE)
            if functions:
                log_info(f"  函数: {', '.join(functions[:10])}{'...' if len(functions) > 10 else ''}")
            
            # 检查 log 函数
            has_log_info = "log_info" in content
            has_log_error = "log_error" in content
            if has_log_info and has_log_error:
                log_pass(f"{script.name} 有日志函数")
            elif not has_log_info:
                log_warn(f"{script.name} 缺少 log_info 函数")
            elif not has_log_error:
                log_warn(f"{script.name} 缺少 log_error 函数")
                
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
                expected_ip = NODE_IP_MAP.get(name, "")
                
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
            
            # 检查 fallback 机制
            has_case_fallback = "case \"$NODE_NAME\"" in content
            has_hardcoded_ips = all(ip in content for ip in NODE_IP_MAP.values())
            
            if has_case_fallback:
                log_pass(f"{script_name} 有 case 语句 fallback")
            else:
                log_warn(f"{script_name} 缺少 case 语句 fallback")
            
            if has_hardcoded_ips:
                log_pass(f"{script_name} 包含所有硬编码节点 IP")
            else:
                missing = [ip for ip in NODE_IP_MAP.values() if ip not in content]
                log_warn(f"{script_name} 缺少 IP fallback: {missing}")
            
            # 验证 fallback IP 与 inventory 一致
            for name, ip in NODE_IP_MAP.items():
                pattern = rf'{name}\)\s+NODE_IP="({re.escape(ip)})"'
                if re.search(pattern, content):
                    log_pass(f"{script_name} 节点 {name} fallback IP 正确")
                elif ip in content:
                    log_info(f"{script_name} 节点 {name} IP 在脚本中")
                
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
