"""纯逻辑模块最小自测(无需 Photoshop / GUI). 运行: python selftest.py

覆盖:
- templates: 文件名解析 / 目录扫描索引 / 智能对象识别 / 变体回退 pick / 缓存命中·失效·清理
- excel_data: 列识别 / hash 模式判定 / 无效与重复 hash / 缺 SKU / 输出命名
- custom_fields: 定制信息解析 / key 编辑距离归组 / 拆值数量校验 / 逐图层分配
- image_assets: 素材扫描 / 显式映射 / 安全路径解析
- template_sync: 多模板文字配置收集 / 同步预览 / 缺失图层忽略
- settings: 读写回填 / 变体字段、智能对象与图片替换映射记忆
- renderer: 普通/智能对象/图片参数 JSON 注入 / 结果回执 / 超时看门狗 + 重试(mock _execute)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import ai_client
import ai_mapping
import custom_fields
import excel_data
import image_assets
import main as app_main
import order_intake
import renderer
import settings
import template_profiles
import template_sync
import templates

PASSED = 0


def check(cond, message):
    global PASSED
    assert cond, message
    PASSED += 1
    print(f"  ok: {message}")


# ---------- templates ----------

def test_templates():
    print("[templates]")
    check(templates.parse_filename("父亲节卡片.psd") == ("父亲节卡片", ""), "无变体文件名解析")
    check(templates.parse_filename("父亲节卡片-[红色].psd") == ("父亲节卡片", "红色"), "变体文件名解析")
    check(templates.parse_filename("a-b-[x-y].psd") == ("a-b", "x-y"), "变体值含连字符")
    check(templates.parse_filename("贺卡.psb") == ("贺卡", ""), "psb 无变体文件名解析")
    check(templates.parse_filename("贺卡-[大尺寸].psb") == ("贺卡", "大尺寸"), "psb 变体文件名解析")

    root = tempfile.mkdtemp(prefix="tpl-test-")
    try:
        # 51052005/贺卡.psb 用 PSB 扩展名, 验证 .psb 与 .psd 一视同仁被索引
        for rel in ["51052004/父亲节卡片.psd", "51052004/父亲节卡片-[红色].psd",
                    "51052004/父亲节卡片-[蓝色].psd", "51052005/贺卡.psb"]:
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"fake-psd-" + rel.encode())

        calls = []
        original = templates._parse_psd
        templates._parse_psd = lambda p: (calls.append(p) or
                                          {"text_layers": ["姓名", "日期"], "width": 2480, "height": 3508})
        try:
            index, stats = templates.scan_templates(root)
            check(stats["sku_count"] == 2 and stats["template_count"] == 4, "扫描到 2 SKU / 4 模版")
            check(stats["variant_count"] == 2, "识别 2 个变体模版")
            check(stats["cache_hits"] == 0 and len(calls) == 4, "首次扫描全量解析")
            check(os.path.exists(os.path.join(root, templates.CACHE_FILENAME)), "缓存文件已写入")
            check(index["51052004"][0].text_layers == ["姓名", "日期"], "文字图层名已收集")

            # pick: 变体精确匹配 / 回退默认 / 未知 sku
            got = templates.pick(index, "51052004", "红色")
            check(len(got) == 1 and got[0].field_value == "红色", "pick 变体精确匹配")
            got = templates.pick(index, "51052004", "绿色")
            check(len(got) == 1 and got[0].field_value == "", "pick 无匹配变体回退默认")
            got = templates.pick(index, "51052004", "")
            check(len(got) == 1 and got[0].field_value == "", "pick 默认模版")
            check(templates.pick(index, "99999999") == [], "pick 未知 sku 返回空")

            # 缓存命中: 二次扫描不再解析
            calls.clear()
            _, stats = templates.scan_templates(root)
            check(stats["cache_hits"] == 4 and not calls, "二次扫描全部缓存命中")

            # mtime 变化 -> 重新解析该文件
            target = os.path.join(root, "51052004/贺卡新增.psd")
            victim = os.path.join(root, "51052005/贺卡.psb")
            os.utime(victim, (os.stat(victim).st_atime, os.stat(victim).st_mtime + 100))
            calls.clear()
            _, stats = templates.scan_templates(root)
            check(stats["cache_hits"] == 3 and len(calls) == 1, "mtime 变化仅重解析该文件")

            # 删除文件 -> 缓存条目清除
            os.remove(os.path.join(root, "51052004/父亲节卡片-[蓝色].psd"))
            index, stats = templates.scan_templates(root)
            with open(os.path.join(root, templates.CACHE_FILENAME), encoding="utf-8") as f:
                entries = json.load(f)["entries"]
            check("51052004/父亲节卡片-[蓝色].psd" not in entries, "已删文件从缓存清除")
            check(stats["template_count"] == 3, "索引同步减少")

            # 解析失败不中断扫描
            def bad_parse(p):
                raise RuntimeError("broken psd")
            templates._parse_psd = bad_parse
            index, stats = templates.scan_templates(root, force=True)
            check(len(stats["errors"]) == 3 and stats["template_count"] == 3, "坏文件记错误但不中断")
            _ = target
        finally:
            templates._parse_psd = original
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_smart_object_parsing():
    print("[smart objects]")
    from psd_tools import PSDImage

    class FakeLayer:
        def __init__(self, name, kind="pixel", visible=True, children=None, smart=None):
            self.name = name
            self.kind = kind
            self.visible = visible
            self._children = children
            self.smart_object = smart
            self.bbox = (1, 2, 31, 42)  # psd-tools 1.24: (left, top, right, bottom)

        def is_group(self):
            return self._children is not None

        def __iter__(self):
            return iter(self._children or [])

    class FakeSmart:
        def __init__(self, object_id, kind="data", is_psd=True, filename="内容.psb"):
            self.unique_id = object_id
            self.kind = kind
            self.filetype = "8bpb" if is_psd else "png"
            self.filename = filename
            self._is_psd = is_psd

        def is_psd(self):
            return self._is_psd

        @contextlib.contextmanager
        def open(self):
            yield io.BytesIO(b"embedded")

    class FakePSD(list):
        width = 100
        height = 200

    embedded = FakePSD([
        FakeLayer("标题组", children=[FakeLayer("姓名", kind="type")]),
        FakeLayer("二级对象", kind="smartobject", smart=FakeSmart("nested")),
    ])
    root_psd = FakePSD([
        FakeLayer("可编辑对象", kind="smartobject", smart=FakeSmart("shared-1")),
        FakeLayer("链接对象", kind="smartobject", smart=FakeSmart("linked", kind="external")),
        FakeLayer("图片对象", kind="smartobject", smart=FakeSmart("image", is_psd=False,
                                                                  filename="照片.png")),
    ])

    original_open = PSDImage.__dict__["open"]
    PSDImage.open = classmethod(
        lambda cls, source: embedded if hasattr(source, "read") else root_psd
    )
    try:
        parsed = templates._parse_psd("fake.psd")
    finally:
        PSDImage.open = original_open

    editable = parsed["all_layers"][0]["smart_object"]
    linked = parsed["all_layers"][1]["smart_object"]
    image = parsed["all_layers"][2]["smart_object"]
    check(parsed["all_layers"][0]["bbox"] == [1, 2, 31, 42],
          "模版扫描记录图层像素边界")
    check(editable["editable"] and editable["id"] == "shared-1", "嵌入式 PSD/PSB 标记为可编辑")
    check([layer["path"] for layer in editable["layers"]] ==
          ["标题组", "标题组/姓名", "二级对象"], "智能对象内部图层按完整路径扫描")
    check(editable["layers"][2]["smart_object"]["editable"] is False,
          "内部再次嵌套的智能对象不继续解析")
    check(not linked["editable"] and "链接型" in linked["reason"], "链接型智能对象安全降级")
    check(not image["editable"] and "不是嵌入式" in image["reason"], "图片智能对象安全降级")


def test_smart_object_helpers():
    encoded = app_main.smart_target_parts(["正面", "对象"], ["标题组", "姓名"])
    check(app_main.parse_smart_target_parts(encoded) ==
          (["正面", "对象"], ["标题组", "姓名"]), "智能对象文字虚拟路径可逆")
    check(app_main.layer_kind_label("smartobject") == "智能对象", "配置树显示智能对象类型")
    check(app_main.is_raster_image_smart_object({
        "kind": "smartobject",
        "smart_object": {"source_kind": "data", "file_type": "png", "filename": "1.png"},
    }), "嵌入式 PNG 智能对象可作为图片替换目标")
    check(app_main.is_raster_image_smart_object({
        "kind": "smartobject",
        "smart_object": {"source_kind": "data", "file_type": "", "filename": "照片.JPEG"},
    }), "按智能对象文件名识别 JPEG 图片目标")
    check(not app_main.is_raster_image_smart_object({
        "kind": "smartobject",
        "smart_object": {"source_kind": "data", "file_type": "8bpb", "filename": "内容.psb"},
    }), "PSD/PSB 智能对象不作为整层图片替换目标")
    alias_config = {"selectors": [{
        "parent": [], "relative_text_aliases": {"姓名": "客户姓名"},
    }, {
        "parent": ["正面"], "relative_text_aliases": {"日期": "纪念日"},
    }]}
    check(app_main.relative_text_alias_for_path(
        alias_config, ["款式A", "姓名"],
    ) == "客户姓名" and app_main.relative_text_alias_for_path(
        alias_config, ["正面", "款式A", "日期"],
    ) == "纪念日", "候选组内文字图层按层级读取模板独立列映射")
    check(app_main.relative_text_alias_for_path(
        alias_config, ["姓名"],
    ) == "", "顶层文字继续使用 SKU 共享映射")
    shared_images = app_main.consolidate_image_replacement_actions([
        {
            "smartObjectId": "shared-image", "path": ["图片1"],
            "sourcePath": "/tmp/a.png", "excelValue": "A",
        },
        {
            "smartObjectId": "shared-image", "path": ["图片1副本"],
            "sourcePath": "/tmp/a.png", "excelValue": "A",
        },
    ])
    check(len(shared_images) == 1, "共享图片智能对象映射相同素材时只替换一次")
    try:
        app_main.consolidate_image_replacement_actions([
            {"smartObjectId": "shared-image", "path": ["图片1"], "sourcePath": "/tmp/a.png"},
            {"smartObjectId": "shared-image", "path": ["图片1副本"], "sourcePath": "/tmp/b.png"},
        ])
        raise AssertionError("共享图片智能对象映射不同素材应报错")
    except ValueError as exc:
        check("共享图片智能对象配置冲突" in str(exc), "共享图片智能对象不同素材阻止生成")
    selectors = [{
        "parent": ["款式"],
        "column": "内部款式",
        "candidates": [
            {"path": ["款式", "A"], "value": "A"},
            {"path": ["款式", "B"], "value": "B"},
        ],
        "relative_text_aliases": {"姓名": "客户姓名"},
    }]
    visibility, replacements = app_main.build_selector_actions(
        selectors,
        {"内部款式": "B", "客户姓名": "Alice"},
        {"款式/A/姓名", "款式/B/姓名"},
    )
    check(visibility == [
        {"path": ["款式"], "visible": True},
        {"path": ["款式", "A"], "visible": False},
        {"path": ["款式", "B"], "visible": False},
        {"path": ["款式", "B"], "visible": True},
    ], "智能对象内部选择器先隐藏全部候选再显示命中图层")
    check(replacements == [
        {"path": ["款式", "B", "姓名"], "value": "Alice"},
    ], "智能对象内部命中图层沿用相对文字字段映射")
    unchanged = app_main.build_selector_actions(
        selectors, {"内部款式": "未配置"}, {"款式/A/姓名", "款式/B/姓名"},
    )
    check(unchanged == ([], []), "智能对象内部列值未命中时保留 PSD 默认显隐")
    repeated_column_selectors = [
        {
            "parent": ["back1"],
            "column": "background",
            "candidates": [
                {"path": ["back1", "red"], "value": "red"},
                {"path": ["back1", "blue"], "value": "blue"},
            ],
            "relative_text_aliases": {},
        },
        {
            "parent": ["back2"],
            "column": "background",
            "candidates": [
                {"path": ["back2", "red"], "value": "red"},
                {"path": ["back2", "blue"], "value": "blue"},
            ],
            "relative_text_aliases": {},
        },
    ]
    repeated_visibility, _ = app_main.build_selector_actions(
        repeated_column_selectors, {"background": "red"}, set(),
    )
    check(
        {tuple(item["path"]) for item in repeated_visibility if item["visible"]} == {
            ("back1",), ("back1", "red"), ("back2",), ("back2", "red"),
        },
        "同一 Excel 列可同时控制多个图层组并显示各组命中图层",
    )
    repeated_candidate_visibility, _ = app_main.build_selector_actions(
        [{
            "parent": [],
            "column": "background",
            "candidates": [
                {"path": ["back1"], "value": "red"},
                {"path": ["back2"], "value": "red"},
            ],
            "relative_text_aliases": {},
        }],
        {"background": "red"},
        set(),
    )
    check(
        [item["path"] for item in repeated_candidate_visibility if item["visible"]] == [
            ["back1"], ["back2"],
        ],
        "同一切换规则允许一个 Excel 值同时显示多个候选组",
    )
    shared = [
        {"smartObjectId": "same", "smartObjectPath": ["对象1"],
         "visibilityActions": [{"path": ["款式A"], "visible": True}],
         "pathReplacements": [{"path": ["姓名"], "value": "Alice"}]},
        {"smartObjectId": "same", "smartObjectPath": ["对象2"],
         "visibilityActions": [{"path": ["款式A"], "visible": True}],
         "pathReplacements": [{"path": ["姓名"], "value": "Alice"}]},
    ]
    check(len(app_main.consolidate_smart_object_tasks(shared)) == 1,
          "共享智能对象相同替换任务只执行一次")
    shared[1]["pathReplacements"][0]["value"] = "Bob"
    try:
        app_main.consolidate_smart_object_tasks(shared)
        raise AssertionError("共享智能对象不同映射应报错")
    except ValueError as exc:
        message = str(exc)
        check(all(text in message for text in (
            "共享智能对象配置冲突",
            "引用 A：对象1",
            "引用 B：对象2",
            "文字替换 · 内部图层「姓名」",
            "引用 A：替换为「Alice」",
            "引用 B：替换为「Bob」",
            "处理方法",
        )), "共享智能对象不同替换值显示引用、内部路径和双方结果")
    shared[1]["pathReplacements"][0]["value"] = "Alice"
    shared[1]["visibilityActions"][0]["visible"] = False
    try:
        app_main.consolidate_smart_object_tasks(shared)
        raise AssertionError("共享智能对象不同显隐应报错")
    except ValueError as exc:
        message = str(exc)
        check(all(text in message for text in (
            "图层显隐 · 内部图层「款式A」",
            "引用 A：显示",
            "引用 B：隐藏",
        )), "共享智能对象不同显隐显示内部路径和双方状态")
    shared[1]["visibilityActions"] = []
    merged = app_main.consolidate_smart_object_tasks(shared)
    check(len(merged) == 1 and merged[0]["visibilityActions"] == [
        {"path": ["款式A"], "visible": True},
    ], "共享智能对象一边未配置显隐时采用另一边的明确配置")

    reverse_missing = [
        {"smartObjectId": "reverse", "smartObjectPath": ["对象1"]},
        {"smartObjectId": "reverse", "smartObjectPath": ["对象2"],
         "visibilityActions": [{"path": ["款式A"], "visible": False}],
         "pathReplacements": [{"path": ["姓名"], "value": "Alice"}]},
    ]
    reverse_merged = app_main.consolidate_smart_object_tasks(reverse_missing)
    check(len(reverse_merged) == 1 and
          reverse_merged[0]["visibilityActions"] == [
              {"path": ["款式A"], "visible": False},
          ] and reverse_merged[0]["pathReplacements"] == [
              {"path": ["姓名"], "value": "Alice"},
          ], "共享智能对象首个引用未配置时合并后续引用的明确配置")

    disjoint = [
        {"smartObjectId": "disjoint", "smartObjectPath": ["对象1"],
         "visibilityActions": [{"path": ["款式A"], "visible": True}]},
        {"smartObjectId": "disjoint", "smartObjectPath": ["对象2"],
         "visibilityActions": [{"path": ["背景B"], "visible": False}]},
    ]
    disjoint_merged = app_main.consolidate_smart_object_tasks(disjoint)
    check(disjoint_merged[0]["visibilityActions"] == [
        {"path": ["款式A"], "visible": True},
        {"path": ["背景B"], "visible": False},
    ], "共享智能对象配置不同内部位置时合并配置")

    equivalent = [
        {"smartObjectId": "ordered", "smartObjectPath": ["对象1"],
         "visibilityActions": [
             {"path": ["款式A"], "visible": False},
             {"path": ["款式B"], "visible": True},
         ]},
        {"smartObjectId": "ordered", "smartObjectPath": ["对象2"],
         "visibilityActions": [
             {"path": ["款式B"], "visible": True},
             {"path": ["款式A"], "visible": False},
         ]},
    ]
    check(len(app_main.consolidate_smart_object_tasks(equivalent)) == 1,
          "共享智能对象最终结果相同但操作排列不同时不误报")


# ---------- excel_data ----------

def _write_xlsx(path, headers, rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_excel():
    print("[excel_data]")
    root = tempfile.mkdtemp(prefix="xlsx-test-")
    try:
        p1 = os.path.join(root, "hash.xlsx")
        _write_xlsx(p1, ["Hash", "SKU", "颜色", "姓名", "祝福语"], [
            ["aB3kF9x2", "51052004", "红色", "张三", "生日快乐"],
            ["xK2mP8q1", "51052004", "", "李四", None],
            ["aB3kF9x2", "51052004", "蓝色", "王五", ""],      # hash 重复
            ["bad", "51052005", "", "赵六", ""],               # hash 无效
            [None, None, None, None, None],                     # 全空行
            ["cD4eF5g6", "", "", "钱七", ""],                   # 缺 SKU
        ])
        data = excel_data.parse_excel(p1)
        check(data.hash_mode, "检测到 Hash 列启用 hash 模式")
        check(data.custom_columns == ["颜色", "姓名", "祝福语"], "定制列识别(排除 SKU/Hash)")
        check(len(data.rows) == 2, "有效行 2 行(重复/无效/缺SKU剔除)")
        check(len(data.skipped) == 3, "跳过 3 行")
        reasons = {row_no: reason for row_no, reason in data.skipped}
        check("重复" in reasons[4] and "无效" in reasons[5] and "SKU" in reasons[7], "跳过原因正确")
        check(data.rows[0].values == {"颜色": "红色", "姓名": "张三", "祝福语": "生日快乐"}, "替换文案收集, 空单元格跳过")
        custom_named_data = excel_data.parse_excel(p1, name_column="姓名")
        check(not custom_named_data.hash_mode and len(custom_named_data.rows) == 4 and
              all("Hash" not in reason for _, reason in custom_named_data.skipped),
              "配置图片命名列后不再按旧 Hash 规则过滤无效或重复行")

        # 输出命名
        row = data.rows[0]
        check(excel_data.output_basename(row, True, "颜色", 1) == "aB3kF9x2-1.png", "hash 模式命名")
        check(excel_data.output_basename(row, False, "颜色", 2) == "51052004-红色-2-2.png", "普通模式命名(含变体值)")
        check(excel_data.output_basename(data.rows[1], False, "颜色", 1) == "51052004-3-1.png", "普通模式命名(无变体值)")
        check(excel_data.sanitize_filename('a/b:c*?"<>|d') == "a_b_c______d", "文件名非法字符替换")
        check(excel_data.output_basename(
            row, True, "颜色", 1, name_column="姓名", template_count=1,
        ) == "张三.png", "配置图片命名列后单模板直接使用 Excel 单元格值")
        check(excel_data.output_basename(
            row, True, "颜色", 2, name_column="姓名", template_count=3,
        ) == "张三-2.png", "配置图片命名列后多模板追加模板序号")
        check(excel_data.row_column_value(row, "SKU") == "51052004" and
              excel_data.row_column_value(row, "Hash") == "aB3kF9x2",
              "图片命名列支持读取 SKU 和 Hash 保留列")
        png_named_row = excel_data.ExcelRow(
            row_no=8, sku="51052004", values={"文件名": "订单/001.png"},
        )
        check(excel_data.output_basename(
            png_named_row, False, "", 1, name_column="文件名",
        ) == "订单_001.png", "自定义文件名清理非法字符且不重复追加 png 扩展名")
        try:
            excel_data.output_basename(
                data.rows[1], False, "", 1, name_column="祝福语",
            )
            raise AssertionError("图片命名列空值应报错")
        except ValueError as exc:
            check("图片命名列" in str(exc) and "为空" in str(exc),
                  "图片命名列单元格为空时返回明确错误")

        # 无 Hash 列 / SKU 大小写不敏感 / 数字 sku
        p2 = os.path.join(root, "plain.xlsx")
        _write_xlsx(p2, ["sku", "姓名"], [[51052004, "张三"]])
        data2 = excel_data.parse_excel(p2)
        check(not data2.hash_mode and data2.rows[0].sku == "51052004", "无 Hash 列普通模式 + 数字 SKU 转字符串")

        # 缺 SKU 列报错
        p3 = os.path.join(root, "nosku.xlsx")
        _write_xlsx(p3, ["姓名"], [["张三"]])
        try:
            excel_data.parse_excel(p3)
            raise AssertionError("缺 SKU 列应报错")
        except ValueError:
            check(True, "缺 SKU 列抛 ValueError")

        # 交易编号命名模式(无 hash 列)
        p4 = os.path.join(root, "order.xlsx")
        _write_xlsx(p4, ["交易编号", "SKU", "颜色", "姓名"], [
            ["T20260101A", "51052004", "红色", "张三"],
            ["", "51052004", "蓝色", "李四"],          # 缺交易编号 -> 跳过
            ["T20260101B", "51052005", "", "王五"],
        ])
        data4 = excel_data.parse_excel(p4)
        check(data4.order_mode and not data4.hash_mode, "检测到交易编号列启用 order 模式")
        check(data4.custom_columns == ["颜色", "姓名"], "定制列识别(排除 SKU/交易编号)")
        check(len(data4.rows) == 2, "缺交易编号行被剔除")
        custom_named_order_data = excel_data.parse_excel(p4, name_column="姓名")
        check(not custom_named_order_data.order_mode and
              len(custom_named_order_data.rows) == 3,
              "配置图片命名列后不再因交易编号为空跳过订单")
        skip4 = {row_no: reason for row_no, reason in data4.skipped}
        check("交易编号" in skip4[3], "缺交易编号跳过原因正确")
        check(data4.rows[0].order_no == "T20260101A", "交易编号已收集")
        check(excel_data.output_basename(data4.rows[0], False, "颜色", 1) == "T20260101A.png",
              "交易编号模式仅使用交易编号命名")

        # Hash 与交易编号同时存在: hash 优先, 命名不变, 空交易编号不跳行
        p5 = os.path.join(root, "hash_order.xlsx")
        _write_xlsx(p5, ["Hash", "交易编号", "SKU", "姓名"], [
            ["aB3kF9x2", "", "51052004", "张三"],       # 空交易编号但 hash 有效 -> 保留
            ["cD4eF5g6", "T20260101C", "51052005", "李四"],
        ])
        data5 = excel_data.parse_excel(p5)
        check(data5.hash_mode and not data5.order_mode, "hash 与交易编号并存时 hash 优先")
        check(len(data5.rows) == 2, "hash 模式下空交易编号不跳行")
        check(excel_data.output_basename(data5.rows[0], True, "", 1) == "aB3kF9x2-1.png",
              "hash 优先命名不受交易编号影响")

        # 定制信息列: 从普通图层列中排除并保存原文
        p6 = os.path.join(root, "custom-info.xlsx")
        custom_text = "Names: Alice, Bob\nDate: 2026-07-18"
        _write_xlsx(p6, ["SKU", "定制文案", "姓名"], [["51052004", custom_text, "备用姓名"]])
        data6 = excel_data.parse_excel(p6)
        check(data6.custom_info_column == "定制文案", "识别定制文案列")
        check(data6.custom_columns == ["姓名"], "定制文案列不作为普通图层列")
        check(data6.rows[0].custom_info == custom_text and
              data6.rows[0].values == {"姓名": "备用姓名"}, "定制信息原文独立保存")

        p7 = os.path.join(root, "custom-info-alias.xlsx")
        _write_xlsx(p7, ["SKU", "定制信息"], [["51052004", "Name: Alice"]])
        data7 = excel_data.parse_excel(p7)
        check(data7.custom_info_column == "定制信息" and
              data7.rows[0].custom_info == "Name: Alice", "继续兼容定制信息列名")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------- custom_fields ----------

def test_custom_fields():
    print("[custom_fields]")
    parsed = custom_fields.parse_custom_info(
        "无键开头\nNames: Alice， Bob\nNames：Cara; Dan\nNote: Dillon & Rachael"
    )
    check(parsed.ignored_lines == ["无键开头"], "开头无 key 内容进入预检提示")
    check(parsed.pairs == [
        ("Names", "Alice， Bob"),
        ("Names", "Cara; Dan"),
        ("Note", "Dillon & Rachael"),
    ], "中英文冒号解析并保留重复 key 顺序")
    split_fields = custom_fields.split_custom_info_fields(
        "kid name: Alice\nkids name: Bob\nDate: 2026"
    )
    check([(field.keys, field.values) for field in split_fields] == [
        (["kid name", "kids name"], ["Alice", "Bob"]),
        (["Date"], ["2026"]),
    ], "相似 key 归为同一拆分字段并保持值顺序")
    check(custom_fields.split_field_value("Alice， Bob; Cara；Dan") ==
          ["Alice", "Bob", "Cara", "Dan"], "中英文逗号和分号拆值")
    check(custom_fields.split_field_value("Dillon & Rachael / Family") ==
          ["Dillon & Rachael / Family"], "& 和 / 不拆值")
    kids_names = "Nathan Haiden Liam Landon Easton Xavier Anastasia Emalyn"
    check(custom_fields.split_field_value(kids_names, "Enter Kids' Name") ==
          ["Nathan", "Haiden", "Liam", "Landon", "Easton", "Xavier", "Anastasia", "Emalyn"],
          "Enter Kids' Name 按连续空格拆值")
    check(custom_fields.split_field_value(kids_names, "Enter Adult Name") == [kids_names],
          "其他 key 不启用空格拆值")
    check(custom_fields.split_field_value('“Ralph”, "Bettina"，”Alvin“') ==
          ["Ralph", "Bettina", "Alvin"], "拆分结果去除首尾中英文双引号")
    pair_key = "Enter Name + Character Code （Please do not enter names only.）"
    plain_pair_value = (
        "Rosalind 13 Henry 10  Nora 6. Stella 13  Al 12. "
        "LeRoy 11. Jodi 5  Josie 16. Anya 5."
    )
    hash_pair_value = (
        "Brett#7, Emma #1, Bailey #4, Abby #2, Boyd #12, Blake #10, "
        "Clara #5, Reed #11, MacKay #7, Ivy #3, Wade #9, Simon #8"
    )
    check(custom_fields.is_name_character_key(pair_key) and
          custom_fields.is_name_character_key(
              "ENTER NAME＋CHARACTER CODE(enter names and codes)"
          ) and
          not custom_fields.is_name_character_key("Customer Name + Character Code"),
          "姓名角色专用字段按固定前缀识别并兼容全半角符号")
    plain_fields = custom_fields.split_custom_info_fields(
        f"{pair_key}: {plain_pair_value}"
    )
    check([(field.label, field.values) for field in plain_fields] == [
        ("Name1", [
            "Rosalind", "Henry", "Nora", "Stella", "Al",
            "LeRoy", "Jodi", "Josie", "Anya",
        ]),
        ("Character1", ["13", "10", "6", "13", "12", "11", "5", "16", "5"]),
    ], "无井号姓名角色值保持配对和原始顺序")
    hash_fields = custom_fields.split_custom_info_fields(
        f"{pair_key}: {hash_pair_value}"
    )
    check([(field.label, field.values) for field in hash_fields] == [
        ("Name1", [
            "Brett", "Emma", "Bailey", "Abby", "Boyd", "Blake",
            "Clara", "Reed", "MacKay", "Ivy", "Wade", "Simon",
        ]),
        ("Character1", ["7", "1", "4", "2", "12", "10", "5", "11", "7", "3", "9", "8"]),
    ], "带井号姓名角色值去除井号并保持原始顺序")
    check(custom_fields.parse_name_character_pairs(
        "Mary Jane #02, Anne-Marie 1; O'Neil 3."
    ) == [
        ("Mary Jane", "2"), ("Anne-Marie", "1"), ("O'Neil", "3"),
    ], "姓名角色解析支持多词、连字符、撇号和前导零编号")
    invalid_pairs = custom_fields.parse_custom_info(
        f"{pair_key}: Brett #7, missing code"
    ).pairs
    check(custom_fields.expand_name_character_pairs(invalid_pairs) == invalid_pairs,
          "姓名角色内容无法完整解析时整组回退原字段")
    check(custom_fields.merge_path_replacements(
        [{"path": ["正面", "姓名"], "value": "独立列"},
         {"path": ["正面", "日期"], "value": "2026"}],
        [{"path": ["正面", "姓名"], "value": "定制信息"}],
    ) == [
        {"path": ["正面", "姓名"], "value": "定制信息"},
        {"path": ["正面", "日期"], "value": "2026"},
    ], "定制信息路径替换覆盖独立列且保持 PSD 顺序")

    check(custom_fields.levenshtein_distance("kitten", "sitting") == 3,
          "Levenshtein 编辑距离正确")
    check(custom_fields.key_similarity("  KID\u00a0 NAME ", "kid name") == 1.0,
          "key 匹配忽略大小写和 Unicode 空白")
    check(abs(custom_fields.key_similarity("abcde", "xbcde") - 0.8) < 1e-12,
          "80% 相似度边界正确")
    targets = [
        custom_fields.TextTarget.from_parts(["正面", "姓名一"], "Kid Name"),
        custom_fields.TextTarget.from_parts(["背面", "姓名二"], "Kids Name"),
    ]
    resolution = custom_fields.resolve_custom_info(
        "kid name: Alice\nkids name: Bob", targets,
    )
    check(not resolution.has_errors and resolution.replacements == [
        {"path": ["正面", "姓名一"], "value": "Alice"},
        {"path": ["背面", "姓名二"], "value": "Bob"},
    ], "相似/重复 key 合并后按 PSD 顺序逐图层赋值")
    check(len(resolution.matches) == 1 and resolution.matches[0].actual_count == 2,
          "相似 PSD key 自动归为一个字段")

    same_key_targets = [
        custom_fields.TextTarget.from_parts(["正面", "姓名"], "Name"),
        custom_fields.TextTarget.from_parts(["背面", "姓名"], "Name"),
    ]
    resolution = custom_fields.resolve_custom_info("Name: Alice, Bob", same_key_targets)
    check(resolution.replacements == [
        {"path": ["正面", "姓名"], "value": "Alice"},
        {"path": ["背面", "姓名"], "value": "Bob"},
    ], "完全相同的 PSD key 视为一个字段并拆分单个 value")

    pair_targets = [
        custom_fields.TextTarget.from_parts(["Name1"], "Name1"),
        custom_fields.TextTarget.from_parts(["Character1"], "Character1"),
        custom_fields.TextTarget.from_parts(["Name2"], "Name2"),
        custom_fields.TextTarget.from_parts(["Character2"], "Character2"),
    ]
    resolution = custom_fields.resolve_custom_info(
        f"{pair_key}: Brett #7, Emma #1", pair_targets,
    )
    check(not resolution.has_errors and not resolution.unmatched_keys and {
        tuple(item["path"]): item["value"] for item in resolution.replacements
    } == {
        ("Name1",): "Brett", ("Character1",): "7",
        ("Name2",): "Emma", ("Character2",): "1",
    }, "姓名角色专用字段展开后参与现有 PSD 字段预检匹配")

    bridged_targets = [
        custom_fields.TextTarget.from_parts(["A"], "abcde"),
        custom_fields.TextTarget.from_parts(["B"], "abcde"),
    ]
    resolution = custom_fields.resolve_custom_info(
        "xbcde: first\nxycde: second", bridged_targets,
    )
    check(resolution.replacements == [
        {"path": ["A"], "value": "first"},
        {"path": ["B"], "value": "second"},
    ], "相似定制 key 先归组后以整组最佳相似度匹配 PSD 字段")

    resolution = custom_fields.resolve_custom_info("kid name: Alice", targets)
    check(resolution.has_errors and
          resolution.count_mismatches[0].expected_count == 2 and
          resolution.count_mismatches[0].actual_count == 1,
          "拆分值数量与图层数量不一致时返回阻断错误")

    ambiguous_targets = [
        custom_fields.TextTarget.from_parts(["A"], "xbcde"),
        custom_fields.TextTarget.from_parts(["B"], "aycde"),
    ]
    resolution = custom_fields.resolve_custom_info("abcde: value", ambiguous_targets)
    check(resolution.has_errors and
          resolution.ambiguous_matches[0].candidates == ["xbcde", "aycde"],
          "相同最高相似度命中多个字段时返回歧义错误")

    resolution = custom_fields.resolve_custom_info(
        "Unknown: x", [custom_fields.TextTarget.from_parts(["Name"], "Name")],
    )
    check(resolution.unmatched_keys == ["Unknown"] and not resolution.has_errors,
          "未匹配 key 仅作为警告信息")
    resolution = custom_fields.resolve_custom_info(
        "abcde: x", [custom_fields.TextTarget.from_parts(["Field"], "xxcde")],
    )
    check(resolution.unmatched_keys == ["abcde"], "低于 80% 相似度不自动归组")


# ---------- image assets ----------

def test_image_assets():
    print("[image_assets]")
    root = tempfile.mkdtemp(prefix="image-assets-test-")
    try:
        os.makedirs(os.path.join(root, "子目录"), exist_ok=True)
        for rel in ["图案A.png", "子目录/图案B.JPG", "忽略.txt", ".隐藏.png"]:
            path = os.path.join(root, rel)
            with open(path, "wb") as f:
                f.write(b"x")
        files = image_assets.scan_image_files(root)
        check(files == ["图案A.png", "子目录/图案B.JPG"], "递归扫描受支持图片并返回相对路径")
        check(
            image_assets.resolve_image_path(root, "子目录/图案B.JPG").endswith("图案B.JPG"),
            "安全解析素材相对路径",
        )
        for invalid in ["../逃逸.png", "/tmp/绝对.png", "忽略.txt"]:
            try:
                image_assets.resolve_image_path(root, invalid)
                raise AssertionError("无效素材路径应报错")
            except ValueError:
                pass
        check(True, "拒绝路径越界、绝对路径和不支持格式")

        config = {
            "image_dir": root,
            "mappings": [
                {"value": "款式A", "file": "图案A.png"},
                {"value": "款式B", "file": "子目录/图案B.JPG"},
            ],
        }
        check(
            image_assets.resolve_mapped_image(config, " 款式A ").endswith("图案A.png"),
            "按去除首尾空格后的 Excel 值精确映射素材",
        )
        try:
            image_assets.resolve_mapped_image(config, "未知款式")
            raise AssertionError("未配置值应报错")
        except ValueError as exc:
            check("没有配置图片映射" in str(exc), "未配置图片值返回明确错误")
        duplicate_config = {
            "image_dir": root,
            "mappings": [
                {"value": "A", "file": "图案A.png"},
                {"value": "A", "file": "子目录/图案B.JPG"},
            ],
        }
        _, duplicates = image_assets.mapping_index(duplicate_config)
        check(duplicates == ["A"], "识别重复 Excel 图片映射值")

        source_layers = [
            {"path_parts": ["图片1"], "kind": "pixel"},
            {"path_parts": ["图片2"], "kind": "smartobject",
             "smart_object": {"file_type": "png", "filename": "图片2.png"}},
            {"path_parts": ["图片3"], "kind": "pixel"},
            {"path_parts": ["图片4"], "kind": "pixel"},
            {"path_parts": ["图片6"], "kind": "pixel"},
        ]
        target_layers = [
            {"path_parts": ["图片1"], "kind": "smartobject",
             "smart_object": {"file_type": "jpeg", "filename": "图片1.jpg"}},
            {"path_parts": ["图片2"], "kind": "pixel"},
            {"path_parts": ["图片4"], "kind": "type"},
            {"path_parts": ["图片6"], "kind": "pixel"},
            {"path_parts": ["目标独有"], "kind": "pixel"},
        ]

        def sync_config(name, column, mappings=None, enabled=True):
            return {
                "layer_path": [name], "enabled": enabled, "column": column,
                "image_dir": root, "mappings": mappings or [],
            }

        source_configs = [
            sync_config("图片1", "图案1", [{"value": "A", "file": "图案A.png"}]),
            sync_config("图片2", "图案2", enabled=False),
            sync_config("图片3", "图案3"),
            sync_config("图片4", "图案4"),
            sync_config("图片5", "图案5"),
            sync_config("图片6", "图案6", [
                {"value": "A", "file": "图案A.png"},
                {"value": "B", "file": "子目录/图案B.JPG"},
            ]),
        ]
        target_configs = [
            sync_config("图片1", "旧图案"),
            sync_config("图片6", "图案6", [
                {"value": "B", "file": "子目录/图案B.JPG"},
                {"value": "A", "file": "图案A.png"},
            ]),
            sync_config("目标独有", "独有列"),
        ]
        before = json.dumps(
            [source_configs, source_layers, target_layers, target_configs],
            ensure_ascii=False, sort_keys=True,
        )
        plan = image_assets.plan_image_config_sync(
            source_configs, source_layers, target_layers, target_configs,
        )
        check(plan["counts"] == {
            "add": 1, "overwrite": 1, "unchanged": 1, "ignored": 3,
        }, "图片配置同步预览区分新增、覆盖、无需变更和忽略")
        statuses = {tuple(action["layer_path"]): action for action in plan["actions"]}
        check(statuses[("图片2",)]["status"] == "add" and
              statuses[("图片2",)]["config"]["enabled"] is False,
              "普通像素层和图片智能对象可跨类型同步并保留停用状态")
        check("缺少" in statuses[("图片3",)]["reason"] and
              "不支持" in statuses[("图片4",)]["reason"] and
              "源模板" in statuses[("图片5",)]["reason"],
              "缺失、类型不支持和源配置失效均给出忽略原因")
        check(json.dumps(
            [source_configs, source_layers, target_layers, target_configs],
            ensure_ascii=False, sort_keys=True,
        ) == before, "生成同步预览不会修改源配置或目标配置")

        sync_settings = {"image_replacement_configs": {}}
        for existing in target_configs:
            settings.set_image_replacement_config(
                sync_settings, "/tpl", "SKU", "目标.psd",
                existing["layer_path"], existing,
            )
        for action in plan["actions"]:
            if action["status"] in {"add", "overwrite"}:
                settings.set_image_replacement_config(
                    sync_settings, "/tpl", "SKU", "目标.psd",
                    action["layer_path"], action["config"],
                )
        applied = settings.get_image_replacement_configs(
            sync_settings, "/tpl", "SKU", "目标.psd",
        )
        applied_by_path = {tuple(item["layer_path"]): item for item in applied}
        check(applied_by_path[("图片1",)]["column"] == "图案1" and
              applied_by_path[("图片2",)]["column"] == "图案2" and
              applied_by_path[("目标独有",)]["column"] == "独有列",
              "确认同步覆盖同路径、写入新增配置并保留目标独有配置")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------- template config sync ----------

def test_template_sync():
    print("[template_sync]")
    source_layers = [
        {"path_parts": ["名字"], "kind": "type"},
        {"path_parts": ["款式A"], "kind": "group"},
        {"path_parts": ["款式A", "姓名"], "kind": "type"},
        {"path_parts": ["款式B"], "kind": "group"},
        {"path_parts": ["款式B", "姓名"], "kind": "type"},
        {
            "path_parts": ["对象"], "kind": "smartobject",
            "smart_object": {"layers": [
                {"path_parts": ["标题"], "kind": "type"},
                {"path_parts": ["内款A"], "kind": "group"},
                {"path_parts": ["内款A", "内名"], "kind": "type"},
                {"path_parts": ["内款B"], "kind": "group"},
                {"path_parts": ["内款B", "内名"], "kind": "type"},
            ]},
        },
    ]
    group_config = {"selectors": [{
        "parent": [], "relative_text_aliases": {"姓名": "组姓名列"},
    }]}
    smart_aliases = [{
        "smart_path": ["对象"], "text_path": ["标题"], "column": "标题列",
    }]
    smart_configs = [{
        "smart_path": ["对象"],
        "selectors": [{
            "parent": [], "relative_text_aliases": {"内名": "内部姓名列"},
        }],
    }]
    entries = template_sync.collect_text_sync_entries(
        source_layers, {"名字": "客户姓名"}, group_config,
        smart_aliases, smart_configs,
    )
    check(len(entries) == 4 and {entry["scope"] for entry in entries} == {
        "root", "group", "smart", "smart_group",
    }, "收集普通、候选组和智能对象内部文字映射")

    target_layers = [
        {"path_parts": ["名字"], "kind": "type"},
        {"path_parts": ["款式A"], "kind": "group"},
        {"path_parts": ["款式A", "姓名"], "kind": "type"},
        {
            "path_parts": ["对象"], "kind": "smartobject",
            "smart_object": {"layers": [
                {"path_parts": ["标题"], "kind": "type"},
                {"path_parts": ["内款A"], "kind": "group"},
                {"path_parts": ["内款A", "内名"], "kind": "type"},
            ]},
        },
    ]
    target_group = {"selectors": [{
        "parent": [], "relative_text_aliases": {"姓名": "旧姓名列"},
    }]}
    target_smart_configs = [{
        "smart_path": ["对象"],
        "selectors": [{
            "parent": [], "relative_text_aliases": {"内名": "内部姓名列"},
        }],
    }]
    plan = template_sync.plan_text_config_sync(
        entries, target_layers, {"名字": "客户姓名"}, target_group,
        [], target_smart_configs,
    )
    check(plan["counts"] == {
        "add": 1, "overwrite": 1, "unchanged": 2, "ignored": 0,
    }, "文字同步预览区分自动共享、新增、覆盖和相同配置")
    by_scope = {action["entry"]["scope"]: action for action in plan["actions"]}
    check("自动共享" in by_scope["root"]["reason"] and
          by_scope["smart"]["status"] == "add" and
          by_scope["group"]["status"] == "overwrite",
          "普通文字说明共享，模板独立文字配置按路径同步")

    different_level_plan = template_sync.plan_text_config_sync(
        [entry for entry in entries if entry["scope"] == "group"],
        [{"path_parts": ["姓名"], "kind": "type"}],
        {}, {"selectors": []}, [], [],
    )
    check(different_level_plan["counts"]["unchanged"] == 0 and
          different_level_plan["counts"]["ignored"] == 1,
          "层级不同且 Excel 列名不同时不误判为已共享")
    same_name_entry = dict(
        next(entry for entry in entries if entry["scope"] == "group")
    )
    same_name_entry["column"] = "姓名"
    different_level_plan = template_sync.plan_text_config_sync(
        [same_name_entry], [{"path_parts": ["姓名"], "kind": "type"}],
        {}, {"selectors": []}, [], [],
    )
    check(different_level_plan["counts"]["unchanged"] == 1 and
          "层级不同" in different_level_plan["actions"][0]["reason"],
          "文字层级不同但同名默认映射已生效时显示无需变更")

    missing_plan = template_sync.plan_text_config_sync(
        entries, [], {"名字": "客户姓名"}, {"selectors": []}, [], [],
    )
    check(missing_plan["counts"]["ignored"] == 4 and all(
        "缺少" in action["reason"] for action in missing_plan["actions"]
    ), "目标模板缺少对应文字图层时全部忽略并说明原因")


# ---------- settings ----------

def test_settings():
    print("[settings]")
    root = tempfile.mkdtemp(prefix="cfg-test-")
    orig_dir, orig_path = settings.CONFIG_DIR, settings.CONFIG_PATH
    settings.CONFIG_DIR = root
    settings.CONFIG_PATH = os.path.join(root, "config.json")
    try:
        cfg = settings.load()
        check(cfg["template_dir"] == "", "无配置文件时返回默认值")
        check(cfg["output_name_column"] == "", "图片命名列默认留空并沿用原规则")
        check(cfg["smart_object_aliases"] == {}, "智能对象文字映射缺省为空")
        check(cfg["smart_object_configs"] == {}, "智能对象显隐配置缺省为空")
        check(cfg["image_replacement_configs"] == {}, "图片替换配置缺省为空")
        cfg["template_dir"] = "/tmp/tpl"
        cfg["output_name_column"] = "订单名称"
        settings.set_variant_field(cfg, "/tmp/tpl", "51052004", "颜色")
        settings.save(cfg)
        cfg2 = settings.load()
        check(cfg2["template_dir"] == "/tmp/tpl", "配置读写回填")
        check(cfg2["output_name_column"] == "订单名称", "图片命名 Excel 列持久化")
        check(settings.get_variant_field(cfg2, "/tmp/tpl", "51052004") == "颜色", "变体字段按 模版目录+SKU 记忆")
        check(settings.get_variant_field(cfg2, "/tmp/tpl", "51052005") == "", "其他 SKU 无记忆")
        check(settings.get_variant_field(cfg2, "/other", "51052004") == "", "其他模版目录无记忆")
        settings.set_variant_field(cfg2, "/tmp/tpl", "51052004", "")
        check(settings.get_variant_field(cfg2, "/tmp/tpl", "51052004") == "", "置空即删除该 SKU 配置")

        settings.set_layer_alias(cfg2, "/tmp/tpl", "51052004", "JASPER", "姓名")
        settings.save(cfg2)
        cfg3 = settings.load()
        check(settings.get_layer_aliases(cfg3, "/tmp/tpl", "51052004") == {"JASPER": "姓名"},
              "图层字段映射持久化")
        settings.set_layer_alias(cfg3, "/tmp/tpl", "51052004", "JASPER", "")
        check(settings.get_layer_aliases(cfg3, "/tmp/tpl", "51052004") == {}, "映射置空即删除")

        settings.set_smart_object_alias(
            cfg3, "/tmp/tpl", "51052004", "模版.psb",
            ["正面", "照片框"], ["标题组", "姓名"], "客户姓名",
        )
        settings.save(cfg3)
        cfg3 = settings.load()
        check(settings.get_smart_object_alias(
            cfg3, "/tmp/tpl", "51052004", "模版.psb",
            ["正面", "照片框"], ["标题组", "姓名"],
        ) == "客户姓名", "智能对象内部文字映射按完整路径持久化")
        check(settings.get_smart_object_aliases(
            cfg3, "/tmp/tpl", "51052004", "模版.psb",
        ) == [{
            "smart_path": ["正面", "照片框"],
            "text_path": ["标题组", "姓名"], "column": "客户姓名",
        }], "批量读取模板的智能对象文字映射用于配置同步")
        settings.set_smart_object_alias(
            cfg3, "/tmp/tpl", "51052004", "模版.psb",
            ["正面", "照片框"], ["标题组", "姓名"], "",
        )
        check(settings.get_smart_object_alias(
            cfg3, "/tmp/tpl", "51052004", "模版.psb",
            ["正面", "照片框"], ["标题组", "姓名"],
        ) == "", "智能对象内部文字映射置空即删除")

        settings.set_smart_object_config(
            cfg3, "/tmp/tpl", "51052004", "模版.psb", ["正面", "照片框"], {
                "selectors": [{
                    "parent": ["款式"],
                    "enabled": True,
                    "column": "内部款式",
                    "candidates": [
                        {"path": ["款式", "A"], "value": "A"},
                        {"path": ["款式", "B"], "value": "B"},
                    ],
                    "relative_text_aliases": {"姓名": "客户姓名"},
                }],
            },
        )
        settings.save(cfg3)
        cfg3 = settings.load()
        smart_cfg = settings.get_smart_object_config(
            cfg3, "/tmp/tpl", "51052004", "模版.psb", ["正面", "照片框"],
        )
        check(
            smart_cfg["selectors"][0]["column"] == "内部款式" and
            smart_cfg["selectors"][0]["candidates"][1]["path"] == ["款式", "B"] and
            smart_cfg["selectors"][0]["relative_text_aliases"] == {"姓名": "客户姓名"},
            "智能对象内部显隐选择器按智能对象完整路径持久化",
        )
        check(settings.get_smart_object_configs(
            cfg3, "/tmp/tpl", "51052004", "模版.psb",
        )[0]["smart_path"] == ["正面", "照片框"],
              "批量读取模板的智能对象配置用于文字映射同步")
        settings.set_smart_object_config(
            cfg3, "/tmp/tpl", "51052004", "模版.psb", ["正面", "照片框"],
            {"selectors": []},
        )
        check(settings.get_smart_object_config(
            cfg3, "/tmp/tpl", "51052004", "模版.psb", ["正面", "照片框"],
        ) == {"selectors": []}, "智能对象内部显隐配置置空即删除")

        settings.set_group_config(cfg3, "/tmp/tpl", "51052004", "模版.psb", {
            "selectors": [{
                "parent": ["背景"],
                "enabled": True,
                "column": "background",
                "candidates": [
                    {"path": ["背景", "图层1"], "value": "1"},
                    {"path": ["背景", "图层2"], "value": "2"},
                ],
                "relative_text_aliases": {"姓名": "name", "年龄": "age"},
            }],
        })
        settings.save(cfg3)
        cfg4 = settings.load()
        group_cfg = settings.get_group_config(cfg4, "/tmp/tpl", "51052004", "模版.psb")
        selector = group_cfg["selectors"][0]
        check(selector["parent"] == ["背景"] and selector["enabled"] and
              selector["column"] == "background" and
              selector["candidates"][0]["path"] == ["背景", "图层1"] and
              selector["relative_text_aliases"] == {"姓名": "name", "年龄": "age"},
              "父组选择器配置持久化")
        settings.set_group_config(cfg4, "/tmp/tpl", "51052004", "模版.psb",
                                  {"selectors": []})
        check(settings.get_group_config(cfg4, "/tmp/tpl", "51052004", "模版.psb") ==
              {"selectors": []}, "父组选择器配置置空即删除")

        # 清除映射: 当前 SKU 的文字映射、图层切换和模版字段均删除, 其他配置保留
        settings.set_variant_field(cfg4, "/tmp/tpl", "51052004", "颜色")
        settings.set_layer_alias(cfg4, "/tmp/tpl", "51052004", "JASPER", "姓名")
        settings.set_smart_object_alias(
            cfg4, "/tmp/tpl", "51052004", "模版.psb",
            ["智能对象"], ["姓名"], "客户姓名",
        )
        settings.set_group_config(cfg4, "/tmp/tpl", "51052004", "模版.psb", {
            "selectors": [{"parent": [], "enabled": True, "column": "颜色", "candidates": []}],
        })
        settings.set_smart_object_config(
            cfg4, "/tmp/tpl", "51052004", "模版.psb", ["智能对象"], {
                "selectors": [{
                    "parent": [], "enabled": True, "column": "内部款式",
                    "candidates": [{"path": ["A"], "value": "A"}],
                }],
            },
        )
        settings.set_image_replacement_config(
            cfg4, "/tmp/tpl", "51052004", "模版.psb", ["图案", "占位层"], {
                "enabled": True,
                "column": "定制图案",
                "image_dir": "/tmp/images",
                "mappings": [
                    {"value": "款式A", "file": "a.png"},
                    {"value": "款式B", "file": "子目录\\b.jpg"},
                ],
            },
        )
        image_cfg = settings.get_image_replacement_config(
            cfg4, "/tmp/tpl", "51052004", "模版.psb", ["图案", "占位层"],
        )
        check(
            image_cfg["enabled"] and image_cfg["column"] == "定制图案" and
            image_cfg["mappings"][1]["file"] == "子目录/b.jpg",
            "图片替换按模版文件和完整图层路径持久化",
        )
        settings.save(cfg4)
        cfg4 = settings.load()
        check(
            settings.get_image_replacement_config(
                cfg4, "/tmp/tpl", "51052004", "模版.psb", ["图案", "占位层"],
            )["mappings"][0] == {"value": "款式A", "file": "a.png"},
            "图片替换配置写入 config.json 后可回填",
        )
        settings.set_variant_field(cfg4, "/tmp/tpl", "51052005", "尺寸")
        settings.set_layer_alias(cfg4, "/tmp/tpl", "51052005", "OTHER", "文案")
        settings.set_group_config(cfg4, "/tmp/tpl", "51052005", "其他.psd", {
            "selectors": [{"parent": [], "enabled": True, "column": "尺寸", "candidates": []}],
        })
        settings.set_variant_field(cfg4, "/other", "51052004", "款式")
        settings.set_layer_alias(cfg4, "/other", "51052004", "ALT", "名称")
        settings.set_group_config(cfg4, "/other", "51052004", "其他.psd", {
            "selectors": [{"parent": [], "enabled": True, "column": "款式", "candidates": []}],
        })
        settings.clear_sku_config(cfg4, "/tmp/tpl", "51052004")
        settings.save(cfg4)
        cfg5 = settings.load()
        check(settings.get_variant_field(cfg5, "/tmp/tpl", "51052004") == "" and
              settings.get_layer_aliases(cfg5, "/tmp/tpl", "51052004") == {} and
              settings.get_group_config(cfg5, "/tmp/tpl", "51052004", "模版.psb") == {"selectors": []} and
              settings.get_smart_object_alias(
                  cfg5, "/tmp/tpl", "51052004", "模版.psb",
                  ["智能对象"], ["姓名"],
              ) == "" and
              settings.get_smart_object_config(
                  cfg5, "/tmp/tpl", "51052004", "模版.psb", ["智能对象"],
              ) == {"selectors": []} and
              settings.get_image_replacement_configs(
                  cfg5, "/tmp/tpl", "51052004", "模版.psb",
              ) == [],
              "清除映射会删除当前 SKU 的全部配置")
        check(settings.get_variant_field(cfg5, "/tmp/tpl", "51052005") == "尺寸" and
              settings.get_layer_aliases(cfg5, "/tmp/tpl", "51052005") == {"OTHER": "文案"} and
              settings.get_group_config(cfg5, "/tmp/tpl", "51052005", "其他.psd")["selectors"],
              "清除映射不影响同目录其他 SKU")
        check(settings.get_variant_field(cfg5, "/other", "51052004") == "款式" and
              settings.get_layer_aliases(cfg5, "/other", "51052004") == {"ALT": "名称"} and
              settings.get_group_config(cfg5, "/other", "51052004", "其他.psd")["selectors"],
              "清除映射不影响其他模版目录")
    finally:
        settings.CONFIG_DIR, settings.CONFIG_PATH = orig_dir, orig_path
        shutil.rmtree(root, ignore_errors=True)


# ---------- renderer (mock) ----------

def _extract_injected(jsx_path, var_name):
    with open(jsx_path, encoding="utf-8") as f:
        src = f.read()
    marker = 'var %s = "' % var_name
    start = src.find(marker) + len(marker)
    return src[start: src.find('"', start)]


def test_renderer():
    print("[renderer]")
    root = tempfile.mkdtemp(prefix="render-test-")
    orig_execute, orig_kill = renderer._execute, renderer.kill_photoshop
    kills = []
    renderer.kill_photoshop = lambda: kills.append(1)
    try:
        psd = os.path.join(root, "模版.psd")
        with open(psd, "wb") as f:
            f.write(b"x")
        out_png = os.path.join(root, "输出", "aB3kF9x2-1.png")
        seen_params = {}

        def ok_execute(jsx_path, timeout):
            params_path = _extract_injected(jsx_path, "PARAMS_PATH")
            result_path = _extract_injected(jsx_path, "RESULT_PATH")
            with open(params_path, encoding="utf-8") as f:
                seen_params.update(json.load(f))
            with open(seen_params["outPngPath"], "wb") as f:
                f.write(b"PNG")
            with open(result_path, "w", encoding="utf-8") as f:
                f.write('{"ok": true, "replaced": ["姓名"]}')

        renderer._execute = ok_execute
        result = renderer.render(
            psd, {"姓名": "张三\n李四"}, out_png,
            visibility_actions=[{"path": ["男"], "visible": True}],
            path_replacements=[{"path": ["男", "姓名"], "value": "张三"}],
            smart_object_text_replacements=[{
                "smartObjectId": "object-1",
                "smartObjectPath": ["正面", "智能对象"],
                "visibilityActions": [{"path": ["款式", "A"], "visible": True}],
                "pathReplacements": [{"path": ["标题组", "姓名"], "value": "张三"}],
            }],
            image_replacements=[{
                "path": ["图案", "图片占位层"],
                "sourcePath": "/tmp/images/a.png",
                "excelValue": "款式A",
            }],
        )
        check(os.path.exists(out_png), "渲染产物已改名落盘(含中文输出目录)")
        check(result["replaced"] == ["姓名"], "渲染结果回传")
        check(seen_params["replacements"] == {"姓名": "张三\n李四"}, "replacements 经 JSON 注入")
        check(seen_params["visibilityActions"] == [{"path": ["男"], "visible": True}],
              "visibilityActions 经 JSON 注入")
        check(seen_params["pathReplacements"] == [{"path": ["男", "姓名"], "value": "张三"}],
              "pathReplacements 经 JSON 注入")
        check(seen_params["smartObjectTextReplacements"][0]["smartObjectPath"] ==
              ["正面", "智能对象"], "smartObjectTextReplacements 经 JSON 注入")
        check(seen_params["smartObjectTextReplacements"][0]["visibilityActions"] ==
              [{"path": ["款式", "A"], "visible": True}],
              "智能对象内部 visibilityActions 经 JSON 注入")
        check(seen_params["imageReplacements"] == [{
            "path": ["图案", "图片占位层"],
            # Windows 上 abspath 会补盘符 (C:/tmp/...)
            "sourcePath": renderer._to_jsx_path("/tmp/images/a.png"),
            "excelValue": "款式A",
        }], "imageReplacements 经 JSON 注入")
        check(seen_params["psdPath"].endswith("模版.psd"), "psdPath 注入")

        # 首次超时 -> kill + 重试成功
        attempts = []

        def flaky_execute(jsx_path, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise renderer.RenderTimeout("渲染超过 1s")
            ok_execute(jsx_path, timeout)

        renderer._execute = flaky_execute
        kills.clear()
        out2 = os.path.join(root, "输出", "retry.png")
        renderer.render(psd, {}, out2, timeout=1)
        check(os.path.exists(out2) and len(attempts) == 2, "超时后重试 1 次成功")

        # JSX 返回错误 -> 两次失败后抛 RenderError
        def fail_execute(jsx_path, timeout):
            result_path = _extract_injected(jsx_path, "RESULT_PATH")
            with open(result_path, "w", encoding="utf-8") as f:
                f.write('{"ok": false, "error": "no such layer"}')

        renderer._execute = fail_execute
        kills.clear()
        try:
            renderer.render(psd, {}, os.path.join(root, "输出", "fail.png"))
            raise AssertionError("应抛 RenderError")
        except renderer.RenderError as e:
            check("no such layer" in str(e), "失败原因透传")
            check(len(kills) == 1, "失败后重启 Photoshop 一次")
    finally:
        renderer._execute, renderer.kill_photoshop = orig_execute, orig_kill
        shutil.rmtree(root, ignore_errors=True)


# ---------- aluminum order automation ----------

def test_aluminum_order_automation():
    print("[aluminum order automation]")
    check(order_intake.parse_size("12×08inch(30×20cm)").key == "30x20",
          "订单尺寸优先使用厘米规格")
    check(order_intake.parse_size("12x8 in").key == "31x20",
          "无厘米规格时英寸换算为严格方向尺寸")
    check(order_intake.parse_size("30-20").written_key == "30x20",
          "无单位订单尺寸按写的数字解析")
    for raw in ('50"x60"', "50“x60“", "50″x60″", "50x60", "50×60", "50-60", "50 x 60 inches"):
        size = order_intake.parse_size(raw)
        check(size is not None and size.written_key == "50x60", f"毛毯尺寸写法 {raw} 按数字解析为 50x60")
    check(order_intake.parse_size('60"x50"').written_key == "60x50", "英寸引号写法保留方向")
    check(order_intake.parse_size("12×16inch(30×40cm)").written_key == "30x40",
          "铝板画尺寸列有厘米时仍按厘米数字")
    blanket = templates.Template(sku="SJS-mt", name="SJS-mt（50×60）", field_value="", path="",
                                 filename="SJS-mt（50×60）.psd", size_key="50x60")
    aluminum = templates.Template(sku="HJ-al", name="HJ-al(31×20cm)", field_value="", path="",
                                  filename="HJ-al(31×20cm).psd", size_key="31x20")
    blanket_index = {"SJS-mt": [blanket], "HJ-al": [aluminum]}

    def order_keys(raw):
        return order_intake.NormalizedOrder(None, 1, "", order_intake.parse_size(raw)).size_keys

    check(templates.pick_by_size(blanket_index, "SJS-mt", order_keys('50"x60"')) == [blanket],
          '订单 50"x60" 匹配模板 (50×60)，不做英寸换算')
    check(templates.pick_by_size(blanket_index, "SJS-mt", order_keys('60"x50"')) == [],
          "方向不同不匹配")
    check(templates.pick_by_size(blanket_index, "HJ-al", order_keys("12x8 in")) == [aluminum],
          "按数字匹配不到时仍用英寸换算后的厘米尺寸兜底")
    check(templates.parse_template_filename("HJ-lbh670916301(20-30)有篝火.psd") ==
          ("HJ-lbh670916301", "20x30", "有篝火", ""),
          "平铺模板文件名解析 SKU、严格尺寸和说明")
    check(templates.parse_template_filename("HJ-lbh670908302（30-20）.psd")[1] == "30x20",
          "全角括号模板尺寸解析")
    check(templates.parse_template_filename("HJ-lbh670908303（30×40cm）.psd")[:2] == ("HJ-lbh670908303", "30x40"),
          "模板文件名尺寸带 cm 单位（全角括号）")
    check(templates.parse_template_filename("HJ-lbh670908301(40×60CM).psd")[:2] == ("HJ-lbh670908301", "40x60"),
          "模板文件名尺寸带 cm 单位（半角括号、大写）")

    scan_root = tempfile.mkdtemp(prefix="nested-template-scan-")
    try:
        nested_files = [
            "任意/深度/HJ-NEST(30-20).psd",
            "另一个/深度/HJ-NEST(30-20).psd",
        ]
        for rel in nested_files:
            path = os.path.join(scan_root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(rel.encode())
        calls = []
        original_parse = templates._parse_psd
        original_thumb = templates._make_thumbnail
        templates._parse_psd = lambda path: (calls.append(path) or {
            "text_layers": [], "all_layers": [], "width": 100, "height": 100,
        })
        templates._make_thumbnail = lambda path, thumb: False
        try:
            nested_index, nested_stats = templates.scan_templates(scan_root)
            check(nested_stats["template_count"] == 2 and len(nested_index["HJ-NEST"]) == 2,
                  "模板库递归扫描任意深度 PSD")
            check(len({item.path for item in nested_index["HJ-NEST"]}) == 2,
                  "不同子目录的同名模板保留为独立条目")
        finally:
            templates._parse_psd = original_parse
            templates._make_thumbnail = original_thumb
    finally:
        shutil.rmtree(scan_root, ignore_errors=True)

    root = tempfile.mkdtemp(prefix="aluminum-profile-")
    try:
        layer = {"path_parts": ["组 1", "Year"], "kind": "type", "visible": True}
        template = templates.Template(
            sku="HJ-test", name="HJ-test", field_value="", path="/tmp/test.psd",
            filename="HJ-test(30-20).psd", all_layers=[layer], width=100, height=50,
            size_key="30x20", structure_signature=templates.structure_signature(100, 50, [layer]),
        )
        suggestion = {
            "text_mappings": [{
                "source_key": "Custom Year", "target_path": ["组 1", "Year"],
                "confidence": 0.99, "required": True,
            }],
            "selector_mappings": [], "ignored_fields": ["Confirm"], "unresolved_fields": [],
        }
        normalized, errors = ai_mapping.validate_mapping_suggestion(suggestion, template)
        check(not errors and normalized["text_mappings"][0]["target_path"] == ["组 1", "Year"],
              "AI 文字映射按完整路径校验")
        profile = template_profiles.make_profile(
            sku=template.sku, size_key=template.size_key, template_filename=template.filename,
            structure_signature=template.structure_signature,
            text_mappings=normalized["text_mappings"], ignored_fields=normalized["ignored_fields"],
        )
        profile["template_binding"] = template_profiles.bind_template(template, root)
        approved = template_profiles.approve_profile(profile, template.structure_signature, "gpt-5.5")
        plan = ai_mapping.compile_render_plan(approved, template, {"Custom Year": ["1991"]})
        check(plan["ok"] and plan["pathReplacements"] == [
            {"path": ["组 1", "Year"], "value": "1991"},
        ], "已审核档案使用订单动态年份替换模板默认文案")
        changed = templates.Template(
            sku=template.sku, name=template.name, field_value="", path=template.path,
            filename=template.filename, all_layers=[layer], width=101, height=50,
            size_key="30x20", structure_signature=templates.structure_signature(101, 50, [layer]),
        )
        status, _ = template_profiles.profile_status(approved, changed.structure_signature)
        check(status == "needs_review", "模板结构签名变化后要求重新审核")
        store = template_profiles.load_store(root)
        template_profiles.put_profile(store, approved)
        template_profiles.save_store(root, store)
        reloaded = template_profiles.load_store(root)
        check(template_profiles.get_profile(reloaded, template.sku, template.size_key, template.filename)["status"] == "approved",
              "模板档案保存到模板库旁文件并可复用")
        bound = template_profiles.bind_template(template, root)
        draft = template_profiles.make_profile(
            sku=template.sku, size_key=template.size_key, template_filename=template.filename,
            structure_signature=template.structure_signature, status="draft", binding=bound,
            analysis=template_profiles.make_analysis(state="ready", model="gpt-5.5",
                                                     order_field_samples={"Custom Year": ["1991"]}),
        )
        store = template_profiles.load_store(root)
        template_profiles.put_profile(store, draft)
        check(template_profiles.profile_status(draft, template.structure_signature)[0] == "needs_review",
              "AI 草稿不能直接作为内置模板出图")
        approved_draft = template_profiles.approve_profile(draft, template.structure_signature, "gpt-5.5")
        template_profiles.put_profile(store, approved_draft)
        check(template_profiles.get_profile(store, template.sku, template.size_key, template.filename)["template_binding"]["filename"] == template.filename,
              "审核通过后模板档案绑定对应 PSD")
        fake_psd = os.path.join(root, template.filename)
        Path(fake_psd).write_bytes(b"psd")
        check(template_profiles.delete_profile(store, template.sku, template.size_key, template.filename, template.path) and
              os.path.exists(fake_psd), "删除内置绑定不删除 PSD 源文件")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    check(ai_client.parse_json_content('```json\n{"ok": true}\n```') == {"ok": True},
          "AI 客户端兼容 Markdown JSON 围栏")


def test_runtime_configuration():
    print("[runtime configuration]")
    root = tempfile.mkdtemp(prefix="env-test-")
    env_path = os.path.join(root, ".env")
    original = {key: os.environ.get(key) for key in (
        "PSD_RENDER_AI_BASE_URL", "PSD_RENDER_AI_API_KEY", "PSD_RENDER_AI_MODELS",
    )}
    try:
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write(
                "PSD_RENDER_AI_BASE_URL=https://relay.example/v1\n"
                "PSD_RENDER_AI_API_KEY=test-key-from-file\n"
                "PSD_RENDER_AI_MODELS=model-a,model-b\n"
            )
        for key in original:
            os.environ.pop(key, None)
        check(ai_client.load_project_env(env_path, override=True), ".env 可被应用加载")
        base_url = os.environ.get("PSD_RENDER_AI_BASE_URL", "")
        api_key = os.environ.get("PSD_RENDER_AI_API_KEY", "")
        models = [item.strip() for item in os.environ.get("PSD_RENDER_AI_MODELS", "").split(",") if item.strip()]
        check(base_url == "https://relay.example/v1" and api_key == "test-key-from-file" and
              models == ["model-a", "model-b"], ".env 提供 AI 地址、密钥和模型顺序")
        os.environ["PSD_RENDER_AI_BASE_URL"] = "https://system.example"
        ai_client.load_project_env(env_path, override=False)
        check(os.environ["PSD_RENDER_AI_BASE_URL"] == "https://system.example",
              "显式系统环境变量优先于 .env")
        order_dir = os.path.join(root, "orders", "2026")  # 不写死 /tmp: Windows 上 abspath 会补盘符
        check(app_main.excel_output_directory(os.path.join(order_dir, "order.xlsx")) == order_dir,
              "订单 Excel 默认输出目录为其所在目录")
        preserved_env = os.path.join(root, "saved.env")
        Path(preserved_env).write_text("# 保留注释\\nOTHER=value\\n", encoding="utf-8")
        ai_client.save_environment_config("https://relay.example/v1", "test-key-write", ["model-b", "model-a"], preserved_env)
        saved_text = Path(preserved_env).read_text(encoding="utf-8")
        check("# 保留注释" in saved_text and "OTHER=value" in saved_text and
              "PSD_RENDER_AI_MODELS=model-b,model-a" in saved_text,
              ".env 保存保留其他变量和模型顺序")
        if os.name != "nt":
            check((os.stat(preserved_env).st_mode & 0o777) == 0o600,
                  ".env 保存后收紧为当前用户权限")
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    # 一组失败时记下来继续跑后面的组, 远程(Windows 自检)一次就能看到全部失败
    failed_groups = []
    for group in (test_templates, test_smart_object_parsing, test_smart_object_helpers, test_excel,
                  test_custom_fields, test_image_assets, test_template_sync, test_settings,
                  test_renderer, test_aluminum_order_automation, test_runtime_configuration):
        try:
            group()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            failed_groups.append(group.__name__)
    if failed_groups:
        print(f"\n失败: {', '.join(failed_groups)}（其余 {PASSED} 项断言通过）")
        sys.exit(1)
    print(f"\n全部通过: {PASSED} 项断言")
