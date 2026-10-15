# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

<!-- PHASED_DELIVERY: ON  -->   判定日期 2026-10-12，理由：多模块 Python 桌面应用（GUI + 订单流水线 + AI 映射 + Photoshop 渲染），功能按阶段迭代交付。

## 项目概述

本地桌面工具（Flet GUI），读取订单 Excel，按 SKU（铝板画模式下按 SKU + 严格方向尺寸）匹配 PSD/PSB 模板，驱动 Photoshop 替换文字/切换图层显隐/替换图片后批量导出 PNG。用户文档：`README.md`、`AI安装与更新指令.md`（复制给 AI 的安装/更新/发布指令）、`操作步骤.txt`（给使用者的精简步骤）、`使用说明.txt`（完整说明，2026-10-14 按铝板画主流程重写：第五章为给美工的模板规范，第六章为出图规则，普通模板流程压缩为附录）；铝板画自动流程的方案与变更记录：`铝板画自动出图实施方案.md`（9.9 为标准模板规范）；实机验证记录：`实施验证结果-2026-10-10.md`（历史记录，只追加）。当前铝板画模板库：`~/Downloads/铝板画`（旧库 `铁皮画` 已停用）。

## 常用命令

项目使用 `.venv`（Python 3.11）。无 lint 配置。

**git 仓库**：公开仓库 https://github.com/LiuMR-del/plugin-psd-render（2026-10-14 建，用户确认公开），`main` 分支，同事通过 git 安装/更新。给 AI 的「首次安装 / 更新 / 发布更新」三段指令在 `AI安装与更新指令.md`（参照 project-hub 的做法：按 git remote 定位目录、冲突问用户、找不到不重新 clone、不动 .env 与数据）。**公开仓库红线**：不得提交 `.env`/Key、真实订单的客户名、订单 Excel、输出图片、模板 PSD、本机用户路径；文档和测试里的姓名一律用虚构名（2026-10-14 首次提交前已把真实订单名替换掉）。`.gitattributes` 固定 `*.bat` 为 CRLF、`*.sh`/`*.command` 为 LF。发布：两套测试通过 → 中文提交信息（第一行写给同事看的改动）→ `git push`；需要 zip 时再 `./pack.sh`。

```bash
./start.sh                      # 一键启动：自动装 Python/重建 venv/按 requirements 校验值装依赖/镜像备用；Windows 用 run.bat（须 CRLF，只用 goto）
.venv/bin/python main.py        # 直接启动 GUI

# 测试（均无需 Photoshop / GUI）
.venv/bin/python selftest.py                                   # 主自测，自定义 check() 断言计数（当前 194 项；unittest 106 项）
.venv/bin/python -m unittest discover -p 'test_*.py'           # unittest 专项测试
.venv/bin/python -m unittest test_order_pipeline.OrderPipelineTests.<test_name>   # 单个测试

bash start.sh selfcheck         # 运行环境自检（windows_selfcheck.py），结果在 自检结果/；Windows 双击 Windows自检.bat
./pack.sh                       # 打源码 zip 到 dist/（Python zipfile，中文名带 UTF-8 标记；排除 .env/.venv/自检结果）；Windows 用 pack.bat
flet pack main.py --name PSD批量生图 --add-data "render.jsx;."   # Windows 打 exe
```

改动后两套测试都要跑：`selftest.py` 覆盖原有纯逻辑模块，`test_*.py` 覆盖订单流水线 / 文字规则 / 文字颜色与字体 / 出图文件夹与订单表标黄 / 字体安装 / 档案查找 / 清理无效记录 / AI 客户端 / 运行时熔断 / 分析状态 / 本地候选。

## 架构

### 两条业务流程共用一个渲染出口

1. **普通模板流程**：`templates.py` 扫描 `{模板目录}/{SKU}/*.psd`（文件名 `-[变体值]` 表示变体）→ `excel_data.py` 解析 Excel 列 → `custom_fields.py` 解析 `定制信息` 文案并按编辑距离归组 → 用户在 GUI 中配置的图层别名/选择器/智能对象/图片映射（存于 `settings.py`）→ `renderer.render()`。
2. **铝板画自动流程**：`order_intake.py` 规范化订单尺寸（有 cm 用 cm，否则按写的数字 `written_key` 匹配——毛毯 `50"x60"` 对 `（50×60）`，再按 inch 换算兜底；方向严格，`30x20` ≠ `20x30`）→ `templates.pick_by_size()` 匹配平铺模板文件名 `SKU(宽×高cm)`（`_SIZE_IN_FILENAME_RE`，兼容旧写法 `SKU（宽-高）`、全/半角括号混用）→ `local_candidates.py` 本地确定性匹配人物素材候选 → 仅歧义字段交给 `ai_client.py` → `ai_mapping.py` 校验 AI 输出 → 用户审核后存入 `template_profiles.py` → `order_pipeline.compile_plan()` 编译渲染参数 → `renderer.render()`。

### 关键边界与不变量

- **Photoshop 协议冻结**：`renderer.py` 把参数写入临时 JSON，通过字符串替换 `render.jsx` 中的 `__PARAMS_JSON_PATH__` / `__RESULT_JSON_PATH__` 注入路径后执行（Windows 走 win32com，macOS 走 osascript），JSX 写回结果 JSON。参数键为 `replacements` / `pathReplacements` / `visibilityActions` / `smartObjectTextReplacements` / `imageReplacements`。方案明确要求**不修改该 JSX 参数协议**；新功能应在 `order_pipeline` 编译层适配，而不是改 JSX。唯一例外（2026-10-13 用户确认）：`pathReplacements` 条目可带可选键 `fitBox`（隐藏“范围”框坐标 + `maxScale` 1.3，由 `text_rules.fit_box` 识别、`compile_plan` 附加），JSX 按框缩放文字并在结果里返回 `textBoxFitted`；不带时行为不变（方案文档 9.7）。第二次例外（2026-10-15 用户确认，方案文档 9.12）：`pathReplacements` 条目可带可选键 `color`（`#RRGGBB`）/ `font`（字体家族名），由 `text_rules` 解析订单的文字颜色/字体字段、`compile_plan` 附加，JSX 写字后、缩放前应用（换家族保持模板字重，本机没有该字体时这一单报错）。`fitBox` 内可再带可选键 `syncGroup`（2026-10-14 用户需求：姓名为 `"name"`，由 `compile_plan` 按 `text_plan["name_layers"]` 附加），JSX 先写完同一文档的所有文字再统一缩放，同组按组内最小比例缩放（必要时退回行数更少的排法），保持名字一样大；只在同一文档内同步。单行要缩到 1/maxScale 以下时 JSX 在单词间空格处换为 2~3 行；有框的段落文字在写入前先转为点文字（标准样板的姓名/年份是段落文字）。
- **`order_pipeline.py` 是纯函数层**：不调 API、不调 Photoshop。`profile_to_legacy_configs()` 把审核档案转换为原版 settings 风格配置，铝板画流程因此复用原版文字替换/选择器逻辑（包括 `layer_aliases`）。`compile_plan()` 负责互斥容器的兄弟分支隐藏、父级 `No` 覆盖子级、共享智能对象的显隐合并。
- **人物素材走字段级规则**（`_material_plan`，见方案文档 9.1）：订单分区 → `Man素材/Woman素材`，字段名 → 编号素材组（`3-Man's Hair Color`），值 → 组内按图层名精确匹配。settings 中按样本学到的 selector 只对字段级规则解析不了的组兜底。`_check_material_scopes` 要求每个涉及的素材组最终恰好显示 1 个素材（`No` 为 0），`_check_material_containers` 要求承载这些素材组的智能对象至少一个在主文档可见，否则计划失败。出现素材错误时应修规则，不要按订单往 `config.json`/档案里补具体取值（这正是 10.12 问题的成因）。
- **姓名/年份走 `text_rules.py`**（方案文档 9.3）：字段按订单字段名分类（年份/男名/女名/通用姓名），图层按可见文字图层的名称/内容分角色（含年份或 since/est → 年份图层）。已审核档案（`text_reviewed` 或非空 `text_mappings`，存于模板库档案）优先，否则用本地规则（1 年份对 1 年份图层；男女名对左右两个不重叠的姓名图层，左男右女）；规则判断不了的交给 AI，再人工审核。`compile_plan` 要求每个有值的姓名/年份字段落到独立的、角色匹配的可见文字图层，否则该订单失败；这些字段不再经过旧的别名/位置兜底，审核也不再写 `config.json` 的 SKU 级 `layer_aliases`。铝板画出图时 `main.aluminum_plan_cfg()` 把已审核档案以 `cfg["profile"]` 传给 `compile_plan`。其他文字字段（Title、Initial 等，kind `text`）只按档案映射写入，本地规则不猜；文字颜色/字体字段见 9.12（档案 `style_mappings`、`color_values`）。年份写入用 `text_rules.year_text()`：按模板图层原文只替换其中唯一的 4 位年份（`Since 1989` → `Since 1980`），不依赖图层名（2026-10-14，标准模板图层名为 `year` 时曾丢掉 Since）。
- **按订单分析**（`order_analysis.py`，方案文档 9.6）：`field_coverage()` 判断一单的字段是否都被模板规则覆盖；覆盖则复用（不调 AI），否则把这一单的完整定制文案交给 AI（`ai_client.analyze_order`），`merge_rules()` + `check_candidate()` 通过即自动 `approve_profile`，否则写入档案 `pending_reviews[签名]` 等人工审核。新流程档案带 `rules_version: 2`；旧的已审核档案不带，保持“approved 即可生成”。`main.py` 中对应 `refresh_order_summary` → `state["order_needs"]` → `schedule_auto_analysis`/`_analyze_order`（单并发）→ `show_ai_review_dialog`/`_pending_card`。
- **出图结果排查**：先用 `compile_plan()` 生成计划，再用 psd-tools 读取智能对象内图层的自身显隐、叠加计划动作，模拟每个素材组的最终显示，无需启动 Photoshop。注意模板缓存里的 `visible` 是叠加父级后的有效显隐，不是图层自身的显隐。
- **两类持久化位置不同**：
  - 用户级 UI 配置：`~/.plugin-psd-render/config.json`（`settings.py`，按「模板目录 → SKU」两级记忆）；AI 熔断状态：`~/.plugin-psd-render/ai-runtime.json`（`ai_runtime.py`，只存 Key 指纹）。
  - 模板库级：`{模板目录}/.psd-render-profiles.json`（模板档案，只存「字段 → 完整图层路径」规则，不存订单具体值）；`{模板目录}/.psd-render-cache.json`（psd-tools 解析缓存，按 mtime+size 失效）。
- **档案复用以结构签名为准**：`templates.structure_signature()` 由画布尺寸 + 图层树计算（bbox、文字内容不参与，缓存 v8 才开始记录这两项）；档案 key 为 SKU + 尺寸 + 文件名（+绝对路径），模板库换路径/换机器时按 SKU + 尺寸 + 文件名唯一匹配兜底。签名变化 → 档案失效回到待审核。已审核且签名一致的模板不调用 AI。
- **清理无效记录**（`library_cleanup.py`，方案文档 9.10）：按 `template_profiles.matched_profile_key`（与 `get_profile` 同一套匹配）找出无主档案，另列其他/已不存在模板库的 `config.json` 配置，用户确认后才删；从不删 PSD 或当前库配置。无主缩略图由 `scan_templates` 每次扫描自动删除（有子目录读不到、`stats["complete"]` 为 False 时不删，也不允许清理）。档案的 SKU + 尺寸 + 文件名兜底只匹配绑定 PSD 已不存在的档案。
- **铝板画出图范围**：`render_rows()` 先按勾选范围取行（`state["selected_rows"]`，空 = 全部；预检同样只查这些行），`run_render` 再筛掉非“可生成”订单（不计入进度和失败，日志一行汇总），只渲染“可生成”订单；模板没有可编辑文字图层而订单有姓名/年份时订单标为异常并跳过（`text_rules.lacks_text_layers`）；待审核/缺模板/异常订单在预检中列为警告并记入失败报告，不阻止整批。铝板画模式下预检用 `compile_plan` 逐单预编译，跳过普通模板流程的列/别名/拆分检查。
- **出图文件夹与订单表标黄**（方案文档 9.14）：图片写到 `{输出目录}/{月.日}/{店铺名}/{品类}/`（`output_layout.py`，品类默认取订单表文件名去掉开头日期，界面「品类文件夹」可改），日志/失败报告带品类写在日期文件夹。生成结束后 `excel_marker.mark_rows()` 把全部成功的订单行在原表整行标黄：订单表嵌有商品图，**不能用 openpyxl 保存（会丢图）**，只改 `styles.xml` 和第一个工作表的 XML，先备份到表旁 `备份/`，再原子替换。
- **单实例**：`main.py` 启动时用 `single_instance.acquire()`（`~/.plugin-psd-render/app.lock`）加锁，已在运行则弹窗并退出。
- **字体**：每次扫描模板库后 `font_installer.install_library_fonts()` 把库内缺少的 `.otf/.ttf/.ttc/.otc`（按 PostScript 名比对）复制到当前用户字体目录，不覆盖已有文件；`run_render` 开始前再查一次，装了新字体就用 `renderer.quit_photoshop()` 正常退出 Photoshop（不强杀）。开跑前查缺字体（2026-10-14 用户需求）：解析模板时按文字段落（StyleRun）记录每个文字图层实际用的字体到缓存 `fonts`（缓存 v9；`FontSet` 本身会带上没用到的 `AdobeHeitiStd`，不能直接用；字体不参与结构签名），`main.check_fonts_then_start()` 在预检确认后、`start_task` 前先装库内字体，再用 `font_installer.missing_template_fonts()` 查本次可生成订单涉及模板的可见文字字体，缺了弹窗“关闭（去补充字体）/继续生成”。已装字体的来源：Photoshop 开着时用 `renderer.photoshop_fonts()` 跑只读 JSX 取 `app.fonts` 的 PostScript 名（不启动、不强杀 Photoshop，45 秒没响应返回 None），并入库内字体名（刚装的要重启 Photoshop 才认，开跑时会自动重启）；没开或没响应时扫描字体文件夹。检查在后台线程跑，界面显示“正在检查字体…”。Windows 分支未实机验证。
- **AI 配置与安全**：URL/Key/模型写在项目根 `.env`（`PSD_RENDER_AI_BASE_URL` / `PSD_RENDER_AI_API_KEY` / `PSD_RENDER_AI_MODELS`）。2026-10-14 起只保留一个 API：`ai_client.provider_configs()` 只返回 `.env` 这一个，忽略旧的 `config.json` `ai_profiles`（备用 API），保存 AI 设置时清掉 `.env` 里旧的 `PSD_RENDER_AI_*_KEY` 和 `ai_profiles`；`request_json_providers` 仍是通用实现，失败时保留底层错误类别。中转站各家模型统一走 OpenAI 兼容 `/v1/chat/completions`；服务点名拒绝 `reasoning_effort` / `response_format` 时去掉该参数重发。AI 分析失败（任何类别）由 `main._show_ai_problem_alert` 弹窗请用户更换 API，同一次运行只弹一次，重新保存 AI 设置后恢复。Key 不得写入 config JSON、模板档案、日志。发给 AI 的只能是 SKU、尺寸、文件名、定制字段、图层树，不得包含电话/地址等 PII 或 PSD/图片二进制。`API_KEY_QUOTA_EXHAUSTED` 触发 provider 级熔断，不再尝试同 provider 的其他模型。
- **AI 只给建议**：AI 结果先存为草稿，用户审核通过才绑定模板并允许出图；低置信度/路径不存在/智能对象路径不确定的条目会阻止审核通过。

### `main.py`

约 4500 行的单文件 Flet GUI，所有页面和回调都是 `main(page)` 内的嵌套闭包；AI 分析和渲染在后台线程执行，通过 `page.update()` 刷新界面。模块级纯函数（图层路径、选择器、智能对象任务合并等）可被 `selftest.py` 直接 import 测试。运行中的旧 GUI 进程可能用内存中的旧档案覆盖 `.psd-render-profiles.json`——手动改档案前先关闭应用；改了代码要让用户重启应用才生效。“导出预处理 Excel”及读取预处理结果（`预检备注` sheet）的代码已于 2026-10-13 删除。

### 一次性脚本

`fix_backfill_1012.py`、`render_9_1012.py`、`inspect_*.jsx` 是针对特定日期订单的临时脚本，内含硬编码的本机路径，不属于应用代码，已在 `.gitignore` 中排除，只留在维护者本机。`test_*.py` 均用相对路径，可在 Windows 上运行。

## 运行环境注意

- Windows 已实机验证：自检、COM 连接与出图、Since/姓名同步（2026-10-14）。尚未在 Windows 上验证：开跑前问 Photoshop 字体（`photoshop_fonts` 的 COM 分支）、AI 安装/更新指令全流程、界面验收清单其余项（方案文档 9.11）。`renderer` 写 `task.jsx` 必须带 BOM（`utf-8-sig`）。
- **用户文档要随功能同步**：改了界面操作、出图规则或提示文字，同步改 `使用说明.txt` 对应章节（必要时 `操作步骤.txt`），按“现在是什么样”写，不写成改动记录（日期和原因写进方案文档变更记录）；界面按钮名以 `main.py` 里的实际文字为准。
- 渲染需要本机安装 Photoshop CC 2018+ 和模板所用字体（缺字体时 Photoshop 会静默替换，结果中 `fontWarnings` 会提示）。
- macOS 需授予终端/应用控制 Photoshop 的「自动化」权限；首次 AppleEvent 超时可重启 Photoshop 后重试。
- 渲染看门狗（`renderer`）：`ensure_photoshop_ready` 先单独启动并等 Photoshop 响应（300s）；`render.jsx` 每步往 `RESULT_PATH + ".progress"` 追加 `毫秒\t步骤`，`_watch` 在某步超过 `STALL_TIMEOUT`（180s）无进展或单张超过 `MAX_RENDER_SECONDS`（900s）时强杀 Photoshop 并重试 1 次，报错带步骤用时。Windows 上所有 COM 调用必须经 `_start_com_call`（线程内 `CoInitialize`）。JSX 按路径找图层统一走 `layerFinder`（子图层名按路径缓存），显隐只在与目标不同时才写（写 `visible` 会重绘）；不要回到逐项从根遍历、无条件写入（同一单 54 秒 → 4.5 秒）。
- 输出文件名同批重名（`excel_data.output_name_key`，casefold）时后一单失败，预检警告。
- `flet` 固定为 0.28.2（0.28.3 在 macOS 上 FilePicker 失效）。
