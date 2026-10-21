# {{PROJECT_NAME}} 🚀

基于 **Google Jules REST API** 与 **GitHub Actions** 的全自动 AI 编程流水线项目。

---

## 📖 项目简介

本项目接入了 Jules 自动化开发引擎：
- 待办需求存放在 `automation/tasks.json`；
- 项目全局规范与技术栈定义在 `automation/config.json`；
- GitHub Actions 定时触发 Jules 编写代码、提交 PR、运行测试并在门禁全部通过后自动合入代码。

---

## 🛠️ 快速上手

请查阅 [QUICKSTART.md](QUICKSTART.md) 了解极简三步配置指南：
1. 配置 GitHub 仓库唯一 Secret: `JULES_API_KEY`；
2. 开启 GitHub Actions 读写与自动合并开关；
3. 编辑 `automation/tasks.json` 添加你的业务需求。

---

## 🧪 本地测试与运行

```bash
# 安装开发与测试依赖 (调度器零外部依赖)
pip install -e .[dev]

# 运行测试套件
pytest

# 本地执行一轮调度循环
export JULES_API_KEY="your-jules-api-key"
export GITHUB_REPOSITORY="your-name/your-repo"
python automation/worker.py
```
