#!/usr/bin/env bash
# verify 服务入口：构建检查 -> 代码测试 -> API/HTTP 冒烟与业务验收。
# 全部通过才以 0 退出；任一步失败立即以对应非零码退出。
set -euo pipefail

cd /workspace

echo "==> [1/3] 构建检查：字节码编译"
python3 -m compileall -q app verify

echo "==> [2/3] 代码测试（pytest：求解器/校验/HTTP 接口）"
python3 -m pytest -q

echo "==> [3/3] API/HTTP 冒烟与业务结果验收"
BASE_URL="http://${APP_HOST:-app}:${APP_PORT:-8080}"
exec python3 verify/acceptance.py --base-url "${BASE_URL}"
