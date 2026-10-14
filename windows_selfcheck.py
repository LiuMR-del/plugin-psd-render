"""运行环境自检: 双击 Windows自检.bat (macOS: bash start.sh selfcheck) 运行.

依次检查运行环境与依赖、自带测试、Photoshop 连接、真实出图(中文路径 + 段落文字 +
范围框换行)、字体安装权限, 结果写到本目录 自检结果/ 下, 把整个文件夹发给维护人员即可.

只读检查: 不改模板库、config.json、.env, 不装字体; 会打开 Photoshop 新建/导出一张测试图.
报告不含 API Key 和 .env 内容.
"""

from __future__ import annotations

import importlib.metadata
import json
import locale
import os
import platform
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "自检结果")  # 中文目录: 顺带验证 Photoshop 读写中文路径
REPORT = os.path.join(OUT_DIR, "自检报告.txt")
TEST_LOG = os.path.join(OUT_DIR, "测试输出.txt")
TEST_PSD = os.path.join(OUT_DIR, "测试模板-中文名.psd")
TEST_PNG = os.path.join(OUT_DIR, "测试出图-中文名.png")
IS_WINDOWS = sys.platform == "win32"

# 测试文档 1200×600 px: 左边段落文字(同标准样板), 右边点文字, 各配一个范围框
MAN_BOX = {"left": 60, "top": 200, "right": 540, "bottom": 420, "maxScale": 1.3}
WOMAN_BOX = {"left": 660, "top": 200, "right": 1140, "bottom": 420, "maxScale": 1.3}
MAKE_PSD_JSX = r"""
var OUT = %(out)s;
var RESULT = %(result)s;
function write(path, s) { var f = new File(path); f.encoding = "UTF-8"; f.open("w"); f.write(s); f.close(); }
var rulerUnits = app.preferences.rulerUnits;
var typeUnits = app.preferences.typeUnits;
try {
    app.displayDialogs = DialogModes.NO;
    app.preferences.rulerUnits = Units.PIXELS;
    app.preferences.typeUnits = TypeUnits.PIXELS;
    var doc = app.documents.add(1200, 600, 72, "selfcheck", NewDocumentMode.RGB, DocumentFill.WHITE);
    var man = doc.artLayers.add();
    man.kind = LayerKind.TEXT;
    man.name = "Man's Name";
    man.textItem.kind = TextType.PARAGRAPHTEXT;
    man.textItem.font = "ArialMT";
    man.textItem.size = 110;
    man.textItem.justification = Justification.CENTER;
    man.textItem.position = [100, 240];
    man.textItem.width = 400;
    man.textItem.height = 140;
    man.textItem.contents = "Name";
    var woman = doc.artLayers.add();
    woman.kind = LayerKind.TEXT;
    woman.name = "Woman's Name";
    woman.textItem.font = "ArialMT";
    woman.textItem.size = 110;
    woman.textItem.justification = Justification.CENTER;
    woman.textItem.position = [900, 350];
    woman.textItem.contents = "Name";
    doc.saveAs(new File(OUT), new PhotoshopSaveOptions(), true);
    doc.close(SaveOptions.DONOTSAVECHANGES);
    write(RESULT, '{"ok": true, "version": "' + app.version + '"}');
} catch (e) {
    write(RESULT, '{"ok": false, "error": "' + String(e.message || e).replace(/["\\\r\n]/g, " ") + '"}');
} finally {
    app.preferences.rulerUnits = rulerUnits;
    app.preferences.typeUnits = typeUnits;
}
"""

results: list[tuple[str, str, str]] = []  # (状态, 项目, 详情)


def record(status: str, item: str, detail: str = "") -> None:
    results.append((status, item, detail))
    print(f"[{status}] {item}" + (f"：{detail.splitlines()[0]}" if detail else ""), flush=True)


def check_environment() -> None:
    info = [f"系统：{platform.platform()}（{platform.machine()}）",
            f"Python：{sys.version.split()[0]}  {sys.executable}",
            f"系统编码：{locale.getpreferredencoding(False)}"]
    if IS_WINDOWS:
        import ctypes
        info.append(f"ANSI 代码页：{ctypes.windll.kernel32.GetACP()}，控制台代码页：{ctypes.windll.kernel32.GetConsoleOutputCP()}")
    info.append(f"程序目录：{ROOT}")
    info.append(f"临时目录：{tempfile.gettempdir()}")
    record("信息", "运行环境", "\n".join(info))

    packages = ["flet", "psd-tools", "openpyxl", "python-dotenv"] + (["pywin32"] if IS_WINDOWS else [])
    versions, missing = [], []
    for name in packages:
        try:
            versions.append(f"{name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    if missing:
        record("失败", "依赖包", "缺少：" + "、".join(missing))
    elif not any(v == "flet 0.28.2" for v in versions):
        record("警告", "依赖包", "flet 不是固定的 0.28.2：" + "，".join(versions))
    else:
        record("通过", "依赖包", "，".join(versions))
    if IS_WINDOWS:
        try:
            import win32com.client  # noqa: F401
            record("通过", "pywin32 (win32com)")
        except Exception as exc:  # noqa: BLE001
            record("失败", "pywin32 (win32com)", str(exc))
    record("信息", ".env（AI 配置）", "存在" if os.path.exists(os.path.join(ROOT, ".env")) else "不存在（AI 分析不可用，不影响出图）")


def run_tests() -> None:
    commands = [("自带测试 selftest.py", [sys.executable, "selftest.py"]),
                ("自带测试 unittest", [sys.executable, "-m", "unittest", "discover", "-p", "test_*.py"])]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(TEST_LOG, "w", encoding="utf-8-sig") as log:
        for label, command in commands:
            try:
                proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=600, env=env)
            except subprocess.TimeoutExpired:
                record("失败", label, "超过 10 分钟未结束")
                continue
            output = (proc.stdout or "") + (proc.stderr or "")
            log.write(f"===== {label}（退出码 {proc.returncode}）=====\n{output}\n")
            lines = [line for line in output.strip().splitlines() if line.strip()]
            summary = lines[-1] if lines else ""
            if proc.returncode == 0:
                record("通过", label, summary)
            else:
                failed = list(dict.fromkeys(line for line in lines if line.startswith(("FAIL", "ERROR", "AssertionError"))))[:15]
                record("失败", label, "\n".join([summary] + failed) + "\n完整输出见 测试输出.txt")


def _windows_photoshop_pids() -> list[int]:
    proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Photoshop.exe", "/FO", "CSV", "/NH"],
                          capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    pids = []
    for line in (proc.stdout or "").splitlines():
        fields = [field.strip('"') for field in line.split('","')]
        if len(fields) > 1 and fields[0].lower() == "photoshop.exe" and fields[1].isdigit():
            pids.append(int(fields[1]))
    return pids


def _process_elevated(pid: int) -> str:
    """'是' / '否' / why unknown — COM refuses to connect across elevation levels."""
    import ctypes
    from ctypes import wintypes

    kernel32, advapi32 = ctypes.windll.kernel32, ctypes.windll.advapi32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD))
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return "无法读取"
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return "无法读取（通常说明它以管理员身份运行）"
        try:
            elevated, size = wintypes.DWORD(), wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size)):
                return "无法读取"
            return "是" if elevated.value else "否"
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def windows_photoshop_diagnostics() -> str:
    """Evidence for the three usual COM start failures: elevation mismatch, slow start, stale registration."""
    import ctypes
    import winreg

    lines = [f"本程序以管理员身份运行：{'是' if ctypes.windll.shell32.IsUserAnAdmin() else '否'}"]
    pids = _windows_photoshop_pids()
    lines.append("Photoshop 进程：" + ("，".join(f"PID {pid} 管理员={_process_elevated(pid)}" for pid in pids)
                                     if pids else "未运行"))

    def read(root, path, name=""):
        try:
            with winreg.OpenKey(root, path) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return ""

    clsid = read(winreg.HKEY_CLASSES_ROOT, r"Photoshop.Application\CLSID")
    current = read(winreg.HKEY_CLASSES_ROOT, r"Photoshop.Application\CurVer")
    lines.append(f"COM 登记 Photoshop.Application：{clsid or '没有'}" + (f"（当前版本 {current}）" if current else ""))
    if clsid:
        server = read(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32")
        exe = server.split('"')[1] if server.startswith('"') else server.split(" /")[0].strip()
        lines.append(f"COM 启动的程序：{server or '没有'}（文件{'存在' if exe and os.path.isfile(exe) else '不存在'}）")
    app_path = read(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe")
    lines.append(f"安装位置（App Paths）：{app_path or '没有'}")
    return "\n".join(lines)


def photoshop_version() -> str:
    if IS_WINDOWS:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            # 第一次连接时 Photoshop 可能还在启动(登录/首页), COM 等不及就报"服务器运行失败":
            # Photoshop 进程已经起来时再等它启动完重连两次, 用来区分"启动慢"与其他原因
            for attempt in range(3):
                started = time.monotonic()
                try:
                    version = str(win32com.client.Dispatch("Photoshop.Application").Version)
                    if attempt:
                        version += f"（第 {attempt + 1} 次连接才成功，Photoshop 启动较慢）"
                    return version
                except Exception as exc:  # noqa: BLE001
                    waited = time.monotonic() - started
                    print(f"  第 {attempt + 1} 次连接失败（等了 {waited:.0f} 秒）：{exc}", flush=True)
                    if attempt == 2 or not _windows_photoshop_pids():
                        raise RuntimeError(f"{exc}（第 {attempt + 1} 次连接，等了 {waited:.0f} 秒）") from exc
                    time.sleep(60)
        finally:
            pythoncom.CoUninitialize()
    proc = subprocess.run(["osascript", "-e", 'tell application id "com.adobe.photoshop" to get version'],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript 调用失败")
    return proc.stdout.strip()


def make_test_psd() -> None:
    import renderer

    work = tempfile.mkdtemp(prefix="psd-selfcheck-")
    try:
        result_path = os.path.join(work, "result.json")
        jsx_path = os.path.join(work, "make.jsx")
        source = MAKE_PSD_JSX % {"out": json.dumps(renderer._to_jsx_path(TEST_PSD)),
                                 "result": json.dumps(renderer._to_jsx_path(result_path))}
        with open(jsx_path, "w", encoding="utf-8-sig") as handle:  # 与 renderer 一致: 带 BOM
            handle.write(source)
        renderer._execute(jsx_path, 180)
        with open(result_path, encoding="utf-8") as handle:
            result = json.load(handle)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "未知错误")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def check_photoshop_render() -> None:
    print("即将打开 Photoshop 新建并导出一张测试图（不会动模板和已打开的文档），请稍候…", flush=True)
    version, error = "", "未返回版本号"
    try:
        version = photoshop_version()
    except Exception as exc:  # noqa: BLE001
        error = exc
    diagnostics = ""
    if IS_WINDOWS:
        try:
            diagnostics = "\n" + windows_photoshop_diagnostics()
        except Exception as exc:  # noqa: BLE001
            diagnostics = f"\n诊断信息读取失败：{exc}"
    if not version:
        record("失败", "连接 Photoshop", f"{error}{diagnostics}\n跳过出图测试")
        return
    record("通过", "连接 Photoshop", f"版本 {version}{diagnostics}")
    for path in (TEST_PSD, TEST_PNG):
        if os.path.exists(path):
            os.remove(path)
    try:
        make_test_psd()
        record("通过", "Photoshop 新建测试模板（中文路径）", os.path.basename(TEST_PSD))
    except Exception as exc:  # noqa: BLE001
        record("失败", "Photoshop 新建测试模板（中文路径）", str(exc))
        return

    import renderer
    started = time.monotonic()
    try:
        result = renderer.render(TEST_PSD, {}, TEST_PNG, timeout=180, path_replacements=[
            {"path": ["Man's Name"], "value": "Ruby ClairRuby", "fitBox": MAN_BOX},
            {"path": ["Woman's Name"], "value": "Ann", "fitBox": WOMAN_BOX},
        ])
    except Exception as exc:  # noqa: BLE001
        record("失败", "出图流程", str(exc))
        return
    seconds = time.monotonic() - started
    fitted = list(result.get("textBoxFitted") or [])
    problems = list(result.get("textFitted") or []) + list(result.get("fontWarnings") or [])
    detail = "\n".join(fitted + problems + [f"用时 {seconds:.1f} 秒"])
    if not os.path.isfile(TEST_PNG) or os.path.getsize(TEST_PNG) == 0:
        record("失败", "出图流程", "没有生成 PNG\n" + detail)
    elif not any(line.startswith("Man's Name: 按范围框换为 2 行") for line in fitted):
        # 中文提示乱码(脚本编码问题)或段落文字/换行没生效都会落到这里
        record("失败", "出图：段落文字转换 + 换行 + 中文提示", detail)
    elif problems:
        record("警告", "出图流程", detail)
    else:
        record("通过", "出图流程（中文路径、段落文字转换、换行、放大）", detail)


def check_font_permissions() -> None:
    import font_installer

    directory = font_installer.user_font_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, f".psd-render-selfcheck-{uuid.uuid4().hex}.tmp")
        with open(probe, "wb"):
            pass
        os.remove(probe)
        record("通过", "用户字体目录可写", directory)
    except OSError as exc:
        record("失败", "用户字体目录可写", f"{directory}：{exc}")
    if IS_WINDOWS:
        import winreg
        try:
            winreg.CloseKey(winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, font_installer.USER_FONT_REGISTRY_KEY,
                                               0, winreg.KEY_SET_VALUE))
            record("通过", "字体注册表可写", "HKCU\\" + font_installer.USER_FONT_REGISTRY_KEY)
        except OSError as exc:
            record("失败", "字体注册表可写", str(exc))
    try:
        names = font_installer.installed_font_names()
        record("信息", "已安装字体", f"共识别 {len(names)} 个字体名")
    except Exception as exc:  # noqa: BLE001
        record("警告", "已安装字体", str(exc))


def write_report() -> None:
    counts = {status: sum(1 for s, _, _ in results if s == status) for status in ("通过", "警告", "失败")}
    lines = [f"PSD 批量生图 自检报告  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"结论：通过 {counts['通过']} 项，警告 {counts['警告']} 项，失败 {counts['失败']} 项", ""]
    for status, item, detail in results:
        lines.append(f"[{status}] {item}")
        lines.extend(f"    {line}" for line in detail.splitlines())
    with open(REPORT, "w", encoding="utf-8-sig") as handle:  # 带 BOM: 记事本按 UTF-8 打开
        handle.write("\n".join(lines) + "\n")
    print("\n" + lines[1])
    print(f"报告：{REPORT}\n请把整个「自检结果」文件夹（报告、测试输出、测试图）发给维护人员。")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    for step in (check_environment, run_tests, check_photoshop_render, check_font_permissions):
        try:
            step()
        except Exception:  # noqa: BLE001 - one broken step must not hide the others
            record("失败", f"自检步骤 {step.__name__} 异常", traceback.format_exc())
    write_report()
    try:
        if IS_WINDOWS:
            os.startfile(OUT_DIR)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", OUT_DIR], check=False)
    except OSError:
        pass


if __name__ == "__main__":
    main()
