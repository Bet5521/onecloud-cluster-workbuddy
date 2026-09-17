# 📚 docs/ — 项目文档

OneCloud 集群的完整技术文档。

---

## 文档目录

| 文档 | 内容 |
|------|------|
| [requirements.md](requirements.md) | 需求分析、功能清单、硬件规格 |
| [architecture.md](architecture.md) | 架构设计、网络拓扑、存储与内存规划 |
| [topology.md](topology.md) | 可视化拓扑图（Mermaid + ASCII） |
| [operations.md](operations.md) | 运维手册（备份/恢复/更新/故障排查） |
| [cloudflare-setup.md](cloudflare-setup.md) | Cloudflare Zero Trust Tunnel 配置指南 |
| [package-trim.md](package-trim.md) | 初始化装包精简说明（核心/可选/桌面图形三档与移除理由） |
| [install-path.md](install-path.md) | 安装路径自适应（SD 卡可用用 SD，否则回退 /opt，决策流程与函数清单） |
| [sd-tools.md](sd-tools.md) | SD 卡工具箱（格式化/迁移/更换备份：功能、前置条件、用法与注意事项） |
| [to-fix.md](to-fix.md) | 全项目验证问题清单与修复记录 |
| [init-deploy-fixes.md](init-deploy-fixes.md) | 初始化/部署链路缺陷修复（脚本权限 / 面板迁移 / 节点IP同步 / 远程数据根） |
| [functional-inventory.md](functional-inventory.md) | 功能清单与说明（节点 / 服务 / 脚本 / 面板 / 验证 / 文档索引） |
| [audit-2026-09.md](audit-2026-09.md) | 代码库质量与功能验证报告（问题分级 + 改进建议） |
| [firewall/](firewall/) | 各节点防火墙建议清单（`firewall-recommend.sh` 生成） |

---

## 快速导航

**新手入门**：先读 [requirements.md](requirements.md) 了解项目全貌，再按 [根目录 README](../README.md) 的快速开始操作。

**架构理解**：[architecture.md](architecture.md) + [topology.md](topology.md)

**日常运维**：[operations.md](operations.md)

**公网访问**：[cloudflare-setup.md](cloudflare-setup.md)
