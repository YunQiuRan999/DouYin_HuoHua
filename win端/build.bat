@echo off
chcp 65001 >nul
REM ============================================================
REM 一键打包：抖音续火花控制台（Win 二合一）
REM 依赖：python + pip install PySide6 fastapi uvicorn websockets
REM        requests playwright python-dotenv tzdata pydantic pyinstaller
REM 产物：dist\DouyinFireConsole\DouyinFireConsole.exe
REM ============================================================
cd /d "%~dp0"

echo [1/5] 校验依赖...
pip show pyinstaller >nul 2>&1 || pip install pyinstaller
pip show PySide6 >nul 2>&1 || pip install PySide6
pip show fastapi >nul 2>&1 || pip install fastapi
pip show uvicorn >nul 2>&1 || pip install uvicorn
pip show websockets >nul 2>&1 || pip install websockets
pip show requests >nul 2>&1 || pip install requests
pip show playwright >nul 2>&1 || pip install playwright
pip show python-dotenv >nul 2>&1 || pip install python-dotenv
pip show tzdata >nul 2>&1 || pip install tzdata

echo [2/5] 打包（首次约 3-10 分钟）...
pyinstaller build.spec --noconfirm || goto :error

echo [3/5] 组装发布目录...
set "DIST=dist\DouyinFireConsole"
copy /y "使用说明.md" "%DIST%\" >nul 2>&1

echo [4/5] 裁剪体积（删除未使用的 Qt 模块与翻译）...
rem 注意：以下模块均为本程序未使用的 Qt 模块；若未来代码隐式依赖其中某个
rem 模块（如 requests 走 QtNetwork、渲染 SVG 等），运行时才会崩溃且难排查。
rem 新增功能后请先完整自检（--smoke-test）再发布；如需恢复某个模块，
rem 删除对应 del 行并重新打包即可。
del /q "%DIST%\_internal\PySide6\Qt6Quick*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Qml*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Pdf*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Network*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6OpenGL*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Svg*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Sql*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Xml*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6PrintSupport*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6DBus*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Test*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6UiTools*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Designer*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Concurrent*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Multimedia*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Charts*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6DataVisualization*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6Web*.dll" 2>nul
del /q "%DIST%\_internal\PySide6\Qt6VirtualKeyboard*.dll" 2>nul
for %%f in ("%DIST%\_internal\PySide6\translations\*.qm") do (
    echo %%~nxf | findstr /i "zh_CN en" >nul || del /q "%%f"
)
for %%d in (qmltooling scenegraph qml qt3d sensors position multimedia pdf sqldrivers tls networkinformation iconengines generic) do (
    if exist "%DIST%\_internal\PySide6\plugins\%%d" rmdir /s /q "%DIST%\_internal\PySide6\plugins\%%d"
)

echo.
echo 打包完成：%DIST%\DouyinFireConsole.exe
pause
exit /b 0

:error
echo 打包失败，请查看上方错误信息。
pause
exit /b 1
