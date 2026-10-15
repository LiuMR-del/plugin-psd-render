# plugin-psd-render — PSD 批量生图

在装有 Photoshop 的电脑上运行的桌面工具：导入订单 Excel，按「SKU + 尺寸」找到铝板画 PSD 模板，驱动 Photoshop 换上订单里的姓名、年份和人物造型，批量导出 PNG。文字由 Photoshop 原生渲染，字体、字距和样式与模板一致。

## 文档

| 文档 | 给谁看 | 内容 |
|---|---|---|
| [AI安装与更新指令.md](AI安装与更新指令.md) | 所有人 | 复制给 AI 编程助手的三段指令：首次安装 / 更新 / 发布更新 |
| [操作步骤.txt](操作步骤.txt) | 使用者 | 精简版：照着做就能出图 |
| [使用说明.txt](使用说明.txt) | 使用者、美工 | 完整说明：出图流程、模板制作规范（第五章）、出图规则、常见问题 |
| [铝板画自动出图实施方案.md](铝板画自动出图实施方案.md) | 开发维护 | 方案设计与变更记录 |
| [CLAUDE.md](CLAUDE.md) | 开发维护（含 AI） | 架构、关键不变量、命令 |

## 安装

- **推荐**：把 [AI安装与更新指令.md](AI安装与更新指令.md) 的「一、首次安装」复制给 AI 编程助手。
- **手动**：下载或解压本仓库，Windows 双击 `run.bat`，macOS 双击 `启动.command`。启动脚本会自动检查环境：缺 Python 3.10+ 时询问后装到当前用户；自动建 `.venv`、按 `requirements.txt` 装依赖（官方源失败时换清华、阿里镜像）；没检测到 Photoshop 只提示。
- 新电脑建议先跑自检：Windows 双击 `Windows自检.bat`，macOS 执行 `bash start.sh selfcheck`，结果在 `自检结果/`。

环境要求：Windows 10/11 或 macOS 12+，Photoshop CC 2018+（已激活）。模板用到的字体放进模板库文件夹，程序会自动安装；开跑前会检查缺字体。

AI（可选，用于分析新字段对应哪个图层）在程序内「AI 设置」配置，保存在项目根目录 `.env`：

```dotenv
PSD_RENDER_AI_BASE_URL=https://your-relay.example/v1
PSD_RENDER_AI_API_KEY=your-api-key
PSD_RENDER_AI_MODELS=model-a,model-b
```

`.env` 已被 `.gitignore` 和打包脚本排除，Key 不会写进其他配置、模板档案或日志。

## 工作方式

1. **订单识别**：订单表同时有 `SKU`、尺寸、`定制文案` 三列时走铝板画流程；尺寸列有 cm 用 cm，否则按写的数字直接匹配（毛毯 `50"x60"` 对 `（50×60）`），找不到再按 inch 换算兜底；方向严格（`30×20` ≠ `20×30`）。
2. **规则复用**：每个模板记着“订单字段 → 图层”的规则（存在模板库的 `.psd-render-profiles.json`，不含订单内容）。字段都见过的订单直接出图；有新字段的订单交给 AI 按单分析，程序检查通过即自动启用，不通过进入人工审核。
3. **出图计划**：`order_pipeline.compile_plan()` 把规则和本单内容编译成 Photoshop 参数（文字替换、图层显隐、范围框），并在进入 Photoshop 前检查姓名/年份落点和人物素材是否完整。
4. **渲染**：`renderer.py` 把参数交给 `render.jsx`（Windows 经 COM、macOS 经 osascript，两平台同一份 JSX），打开 PSD → 改文字和显隐 → 导出 PNG24 → 关闭不保存。看门狗在 Photoshop 卡住时重启并重试一次。

不满足铝板画条件的表格走早期的「普通模板流程」（按 SKU 文件夹、手动配置字段），目前未使用，见使用说明附录。

## 开发

```bash
.venv/bin/python main.py                               # 启动界面
.venv/bin/python selftest.py                           # 主自测（无需 Photoshop）
.venv/bin/python -m unittest discover -p 'test_*.py'   # 专项测试（无需 Photoshop）
./pack.sh                                              # 打源码 zip 到 dist/（Windows 用 pack.bat）
```

改动后两套测试都要跑。发布：测试通过 → 中文提交信息（第一行写给同事看的改动）→ `git push`，或把「三、发布更新」指令交给 AI。

这是**公开仓库**：不要提交 `.env`/Key、真实订单的客户名、订单 Excel、输出图片、模板 PSD 或本机用户路径。
