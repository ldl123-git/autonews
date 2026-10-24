#!/bin/bash
cd /mnt/e/autonews || exit 1
rm -f tmp_git_check.sh

git init -q
git config user.name "autonews"
git config user.email "x862456735@163.com"

echo "=== 检查忽略 ==="
for p in data output output_finish upload .codebuddy docs/DEPLOY-PRODUCTION-WSL.md; do
  if git check-ignore -q "$p"; then echo "$p: ignored"; else echo "$p: tracked-ok"; fi
done

git add -A
echo "=== 待提交文件数 ==="
git diff --cached --name-only | wc -l
echo "=== 是否混入运行数据（应为空）==="
git diff --cached --name-only | grep -E '^(data|output|output_finish|upload)/|\.db$' || echo "clean"

git commit -q -m "init: autonews 资讯采集 / 改写 / 素材 / 排版 / 发布" || echo "commit failed"
git branch -M main
git remote remove origin 2>/dev/null
git remote add origin https://github.com/ldl123-git/autonews.git

echo "=== log ==="; git --no-pager log --oneline -3
echo "=== remote ==="; git --no-pager remote -v
echo "=== push 尝试（无凭据时会失败，属正常）==="
GIT_TERMINAL_PROMPT=0 git push -u origin main 2>&1 | tail -8
