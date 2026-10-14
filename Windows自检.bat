@echo off
chcp 65001 >nul
rem PSD 批量生图 — Windows 自检(双击运行): 先像 run.bat 一样准备好环境, 再运行 windows_selfcheck.py
rem 结果写到本目录 自检结果\ 下, 把整个文件夹发给维护人员即可
call "%~dp0run.bat" selfcheck
