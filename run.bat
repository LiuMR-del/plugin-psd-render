@echo off
chcp 65001 >nul
rem PSD 批量生图 — Windows 一键启动(双击运行)
rem 每次启动自动检查并修复运行环境:
rem   1. 找 Python 3.10+; 没有时询问后用 winget 安装到当前用户(不需要管理员权限)
rem   2. .venv 不存在或已损坏(如从别的电脑拷来)时重建
rem   3. 依赖缺失或 requirements.txt 有变化时重装; 官方源失败时换国内镜像重试
rem   4. 检查 Photoshop, 没装只提示(仍可打开程序审核模板)
rem 带参数 selfcheck 时(Windows自检.bat)环境就绪后运行 windows_selfcheck.py, 而不是打开程序
rem 注意: 本文件须保持 CRLF 换行; 主流程只用 goto, 不用括号代码块(块内中文括号、变量展开易出错)
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "IMPORTS=import flet, psd_tools, openpyxl, dotenv, win32com.client"
set "HASH_PY=import hashlib, os, sys; h = hashlib.sha256(open('requirements.txt', 'rb').read()).hexdigest(); p = os.path.join('.venv', '.requirements.sha256')"
set "PIP_ARGS=--disable-pip-version-check --retries 2 --timeout 30 -r requirements.txt"

rem ---- 1-2. Python 与虚拟环境 ----
if not exist ".venv\" goto :venv_check
%VENV_PY% -c "import sys" >nul 2>nul && goto :venv_check
echo 虚拟环境不完整或已损坏（可能是上次创建被中断，或从别的电脑拷来），重新创建...
rmdir /s /q .venv
:venv_check
if exist "%VENV_PY%" goto :deps
call :find_python
if errorlevel 1 call :install_python
if errorlevel 1 goto :fail
echo 首次运行：用 %PYTHON% 创建虚拟环境...
%PYTHON% -m venv .venv
if errorlevel 1 set "MSG=创建虚拟环境失败。" & goto :fail

rem ---- 3. 依赖：能导入且 requirements.txt 没变就跳过 ----
:deps
%VENV_PY% -c "%HASH_PY%; sys.exit(0 if os.path.exists(p) and open(p).read().strip() == h else 1)" >nul 2>nul || goto :install_deps
%VENV_PY% -c "%IMPORTS%" >nul 2>nul && goto :photoshop
:install_deps
call :pip_install
set "PIP_RC=%errorlevel%"
%VENV_PY% -c "%IMPORTS%" >nul 2>nul
if errorlevel 1 set "MSG=依赖安装失败，请检查网络后重试；仍不行请截图本窗口联系维护人员。" & goto :fail
if not "%PIP_RC%"=="0" goto :pip_offline
%VENV_PY% -c "%HASH_PY%; open(p, 'w').write(h)"
goto :photoshop
:pip_offline
rem 离线时更新失败但旧依赖还能用：先照常启动，下次联网再更新
echo 提示：依赖更新失败（可能没联网），先用已安装的版本启动。

rem ---- 4. Photoshop（只提示） ----
:photoshop
reg query "HKCR\Photoshop.Application" >nul 2>nul && goto :launch
reg query "HKLM\SOFTWARE\Adobe\Photoshop" >nul 2>nul && goto :launch
echo 提示：未检测到 Photoshop。程序可以打开、审核模板，但出图需要安装 Photoshop CC 2018 或更新版本。

:launch
if /i "%~1"=="selfcheck" goto :selfcheck
%VENV_PY% main.py
if errorlevel 1 pause
exit /b 0

:selfcheck
%VENV_PY% windows_selfcheck.py
pause
exit /b 0

:fail
echo %MSG%
pause
exit /b 1

rem ---- 子程序 ----
rem 找 Python 3.10+，找到时 PYTHON=可执行命令，返回 0
:find_python
set "PYTHON="
call :try_python py -3 && exit /b 0
rem 应用商店的 python 占位程序运行会失败，自然跳过
call :try_python python && exit /b 0
for %%v in (313 312 311 310) do if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" call :try_python "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" && exit /b 0
exit /b 1

:try_python
%* -c "import sys; sys.exit(0 if sys.hexversion >= 0x30A0000 else 1)" >nul 2>nul || exit /b 1
set PYTHON=%*
exit /b 0

:install_python
echo 未找到 Python 3.10+。
where winget >nul 2>nul || goto :no_winget
set "ANSWER=Y"
set /p "ANSWER=是否自动下载安装 Python 3.11 到当前用户（约 30MB，不需要管理员权限）？[Y/n] "
if /i "%ANSWER:~0,1%"=="N" set "MSG=请手动安装 Python 3.10+（https://www.python.org/downloads/ ，安装时勾选 Add python.exe to PATH）后再启动。" & exit /b 1
echo 安装 Python 3.11...
winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
call :find_python && exit /b 0
set "MSG=Python 安装失败或安装后找不到，请手动安装 Python 3.10+（https://www.python.org/downloads/ ，安装时勾选 Add python.exe to PATH）。"
exit /b 1
:no_winget
set "MSG=未找到 Python 3.10+，本机也没有 winget 无法自动安装。请手动安装 Python 3.10+（https://www.python.org/downloads/ ，安装时勾选 Add python.exe to PATH）。"
exit /b 1

:pip_install
echo 安装/更新依赖（需联网）...
%VENV_PY% -m pip install %PIP_ARGS% && exit /b 0
for %%i in (https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple/) do echo 改用镜像 %%i 重试... & %VENV_PY% -m pip install -i %%i %PIP_ARGS% && exit /b 0
exit /b 1
