@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo 已生成 .env，请先按需修改（MUSE_KEY / 支付宝参数）
)

where python >nul 2>nul || (echo 找不到 python，请先安装 Python 3.10+ & exit /b 1)
python -c "import fastapi,uvicorn,segno,PIL,cryptography" 2>nul || (
    echo 正在安装依赖...
    python -m pip install -r backend\requirements.txt
)

echo.
echo   本地地址: http://127.0.0.1:8080
echo   管理后台: http://127.0.0.1:8080/admin
echo   按 Ctrl+C 停止
echo.
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
