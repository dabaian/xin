#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

[[ -f .env ]] || { cp .env.example .env; echo "已生成 .env，请按需修改"; }
python3 -c "import fastapi" 2>/dev/null || python3 -m pip install -r backend/requirements.txt

echo "本地地址: http://127.0.0.1:8080"
echo "管理后台: http://127.0.0.1:8080/admin"
cd backend
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8080
