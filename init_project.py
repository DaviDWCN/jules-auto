#!/usr/bin/env python3
"""
Jules Auto Project Generator
Quickly initializes a new GitHub repository from the jules_auto_template.
"""

import argparse
import json
import os
import shutil
import sys

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser(description="Initialize a new project from jules_auto_template")
    parser.add_argument("-t", "--target-dir", required=True, help="Destination directory for the new project")
    parser.add_argument("-n", "--name", help="Name of the new project (defaults to destination folder name)")
    parser.add_argument("-c", "--context", help="Project context/tech stack guidelines for Jules")

    args = parser.parse_args()

    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jules_auto_template")
    target_dir = os.path.abspath(args.target_dir)

    if not os.path.isdir(template_dir):
        print(f"[Error] Template directory not found at: {template_dir}")
        sys.exit(1)

    project_name = args.name or os.path.basename(target_dir)
    project_context = args.context or f"Production project for {project_name}. Modular design with full unit test coverage using pytest."

    print(f"\n[*] Creating new Jules Auto project:")
    print(f"   - Target directory : {target_dir}")
    print(f"   - Project Name     : {project_name}")
    print(f"   - Project Context  : {project_context}\n")

    os.makedirs(target_dir, exist_ok=True)

    # Copy files
    for item in os.listdir(template_dir):
        s = os.path.join(template_dir, item)
        d = os.path.join(target_dir, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)

    # Customize automation/config.json
    config_file = os.path.join(target_dir, "automation", "config.json")
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        config["project_name"] = project_name
        config["project_context"] = project_context
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    # Customize pyproject.toml
    pyproject_file = os.path.join(target_dir, "pyproject.toml")
    if os.path.exists(pyproject_file):
        with open(pyproject_file, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace('name = "my-new-app"', f'name = "{project_name}"')
        with open(pyproject_file, "w", encoding="utf-8") as f:
            f.write(content)

    # Customize README.md
    readme_file = os.path.join(target_dir, "README.md")
    if os.path.exists(readme_file):
        with open(readme_file, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("{{PROJECT_NAME}}", project_name)
        with open(readme_file, "w", encoding="utf-8") as f:
            f.write(content)

    print("[OK] Project generated successfully!")
    print("\nNext Steps:")
    print(f"   1. cd \"{target_dir}\"")
    print("   2. git init && git add . && git commit -m \"feat: initial commit with jules-auto template\"")
    print("   3. git remote add origin <your-new-github-repo-url> && git push -u origin main")
    print("   4. 在 GitHub 仓库设置中添加唯一 Secret: JULES_API_KEY")
    print("   5. 勾选 Allow auto-merge 与 Workflow Read & write 权限")
    print("   6. 在 automation/tasks.json 中定义你的第一个需求任务！\n")

if __name__ == "__main__":
    main()
