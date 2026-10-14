# AI 安装与更新指令

用法：复制下面对应的一整段，粘贴给 AI 编程助手（Claude Code 等），它会自动完成。

- 第一次在一台电脑上用 → 「一、首次安装」
- 已经装过，要换成最新版 → 「二、更新」
- 维护人员发布新版本 → 「三、发布更新」

指令里不含任何密钥。AI 的地址和 Key 由维护人员线下私发（`.env` 文件），或装好后在程序「AI 设置」里填写。

仓库地址：https://github.com/LiuMR-del/plugin-psd-render

---

## 一、首次安装（新电脑，或以前用的是 zip 解压版）

```text
帮我安装并启动「PSD 批量生图」这个工具，仓库：https://github.com/LiuMR-del/plugin-psd-render

1. 先读仓库里的 README.md、CLAUDE.md、操作步骤.txt，了解这个工具（Windows 为主，macOS 也支持）。
2. 检查有没有 git。没有的话：Windows 用 `winget install --id Git.Git -e`，macOS 用 `xcode-select --install`。
3. 先确认是不是已经装过：按「更新」指令第 1 步的办法，用仓库地址查本机有没有这个仓库。已经有了就不要再 clone，告诉我位置并改按更新处理。
4. clone 到固定位置：Windows 放 `%USERPROFILE%\plugin-psd-render`，macOS 放 `~/plugin-psd-render`。该目录已存在、但不是这个仓库时，先问我。
5. 找一下以前解压的 zip 旧版目录（特征：有 run.bat 和 .env，但没有 .git；常见在 D:\PSD批量生图、桌面、下载目录）。找到就只把旧目录里的 `.env` 复制到新目录，别的都不动，并告诉我旧目录位置，由我决定删不删。
6. 运行环境自检（会自动装 Python 3.11、建 .venv、装依赖、打开 Photoshop 真实出一张测试图，约 1~5 分钟）：
   - Windows：在新目录执行 `Windows自检.bat`
   - macOS：在新目录执行 `bash start.sh selfcheck`
   脚本有两处会等键盘输入：缺 Python 时问「[Y/n]」、结束时「按任意键/回车退出」。用管道喂输入，避免卡住，例如 PowerShell：`'Y' | cmd /c Windows自检.bat`，macOS：`printf 'Y\n\n' | bash start.sh selfcheck`。命令超时至少给 10 分钟。
   结束后读 `自检结果/自检报告.txt`，把第 2 行结论告诉我；有失败项把对应内容贴给我。
7. 启动程序（界面会一直开着，用后台方式启动，不要等它结束）：Windows `start "" run.bat`，macOS `open 启动.command`。
8. 最后告诉我：安装位置、自检结论、还需要我做什么（通常是：在程序右上角「AI 设置」填 AI 地址和 Key 并拉取、勾选模型，或者把维护人员给的 .env 放进程序目录；然后选择模板库文件夹）。

注意：
- Photoshop 要我自己装好并激活；没装就告诉我，不要替我装。
- 不要修改 .env 的内容，不要把 Key 打印出来，也不要写进任何文件、日志或提交。
- 不要改代码，不要 git commit / push。
```

---

## 二、更新（已经装过）

```text
帮我把本地的「PSD 批量生图」更新到最新版，仓库：https://github.com/LiuMR-del/plugin-psd-render

1. 定位项目目录。目录名可能被改过，按仓库地址找，不要按名字猜：
   - 先看默认位置：Windows `%USERPROFILE%\plugin-psd-render`，macOS `~/plugin-psd-render`，用 `git -C <目录> remote get-url origin` 确认地址里有 plugin-psd-render。
   - 不在默认位置就搜索：
     macOS / Git Bash：
     find ~ -maxdepth 6 -name .git -type d -not -path "*/node_modules/*" -not -path "*/.venv/*" 2>/dev/null | while read g; do d=$(dirname "$g"); git -C "$d" remote get-url origin 2>/dev/null | grep -q "plugin-psd-render" && echo "$d"; done
     Windows PowerShell（家目录和各个盘）：
     foreach ($root in @($HOME) + (Get-PSDrive -PSProvider FileSystem).Root) { Get-ChildItem $root -Directory -Recurse -Depth 5 -Filter .git -Force -ErrorAction SilentlyContinue | ForEach-Object { $d = $_.Parent.FullName; if ((git -C $d remote get-url origin 2>$null) -match 'plugin-psd-render') { $d } } }
   - 找不到就直接告诉我，不要重新 clone（重新下载一份，我的 AI 配置看起来就"不见了"）。如果只找到以前解压的 zip 版（有 run.bat 但没有 .git），告诉我，改用「首次安装」指令。
2. 请我先关掉正在运行的「PSD 批量生图」窗口（开着时更新，旧程序可能把审核记录写回旧内容），我确认关了再继续。
3. 进入目录，`git status` 看有没有本地改动；记下当前版本：`git log -1 --format="%h %ad %s" --date=short`。
4. 执行 `git pull`。如果报 "local changes would be overwritten" 或有冲突，把文件列出来问我保留还是丢弃，不要自己决定。
5. 用 `git log <旧版本>..HEAD --format="- %ad %s" --date=short` 列出这次更新了什么，用中文简要告诉我。已经是最新就直接说。
6. 不用手动装依赖：启动时会按 requirements.txt 自动更新。启动一次确认能打开（后台方式，不要等它结束）：Windows `start "" run.bat`，macOS `open 启动.command`。
7. 如果这次更新改了 render.jsx 或 renderer.py（出图相关），建议再跑一次环境自检，方法同「首次安装」第 6 步。

注意：不要动 .env、.venv、自检结果、输出目录和模板库文件夹；不要改代码，不要 commit / push。
```

---

## 三、发布更新（维护人员）

```text
帮我把「PSD 批量生图」（本目录，仓库 https://github.com/LiuMR-del/plugin-psd-render ，公开仓库）的改动发布出去：

1. 跑两套测试，必须全部通过：`.venv/bin/python selftest.py` 和 `.venv/bin/python -m unittest discover -p 'test_*.py'`（Windows 用 .venv\Scripts\python）。不通过就停下告诉我。
2. 检查 `git status` 和 `git diff`：不能提交 .env、Key、真实订单的客户名、订单 Excel、输出图片、模板 PSD、本机路径（/Users/<用户名>、C:\Users\<用户名>）。发现了先停下问我。
3. 确认文档已同步：改了规则/流程的，方案文档（铝板画自动出图实施方案.md）变更记录要有当天一条；给使用者看的变化要写进 操作步骤.txt / 使用说明.txt。
4. 提交并推送：提交信息用中文，第一行写给同事看的改动（同事更新时会看到），例如「缺字体时开跑前弹窗提醒」。然后 `git push`。
5. 需要给没装 git 的人发 zip 时，再运行 `./pack.sh`（Windows 用 pack.bat），包在 dist/ 下。
6. 告诉我：提交了哪些改动、提交号、推送结果。
```
