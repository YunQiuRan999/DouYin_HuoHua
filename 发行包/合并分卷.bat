@echo off
chcp 65001 >nul
cd /d %~dp0
echo 正在合并 4 个分卷 ...
copy /b 抖音续火花_成品版_打开即用.zip.part01+抖音续火花_成品版_打开即用.zip.part02+抖音续火花_成品版_打开即用.zip.part03+抖音续火花_成品版_打开即用.zip.part04 "抖音续火花_成品版_打开即用.zip" >nul
echo 合并完成：抖音续火花_成品版_打开即用.zip，解压即可使用。
pause
