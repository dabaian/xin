@echo off
chcp 65001 >nul
setlocal
echo ==============================
echo  Viora AI 客户端打包
echo ==============================

where python >nul 2>nul || (echo 找不到 python & exit /b 1)

echo [1/3] 安装依赖...
python -m pip install --quiet pyinstaller pywebview pythonnet || exit /b 1

echo [2/3] 打包 EXE...
python -m PyInstaller --noconfirm --clean VioraAI.spec || exit /b 1

echo [3/3] 制作安装包...
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    %ISCC% installer.iss || exit /b 1
    echo 安装包: installer_out\VioraAI-Setup.exe
) else (
    echo 未安装 Inno Setup 6，跳过安装包制作。
    echo 免安装版: dist\VioraAI.exe
)

echo 完成。
endlocal
