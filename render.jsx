// render.jsx — PSD 批量生图渲染核心 (ExtendScript, Windows/macOS Photoshop 共用)
//
// 由 renderer.py 把本文件中的两个占位符替换为实际路径后执行:
//   __PARAMS_JSON_PATH__  参数 JSON: { psdPath, outPngPath, replacements,
//       visibilityActions, pathReplacements, smartObjectTextReplacements, imageReplacements }
//       pathReplacements 条目可选 fitBox (2026-10-13 新增, 可选键, 不带时行为不变);
//       fitBox 内可选 syncGroup (2026-10-14 新增): 同一文档里同组文字按组内最小比例一起缩放
//   __RESULT_JSON_PATH__  结果 JSON: { ok, replaced, smartObjectReplaced, imageReplaced } 或错误
//   另在 RESULT_PATH + ".progress" 逐行追加 "毫秒\t步骤"(2026-10-14), Python 看门狗据此判断卡在哪一步
//
// 流程: 打开 PSD -> 替换普通及智能对象内部文字 -> SaveForWeb 导出 PNG24 -> 关闭不保存

var PARAMS_PATH = "__PARAMS_JSON_PATH__";
var RESULT_PATH = "__RESULT_JSON_PATH__";
var PROGRESS_PATH = RESULT_PATH + ".progress";
var START_TIME = new Date().getTime();

// 记录"开始做某一步"; 写不进去不影响出图
function progress(step) {
    try {
        var f = new File(PROGRESS_PATH);
        f.encoding = "UTF-8";
        f.open("a");
        f.write((new Date().getTime() - START_TIME) + "\t" + step + "\n");
        f.close();
    } catch (e) {}
}

function readUtf8(path) {
    var f = new File(path);
    f.encoding = "UTF-8";
    f.open("r");
    var s = f.read();
    f.close();
    return s;
}

function writeUtf8(path, content) {
    var f = new File(path);
    f.encoding = "UTF-8";
    f.open("w");
    f.write(content);
    f.close();
}

function escapeJson(s) {
    s = String(s);
    var out = "";
    for (var i = 0; i < s.length; i++) {
        var c = s.charAt(i);
        var code = s.charCodeAt(i);
        if (c === '"' || c === "\\") out += "\\" + c;
        else if (c === "\n") out += "\\n";
        else if (c === "\r") out += "\\r";
        else if (c === "\t") out += "\\t";
        else if (code < 32) out += "\\u" + ("000" + code.toString(16)).slice(-4);
        else out += c;
    }
    return out;
}

function stringArrayJson(items) {
    var out = [];
    for (var i = 0; i < items.length; i++) out.push('"' + escapeJson(items[i]) + '"');
    return "[" + out.join(",") + "]";
}

function safeTextFont(layer) {
    try {
        return String(layer.textItem.font || "");
    } catch (e) {
        return "";
    }
}

// 长文字自适应: 点文字替换后超出画布安全区(两侧各留 4%)时按对齐锚点等比缩小。
// 安全区与模版原示例文字范围取并集, 设计师本来就贴边放的文字不会因此被缩小。
var TEXT_SAFE_MARGIN = 0.04;
var textFitted = [];

function textBoundsPx(layer) {
    var b = layer.bounds;
    return { left: unitPixels(b[0]), right: unitPixels(b[2]) };
}

function selectOnlyLayer(layer) {
    var desc = new ActionDescriptor();
    var ref = new ActionReference();
    ref.putIdentifier(charIDToTypeID("Lyr "), layer.id);
    desc.putReference(charIDToTypeID("null"), ref);
    desc.putBoolean(charIDToTypeID("MkVs"), false);
    executeAction(charIDToTypeID("slct"), desc, DialogModes.NO);
}

function fitTextToCanvas(layer, original, pathLabel) {
    try {
        if (layer.textItem.kind !== TextType.POINTTEXT) return;
    } catch (e) {
        return;
    }
    var docWidth = unitPixels(app.activeDocument.width);
    var minLeft = Math.min(docWidth * TEXT_SAFE_MARGIN, original.left);
    var maxRight = Math.max(docWidth * (1 - TEXT_SAFE_MARGIN), original.right);
    var anchor = textAnchor(layer);
    function fits(b) {
        return b.left >= minLeft - 0.5 && b.right <= maxRight + 0.5;
    }
    var start = textBoundsPx(layer);
    var startWidth = start.right - start.left;
    if (!(startWidth > 0) || fits(start)) return;
    // 改字号在带变换的文字上不可靠(设回原值也会变小), 不使用, 只用 layer.resize。
    withOnlyLayer(layer, function () {
        for (var attempt = 0; attempt < 3; attempt++) {
            var current = textBoundsPx(layer);
            if (fits(current)) break;
            var width = current.right - current.left;
            var allowed;
            if (anchor === AnchorPosition.MIDDLELEFT) allowed = maxRight - current.left;
            else if (anchor === AnchorPosition.MIDDLERIGHT) allowed = current.right - minLeft;
            else {
                var center = (current.left + current.right) / 2;
                allowed = 2 * Math.min(center - minLeft, maxRight - center);
            }
            if (!(allowed > 0) || !(width > 0)) break;
            layer.resize(Math.floor(allowed / width * 1000) / 10, Math.floor(allowed / width * 1000) / 10, anchor);
        }
    });
    var end = textBoundsPx(layer);
    var note = pathLabel + ": 文字超出画布安全区, 已缩小至 " +
        Math.round((end.right - end.left) / startWidth * 100) + "%";
    if (!fits(end)) note += "（仍超出, 需人工处理）";
    textFitted.push(note);
}

// 范围框适配: 模版里与文字配套的隐藏矩形(如 "Man's Name文本框范围")由 Python 端识别,
// 以 fitBox {left, top, right, bottom, maxScale} 传入。替换后文字等比缩放到框内:
// 长了缩小, 短了放大到碰到框的宽或高, 但放大不超过 maxScale(相对模版字号)。
// 单行要缩到模版字号的 1/maxScale 以下时, 改试在单词间空格处换成 2~3 行, 取字最大的排法
// (2026-10-14 用户确认; 不拆单词, 字一样大时取行数少的)。
var textBoxFitted = [];
var WRAP_MAX_LINES = 3;

function textRectPx(layer) {
    var b = layer.bounds;
    return { left: unitPixels(b[0]), top: unitPixels(b[1]), right: unitPixels(b[2]), bottom: unitPixels(b[3]) };
}

// layer.resize 是线性的, 但它作用于"当前选中的所有图层"及链接图层: 模版保存时
// 常带着多图层选中状态(实测 activeLayer 赋值也不会取消多选), 会把姓名/年份一起缩放。
// 所以先用 Action Manager 只选中本图层, 并临时解除链接, 缩放后再链回。
function withOnlyLayer(layer, fn) {
    selectOnlyLayer(layer);
    var linked = [];
    try {
        linked = layer.linkedLayers || [];
    } catch (e) {}
    if (linked.length) layer.unlink();
    try {
        fn();
    } finally {
        for (var i = 0; i < linked.length; i++) {
            try {
                layer.link(linked[i]);
            } catch (e) {}
        }
    }
}

function textAnchor(layer) {
    try {
        var justification = layer.textItem.justification;
        if (justification === Justification.LEFT) return AnchorPosition.MIDDLELEFT;
        if (justification === Justification.RIGHT) return AnchorPosition.MIDDLERIGHT;
    } catch (e) {}
    return AnchorPosition.MIDDLECENTER;
}

// 单词分成 lineCount 行的所有断法里, 取最长一行字符数最少的前 2 种, 留给 Photoshop 实测
function wrapCandidates(words, lineCount) {
    var ranked = [];
    function walk(start, linesLeft, lines) {
        if (linesLeft === 1) {
            lines = lines.concat([words.slice(start).join(" ")]);
            var longest = 0;
            for (var i = 0; i < lines.length; i++) longest = Math.max(longest, lines[i].length);
            ranked.push({ lines: lines, longest: longest, order: ranked.length });
            return;
        }
        for (var end = start + 1; end <= words.length - linesLeft + 1; end++) {
            walk(end, linesLeft - 1, lines.concat([words.slice(start, end).join(" ")]));
        }
    }
    walk(0, lineCount, []);
    ranked.sort(function (a, b) {
        return a.longest - b.longest || a.order - b.order;
    });
    var out = [];
    for (var j = 0; j < ranked.length && j < 2; j++) out.push(ranked[j].lines);
    return out;
}

// 改内容可能让 Photoshop 换掉字体: 每次改完先换回原字体再量, 否则按替代字体比较排法
function setTextContents(layer, contents, font) {
    layer.textItem.contents = contents;
    if (font && safeTextFont(layer) !== font) {
        try {
            layer.textItem.font = font;
        } catch (e) {}
    }
}

// 在单词间空格处换行: 逐个实测候选排法, 取能放大的字号比单行(singleScale)大 1% 以上的那种。
// 文字留在选中的排法上, 返回 {lines, rect}; 不换行时恢复原文字并返回 null。
// 实测过的排法都追加到 options({contents, lines, rect, scale}), 姓名同步缩小时可退回行数少的排法。
function wrapTextInBox(layer, scaleOf, singleScale, options) {
    var text = String(layer.textItem.contents);
    if (text.indexOf("\r") >= 0) return null; // 订单自带换行, 按原样
    var parts = text.split(" ");
    var words = [];
    for (var i = 0; i < parts.length; i++) if (parts[i]) words.push(parts[i]);
    if (words.length < 2) return null;
    var font = safeTextFont(layer);
    var best = null;
    var bestScale = singleScale;
    var current = text;
    for (var count = 2; count <= Math.min(WRAP_MAX_LINES, words.length); count++) {
        var candidates = wrapCandidates(words, count);
        for (var c = 0; c < candidates.length; c++) {
            current = candidates[c].join("\r");
            setTextContents(layer, current, font);
            var rect = textRectPx(layer);
            var scale = scaleOf(rect);
            options.push({ contents: current, lines: count, rect: rect, scale: scale });
            if (scale > bestScale * 1.01) {
                bestScale = scale;
                best = { contents: current, lines: count, rect: rect };
            }
        }
    }
    var wanted = best ? best.contents : text;
    if (current !== wanted) setTextContents(layer, wanted, font);
    return best;
}

// 范围框适配分两步: planTextBox 在写入文字后选排法并算出缩放比例(不缩放);
// applyTextBoxPlans 再统一缩放。fitBox.syncGroup 相同(姓名为 "name")的文字在同一文档里
// 按其中最小的比例缩放, 一个名字太长缩小时其他名字跟着缩小, 保持大小一致(2026-10-14 用户需求)。
function planTextBox(layer, box, pathLabel) {
    try {
        if (layer.textItem.kind !== TextType.POINTTEXT) {
            textFitted.push(pathLabel + ": 段落文字无法转为点文字, 未按范围框调整, 需人工检查");
            return null;
        }
    } catch (e) {
        return null;
    }
    var boxWidth = box.right - box.left;
    var boxHeight = box.bottom - box.top;
    var maxScale = Number(box.maxScale) > 0 ? Number(box.maxScale) : 1;
    var start = textRectPx(layer);
    var startWidth = start.right - start.left;
    var startHeight = start.bottom - start.top;
    if (!(startWidth > 0) || !(startHeight > 0) || !(boxWidth > 0) || !(boxHeight > 0)) return null;
    function ratio(r) {
        return Math.min(boxWidth / (r.right - r.left), boxHeight / (r.bottom - r.top));
    }
    function scaleOf(r) {
        return Math.min(ratio(r), maxScale);
    }
    var plan = {
        layer: layer,
        box: box,
        pathLabel: pathLabel,
        group: box.syncGroup ? String(box.syncGroup) : "",
        font: safeTextFont(layer),
        ratio: ratio,
        anchor: textAnchor(layer),
        originCenterY: (start.top + start.bottom) / 2,
        contents: String(layer.textItem.contents),
        lines: 1,
        start: start,
        scale: scaleOf(start),
        options: []
    };
    plan.options.push({ contents: plan.contents, lines: 1, rect: start, scale: plan.scale });
    if (plan.scale < 1 / Math.max(maxScale, 1)) {
        var wrapped = wrapTextInBox(layer, scaleOf, plan.scale, plan.options);
        if (wrapped) {
            plan.contents = wrapped.contents;
            plan.lines = wrapped.lines;
            plan.start = wrapped.rect;
            plan.scale = scaleOf(wrapped.rect);
        }
    }
    return plan;
}

// 同步缩小到 target 时, 换行就不一定需要了: 取放得下 target 的排法里行数最少的
function chooseSyncedLayout(plan, target) {
    var chosen = null;
    for (var i = 0; i < plan.options.length; i++) {
        var option = plan.options[i];
        if (option.scale < target * 0.995) continue;
        if (!chosen || option.lines < chosen.lines || (option.lines === chosen.lines && option.scale > chosen.scale)) {
            chosen = option;
        }
    }
    if (!chosen || chosen.contents === plan.contents) return;
    setTextContents(plan.layer, chosen.contents, plan.font);
    plan.contents = chosen.contents;
    plan.lines = chosen.lines;
    plan.start = textRectPx(plan.layer);
}

function applyTextBox(plan, scale, synced) {
    var layer = plan.layer;
    var box = plan.box;
    var anchor = plan.anchor;
    var startWidth = plan.start.right - plan.start.left;
    withOnlyLayer(layer, function () {
        if (Math.abs(scale - 1) > 0.005) layer.resize(scale * 100, scale * 100, anchor);
        // 文字缩放不完全线性, 超出时再收几次
        for (var attempt = 0; attempt < 3; attempt++) {
            var again = plan.ratio(textRectPx(layer));
            if (again >= 0.995) break;
            layer.resize(Math.floor(again * 1000) / 10, Math.floor(again * 1000) / 10, anchor);
        }
        // 原文字不在框正中时, 缩放后可能偏出一侧: 平移回框内。
        // 换行后多出的行向下排, 先把整段竖直居中到原文字的中线, 再收回框内。
        var r = textRectPx(layer);
        var dx = 0;
        var dy = plan.lines > 1 ? plan.originCenterY - (r.top + r.bottom) / 2 : 0;
        if (r.left < box.left) dx = box.left - r.left;
        else if (r.right > box.right) dx = box.right - r.right;
        if (r.top + dy < box.top) dy = box.top - r.top;
        else if (r.bottom + dy > box.bottom) dy = box.bottom - r.bottom;
        if (dx || dy) layer.translate(new UnitValue(dx, "px"), new UnitValue(dy, "px"));
    });
    var end = textRectPx(layer);
    var percent = Math.round((end.right - end.left) / startWidth * 100);
    var inside = end.left >= box.left - 1 && end.right <= box.right + 1 &&
        end.top >= box.top - 1 && end.bottom <= box.bottom + 1;
    var how = (plan.lines > 1 ? "换为 " + plan.lines + " 行, " : "") + "调整为 " + percent + "%" +
        (synced ? "（与其他姓名同步大小）" : "");
    if (!inside) textFitted.push(plan.pathLabel + ": 文字按范围框" + how + " 后仍超出范围框, 需人工处理");
    else textBoxFitted.push(plan.pathLabel + ": 按范围框" + how);
}

function applyTextBoxPlans(plans) {
    var groupScale = {}; // "k" + syncGroup -> 组内最小比例, 加前缀避开 Object 自带属性
    var groupCount = {};
    var i, key;
    for (i = 0; i < plans.length; i++) {
        if (!plans[i].group) continue;
        key = "k" + plans[i].group;
        groupCount[key] = (groupCount[key] || 0) + 1;
        groupScale[key] = groupScale.hasOwnProperty(key) ? Math.min(groupScale[key], plans[i].scale) : plans[i].scale;
    }
    var originalRulerUnits = app.preferences.rulerUnits;
    app.preferences.rulerUnits = Units.PIXELS;
    try {
        for (i = 0; i < plans.length; i++) {
            var plan = plans[i];
            var target = plan.scale;
            var synced = false;
            key = "k" + plan.group;
            if (plan.group && groupCount[key] > 1 && groupScale[key] < plan.scale * 0.995) {
                target = groupScale[key];
                synced = true;
                chooseSyncedLayout(plan, target);
            }
            applyTextBox(plan, target, synced);
        }
    } finally {
        app.preferences.rulerUnits = originalRulerUnits;
    }
}

// 有 fitBox 时只写入文字并返回适配计划(由调用方 applyTextBoxPlans 缩放), 否则返回 null
function replaceLayerText(layer, value, replaced, fontWarnings, pathLabel, requestedFont, fitBox) {
    var originalRulerUnits = app.preferences.rulerUnits;
    app.preferences.rulerUnits = Units.PIXELS;
    var plan = null;
    try {
        // 段落文字放不下时 Photoshop 自己断词(加连字符)并把多出的行藏掉:
        // 有范围框时先转成点文字, 再写入订单文字, 由 planTextBox 换行、applyTextBoxPlans 缩放。
        if (fitBox) {
            try {
                if (layer.textItem.kind !== TextType.POINTTEXT) layer.textItem.kind = TextType.POINTTEXT;
            } catch (e) {}
        }
        var original = fitBox ? null : textBoundsPx(layer);
        replaceLayerTextContents(layer, value, fontWarnings, pathLabel, requestedFont);
        if (fitBox) plan = planTextBox(layer, fitBox, pathLabel);
        else fitTextToCanvas(layer, original, pathLabel);
    } finally {
        app.preferences.rulerUnits = originalRulerUnits;
    }
    replaced.push(pathLabel);
    return plan;
}

function replaceLayerTextContents(layer, value, fontWarnings, pathLabel, requestedFont) {
    var beforeFont = safeTextFont(layer);
    if (requestedFont) {
        try {
            layer.textItem.font = requestedFont;
        } catch (fontError) {
            fontWarnings.push(pathLabel + ": 无法应用指定字体 " + requestedFont + " (" + (fontError.message ? fontError.message : fontError) + ")");
        }
    }
    // Photoshop 文本图层的换行符是 \r
    layer.textItem.contents = String(value).split("\n").join("\r");
    var afterFont = safeTextFont(layer);
    if (beforeFont && afterFont && beforeFont !== afterFont) {
        try {
            layer.textItem.font = beforeFont;
            afterFont = safeTextFont(layer);
        } catch (e) {
            fontWarnings.push(pathLabel + ": 无法恢复字体 " + beforeFont + " (" + (e.message ? e.message : e) + ")");
        }
    }
    if (beforeFont && afterFont && beforeFont !== afterFont) {
        fontWarnings.push(pathLabel + ": 替换后字体从 " + beforeFont + " 变为 " + afterFont);
    }
}

function replaceTextLayers(container, replacements, replaced, fontWarnings, parentParts, requestedFont) {
    parentParts = parentParts || [];
    for (var i = 0; i < container.layers.length; i++) {
        var layer = container.layers[i];
        var parts = parentParts.concat([layer.name]);
        if (layer.typename === "LayerSet") {
            replaceTextLayers(layer, replacements, replaced, fontWarnings, parts, requestedFont);
        } else if (layer.kind === LayerKind.TEXT && replacements.hasOwnProperty(layer.name)) {
            replaceLayerText(
                layer,
                replacements[layer.name],
                replaced,
                fontWarnings,
                pathToString(parts),
                requestedFont
            );
        }
    }
}

// 按完整路径找图层(同名取第一个)。每读一次 layers / name 都是一次 Photoshop 调用:
// 几百项显隐共用上级路径时, 逐项从根往下找会把同一批名字读几百遍(实测 399 项约 48 秒,
// Windows 更慢, 是 10.14 渲染超时的主因)。所以每个图层组的子图层只读一遍, 按路径缓存;
// 文档结构在出图过程中不变(只改显隐和文字), 缓存一直有效。
function layerFinder(container) {
    var children = {}; // 路径前缀 -> {"k" + 名字: 图层}, 加前缀避开 Object 自带属性
    var isSet = {};    // 路径前缀 -> 是否图层组
    function childrenOf(owner, key) {
        if (children.hasOwnProperty(key)) return children[key];
        var map = {};
        var layers = owner.layers;
        var count = layers.length;
        for (var i = 0; i < count; i++) {
            var layer = layers[i];
            var name = "k" + layer.name;
            if (!map.hasOwnProperty(name)) map[name] = layer;
        }
        children[key] = map;
        return map;
    }
    return function (parts) {
        if (!parts || !parts.length) return null;
        var current = container;
        var key = "";
        var layer = null;
        for (var depth = 0; depth < parts.length; depth++) {
            var map = childrenOf(current, key);
            var name = "k" + parts[depth];
            layer = map.hasOwnProperty(name) ? map[name] : null;
            if (!layer) return null;
            key += "\u0001" + parts[depth];
            if (depth < parts.length - 1) {
                if (!isSet.hasOwnProperty(key)) isSet[key] = layer.typename === "LayerSet";
                if (!isSet[key]) return null;
                current = layer;
            }
        }
        return layer;
    };
}

function findLayerByPath(container, parts) {
    return layerFinder(container)(parts);
}

function pathToString(parts) {
    var out = [];
    for (var i = 0; i < parts.length; i++) out.push(String(parts[i]));
    return out.join("/");
}

function applyVisibilityActions(doc, actions, changed, pathPrefix) {
    actions = actions || [];
    var where = pathPrefix ? "智能对象 " + pathPrefix + "：" : "主文档：";
    if (actions.length) progress(where + "切换显隐（" + actions.length + " 项）");
    var find = layerFinder(doc);
    for (var i = 0; i < actions.length; i++) {
        // 每 50 项报一次进度: 慢但在动就不会被看门狗判卡死
        if (i && i % 50 === 0) progress(where + "切换显隐 " + i + "/" + actions.length);
        var action = actions[i];
        var layer = find(action.path);
        var itemPath = pathToString(action.path || []);
        var pathLabel = pathPrefix ? pathPrefix + " › " + itemPath : itemPath;
        if (!layer) throw new Error("找不到显隐图层: " + pathLabel);
        var wanted = !!action.visible;
        if (layer.visible !== wanted) layer.visible = wanted;
        changed.push(pathLabel + "=" + (!!action.visible ? "显示" : "隐藏"));
    }
}

function replaceTextLayersByPath(doc, items, replaced, fontWarnings, pathPrefix, requestedFont) {
    items = items || [];
    if (items.length) progress((pathPrefix ? "智能对象 " + pathPrefix + "：" : "主文档：") + "替换文字（" + items.length + " 处）");
    var plans = [];
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var itemPath = pathToString(item.path || []);
        var pathLabel = pathPrefix ? pathPrefix + " › " + itemPath : itemPath;
        var layer = findLayerByPath(doc, item.path);
        if (!layer) throw new Error("找不到文字图层: " + pathLabel);
        if (layer.typename === "LayerSet" || layer.kind !== LayerKind.TEXT) {
            throw new Error("目标不是文字图层: " + pathLabel);
        }
        var plan = replaceLayerText(layer, item.value, replaced, fontWarnings, pathLabel, requestedFont, item.fitBox || null);
        if (plan) plans.push(plan);
    }
    // 先写完本文档所有文字再缩放: 同组姓名要按组内最小比例一起缩
    applyTextBoxPlans(plans);
}

function replaceSmartObjectText(doc, tasks, replaced, fontWarnings, visibilityChanged) {
    tasks = tasks || [];
    for (var i = 0; i < tasks.length; i++) {
        var task = tasks[i];
        var smartPath = task.smartObjectPath || [];
        var smartLabel = pathToString(smartPath);
        app.activeDocument = doc;
        var smartLayer = findLayerByPath(doc, smartPath);
        if (!smartLayer) throw new Error("找不到智能对象: " + smartLabel);
        if (smartLayer.typename === "LayerSet" || smartLayer.kind !== LayerKind.SMARTOBJECT) {
            throw new Error("目标不是智能对象: " + smartLabel);
        }

        doc.activeLayer = smartLayer;
        var innerDoc = null;
        try {
            progress("打开智能对象 " + smartLabel);
            app.runMenuItem(stringIDToTypeID("placedLayerEditContents"));
            if (app.activeDocument === doc) {
                throw new Error("智能对象内容未在 Photoshop 中打开: " + smartLabel);
            }
            innerDoc = app.activeDocument;
            applyVisibilityActions(
                innerDoc,
                task.visibilityActions,
                visibilityChanged,
                smartLabel
            );
            replaceTextLayersByPath(
                innerDoc,
                task.pathReplacements,
                replaced,
                fontWarnings,
                smartLabel,
                task.fontName || ""
            );
            progress("保存智能对象 " + smartLabel);
            innerDoc.close(SaveOptions.SAVECHANGES);
            innerDoc = null;
            app.activeDocument = doc;
        } catch (e) {
            if (innerDoc !== null) {
                try {
                    app.activeDocument = innerDoc;
                    innerDoc.close(SaveOptions.DONOTSAVECHANGES);
                } catch (closeError) {}
            }
            app.activeDocument = doc;
            throw e;
        }
    }
}

function unitPixels(value) {
    try {
        return Number(value.as("px"));
    } catch (e) {
        return Number(value);
    }
}

function layerPixelBounds(layer, pathLabel) {
    var bounds = layer.bounds;
    var left = unitPixels(bounds[0]);
    var top = unitPixels(bounds[1]);
    var right = unitPixels(bounds[2]);
    var bottom = unitPixels(bounds[3]);
    if (!(right > left) || !(bottom > top)) {
        throw new Error("像素图层没有有效边界: " + pathLabel);
    }
    return {
        left: left,
        top: top,
        right: right,
        bottom: bottom,
        width: right - left,
        height: bottom - top,
        centerX: (left + right) / 2,
        centerY: (top + bottom) / 2
    };
}

function replaceLayerImages(doc, items, replaced) {
    items = items || [];
    if (items.length) progress("替换图片（" + items.length + " 处）");
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var targetPath = item.path || [];
        var targetLabel = pathToString(targetPath);
        app.activeDocument = doc;
        var targetLayer = findLayerByPath(doc, targetPath);
        if (!targetLayer) throw new Error("找不到图片占位层: " + targetLabel);
        var sourceFile = new File(item.sourcePath);
        if (!sourceFile.exists) throw new Error("图片素材不存在: " + item.sourcePath);

        if (targetLayer.typename !== "LayerSet" && targetLayer.kind === LayerKind.SMARTOBJECT) {
            doc.activeLayer = targetLayer;
            var replaceDescriptor = new ActionDescriptor();
            replaceDescriptor.putPath(charIDToTypeID("null"), sourceFile);
            executeAction(
                stringIDToTypeID("placedLayerReplaceContents"),
                replaceDescriptor,
                DialogModes.NO
            );
            replaced.push(
                targetLabel + " ← " + String(item.excelValue || "") +
                " (" + sourceFile.name + ", 智能对象内容)"
            );
            continue;
        }
        if (targetLayer.typename === "LayerSet" || targetLayer.kind !== LayerKind.NORMAL) {
            throw new Error("图片替换目标不是普通像素层或智能对象: " + targetLabel);
        }

        var targetBounds = layerPixelBounds(targetLayer, targetLabel);
        var targetName = String(targetLayer.name);
        var targetVisible = !!targetLayer.visible;

        var sourceDoc = null;
        try {
            sourceDoc = app.open(sourceFile);
            if (sourceDoc.layers.length > 1) sourceDoc.mergeVisibleLayers();
            var sourceLayer = sourceDoc.activeLayer;
            var sourceBounds = layerPixelBounds(sourceLayer, String(item.sourcePath));
            var importedLayer = sourceLayer.duplicate(doc, ElementPlacement.PLACEATBEGINNING);

            app.activeDocument = doc;
            importedLayer.move(targetLayer, ElementPlacement.PLACEBEFORE);
            var scale = Math.min(
                targetBounds.width / sourceBounds.width,
                targetBounds.height / sourceBounds.height
            ) * 100;
            importedLayer.resize(scale, scale, AnchorPosition.MIDDLECENTER);
            var importedBounds = layerPixelBounds(importedLayer, targetLabel);
            importedLayer.translate(
                new UnitValue(targetBounds.centerX - importedBounds.centerX, "px"),
                new UnitValue(targetBounds.centerY - importedBounds.centerY, "px")
            );
            importedLayer.name = targetName;
            importedLayer.visible = targetVisible;
            targetLayer.remove();
            replaced.push(
                targetLabel + " ← " + String(item.excelValue || "") + " (" + sourceFile.name + ")"
            );
        } finally {
            if (sourceDoc !== null) {
                try {
                    app.activeDocument = sourceDoc;
                    sourceDoc.close(SaveOptions.DONOTSAVECHANGES);
                } catch (closeError) {}
            }
            app.activeDocument = doc;
        }
    }
}

(function () {
    var resultJson;
    try {
        progress("开始执行脚本");
        var params = eval("(" + readUtf8(PARAMS_PATH) + ")");
        app.displayDialogs = DialogModes.NO; // 禁弹窗(缺字体等一律静默用替代方案)
        progress("打开 PSD");
        var doc = app.open(new File(params.psdPath));
        var replaced = [];
        var smartObjectReplaced = [];
        var smartObjectVisibility = [];
        var imageReplaced = [];
        var fontWarnings = [];
        var visibilityChanged = [];
        try {
            applyVisibilityActions(doc, params.visibilityActions, visibilityChanged);
            replaceTextLayers(doc, params.replacements, replaced, fontWarnings, [], params.fontName || "");
            replaceTextLayersByPath(doc, params.pathReplacements, replaced, fontWarnings, "", params.fontName || "");
            replaceSmartObjectText(
                doc,
                params.smartObjectTextReplacements,
                smartObjectReplaced,
                fontWarnings,
                smartObjectVisibility
            );
            replaceLayerImages(doc, params.imageReplacements, imageReplaced);
            var opts = new ExportOptionsSaveForWeb();
            opts.format = SaveDocumentType.PNG;
            opts.PNG8 = false;
            opts.transparency = true;
            opts.interlaced = false;
            progress("导出 PNG");
            doc.exportDocument(new File(params.outPngPath), ExportType.SAVEFORWEB, opts);
        } finally {
            progress("关闭文档");
            doc.close(SaveOptions.DONOTSAVECHANGES);
        }
        resultJson = '{"ok": true, "replaced": ' + stringArrayJson(replaced) +
            ', "smartObjectReplaced": ' + stringArrayJson(smartObjectReplaced) +
            ', "smartObjectVisibility": ' + stringArrayJson(smartObjectVisibility) +
            ', "imageReplaced": ' + stringArrayJson(imageReplaced) +
            ', "visibility": ' + stringArrayJson(visibilityChanged) +
            ', "fontWarnings": ' + stringArrayJson(fontWarnings) +
            ', "textFitted": ' + stringArrayJson(textFitted) +
            ', "textBoxFitted": ' + stringArrayJson(textBoxFitted) + "}";
    } catch (e) {
        resultJson = '{"ok": false, "error": "' + escapeJson(e.message ? e.message : e) + '"}';
    }
    writeUtf8(RESULT_PATH, resultJson);
})();
