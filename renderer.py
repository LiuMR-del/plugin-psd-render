"""Photoshop 渲染平台适配 + 看门狗.

渲染业务逻辑全部在 render.jsx (两平台共用同一 JSX 引擎):
- Windows: win32com Dispatch("Photoshop.Application").DoJavaScriptFile(), COM 调用跑子线程
- macOS:   osascript 'tell application id "com.adobe.photoshop" to do javascript ...'

参数经临时 JSON 传递(路径以字符串替换注入 jsx, 避免命令行转义问题);
JSX 末尾写 result JSON, Python 读它判断成败。

看门狗(2026-10-14): 先单独启动 Photoshop 并等它能响应(STARTUP_TIMEOUT, 冷启动不占出图时间),
再执行脚本。JSX 每做一步往 result JSON 旁的 .progress 文件追加一行; 某一步超过 stall 秒
没有新进度、或单张超过 MAX_RENDER_SECONDS, 判定卡死 -> 强杀 Photoshop, 报错里写明卡在哪一步。
超时/失败 -> 重启 Photoshop 后该张重试 1 次, 仍失败抛 RenderError。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

IS_WINDOWS = sys.platform == "win32"
RENDER_JSX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render.jsx")
# 打包成无控制台的 exe 后, 调 tasklist/taskkill 不弹出黑色命令行窗口
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}

STALL_TIMEOUT = 180.0        # 某一步超过这么久没有新进度 -> 卡死(多半是 Photoshop 弹了对话框)
MAX_RENDER_SECONDS = 900.0   # 单张总上限
STARTUP_TIMEOUT = 300.0      # 启动 Photoshop 并等它能响应
RESULT_FILENAME = "result.json"
PROGRESS_SUFFIX = ".progress"  # JSX 写在 RESULT_PATH + 该后缀, 不改参数协议


class RenderError(Exception):
    """渲染失败(Photoshop 返回错误或无结果)."""


class RenderTimeout(RenderError):
    """渲染超时(看门狗触发)."""


def _to_jsx_path(path: str) -> str:
    # ExtendScript 的 File() 接受正斜杠路径, 两平台通用
    return os.path.abspath(path).replace("\\", "/")


def kill_photoshop() -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/IM", "Photoshop.exe"], capture_output=True, **_NO_WINDOW)
    else:
        subprocess.run(["pkill", "-f", "Adobe Photoshop"], capture_output=True)
    time.sleep(3)


def photoshop_running() -> bool:
    """True when Photoshop is running; never launches it."""
    try:
        if IS_WINDOWS:
            proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Photoshop.exe", "/NH"],
                                  capture_output=True, text=True, timeout=15, **_NO_WINDOW)
            return "photoshop.exe" in (proc.stdout or "").lower()
        proc = subprocess.run(["osascript", "-e", 'application id "com.adobe.photoshop" is running'],
                              capture_output=True, text=True, timeout=15)
        return (proc.stdout or "").strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _start_com_call(action) -> tuple[threading.Thread, dict]:
    """Run ``action(photoshop_app)`` on its own thread; state gets "result" or "error".

    COM must be initialised on every thread that uses it; the app calls
    Photoshop from background threads, so never Dispatch outside this helper.
    """
    state: dict = {}

    def worker():
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]

        pythoncom.CoInitialize()
        try:
            state["result"] = action(win32com.client.Dispatch("Photoshop.Application"))
        except Exception as e:  # noqa: BLE001
            state["error"] = str(e)
        finally:
            pythoncom.CoUninitialize()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, state


def ensure_photoshop_ready(timeout: float = STARTUP_TIMEOUT) -> float:
    """Start Photoshop if needed and wait until it answers; return the seconds it took."""
    started = time.monotonic()
    hint = "可能卡在登录、授权或其他弹窗，请查看 Photoshop 窗口"
    if IS_WINDOWS:
        thread, state = _start_com_call(lambda app: str(app.Version))
        thread.join(timeout)
        if thread.is_alive():
            raise RenderError(f"启动 Photoshop 超过 {timeout:.0f} 秒仍未响应（{hint}）")
        if "error" in state:
            raise RenderError(f"无法连接 Photoshop: {state['error']}")
    else:
        try:
            proc = subprocess.run(["osascript", "-e", 'tell application id "com.adobe.photoshop" to get version'],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RenderError(f"启动 Photoshop 超过 {timeout:.0f} 秒仍未响应（{hint}）") from None
        if proc.returncode != 0:
            raise RenderError(f"无法连接 Photoshop: {(proc.stderr or '').strip()}")
    return time.monotonic() - started


def quit_photoshop(timeout: float = 90.0) -> bool:
    """Ask Photoshop to quit normally so newly installed fonts load on restart.

    Never force-kills: if the user has unsaved documents Photoshop asks them,
    and we only wait. Returns True when Photoshop is no longer running.
    """
    if not photoshop_running():
        return True
    if IS_WINDOWS:
        thread, _ = _start_com_call(lambda app: app.Quit())
        thread.join(30)  # 有未保存文档时 Quit 会等用户处理, 下面继续等进程退出
    else:
        subprocess.Popen(["osascript", "-e", 'tell application id "com.adobe.photoshop" to quit'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        if not photoshop_running():
            time.sleep(2)
            return True
    return False


FONT_QUERY_TIMEOUT = 45.0
_FONT_LIST_JSX = r"""
var OUT = "__FONTS_OUT_PATH__";
var names = [];
for (var i = 0; i < app.fonts.length; i++) {
    try { names.push(app.fonts[i].postScriptName); } catch (e) {}
}
var f = new File(OUT);
f.encoding = "UTF-8";
f.open("w");
f.write(names.join("\n"));
f.close();
"""


def photoshop_fonts(timeout: float = FONT_QUERY_TIMEOUT) -> set[str] | None:
    """PostScript names of the fonts the running Photoshop can use (2026-10-14).

    Only asks a Photoshop that is already open: never launches or kills it.
    Returns None when Photoshop is not running, does not answer in time (e.g.
    a dialog is open) or the script fails; the caller then falls back to
    scanning font folders.
    """
    if not photoshop_running():
        return None
    work = tempfile.mkdtemp(prefix="psd-render-fonts-")
    try:
        out_path = os.path.join(work, "fonts.txt")
        jsx_path = os.path.join(work, "fonts.jsx")
        with open(jsx_path, "w", encoding="utf-8-sig") as handle:  # 与出图一致: 带 BOM
            handle.write(_FONT_LIST_JSX.replace("__FONTS_OUT_PATH__", _to_jsx_path(out_path)))
        if IS_WINDOWS:
            thread, state = _start_com_call(lambda app: app.DoJavaScriptFile(jsx_path))
            thread.join(timeout)
            if thread.is_alive() or "error" in state:
                return None
        else:
            script = ('tell application id "com.adobe.photoshop" to do javascript '
                      '"$.evalFile(new File(\'%s\'))"' % _to_jsx_path(jsx_path))
            try:
                proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return None
            if proc.returncode != 0:
                return None
        try:
            with open(out_path, encoding="utf-8-sig", errors="replace") as handle:
                names = {line.strip() for line in handle if line.strip()}
        except OSError:
            return None
        return names or None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _read_progress(path: str) -> list[tuple[float, str]]:
    """[(seconds since script start, step)] written by render.jsx."""
    steps = []
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                elapsed, _, step = line.rstrip("\r\n").partition("\t")
                if step and elapsed.strip().isdigit():
                    steps.append((int(elapsed) / 1000, step))
    except OSError:
        pass
    return steps


def _progress_summary(steps: list[tuple[float, str]], limit: int = 6) -> str:
    timed = [f"{step} {steps[i + 1][0] - at:.0f}s" if i + 1 < len(steps) else f"{step}（进行中）"
             for i, (at, step) in enumerate(steps)]
    return " → ".join((["…"] if len(timed) > limit else []) + timed[-limit:])


def _watch(wait, progress_path: str, stall_timeout: float) -> tuple[float, float] | None:
    """Wait for the script; None when it finished, else (stalled, total) seconds.

    ``wait(seconds)`` blocks up to that long and returns True once the script ended.
    Any growth of the progress file counts as progress.
    """
    started = last_change = time.monotonic()
    last_size = -1
    while not wait(0.5):
        now = time.monotonic()
        try:
            size = os.path.getsize(progress_path)
        except OSError:
            size = -1
        if size != last_size:
            last_size, last_change = size, now
        if now - last_change > stall_timeout or now - started > MAX_RENDER_SECONDS:
            return now - last_change, now - started
    return None


def _timeout_error(progress_path: str, stalled: float, total: float, stall_timeout: float) -> RenderTimeout:
    steps = _read_progress(progress_path)
    where = steps[-1][1] if steps else "Photoshop 尚未开始执行脚本"
    reason = (f"「{where}」之后 {stalled:.0f} 秒没有进展" if stalled > stall_timeout
              else f"超过单张上限 {MAX_RENDER_SECONDS:.0f} 秒，最后一步「{where}」")
    detail = f"；步骤用时：{_progress_summary(steps)}" if steps else ""
    return RenderTimeout(f"渲染卡住：{reason}（共 {total:.0f} 秒），已强制结束 Photoshop。"
                         f"Photoshop 可能弹出了对话框，请查看 Photoshop 窗口{detail}")


def _progress_path_for(jsx_path: str) -> str:
    return os.path.join(os.path.dirname(jsx_path), RESULT_FILENAME + PROGRESS_SUFFIX)


def _execute_macos(jsx_path: str, timeout: float) -> float:
    startup = ensure_photoshop_ready()
    script = (
        'tell application id "com.adobe.photoshop" to do javascript '
        '"$.evalFile(new File(\'%s\'))"' % jsx_path
    )
    proc = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait(seconds: float) -> bool:
        try:
            proc.wait(seconds)
            return True
        except subprocess.TimeoutExpired:
            return False

    progress_path = _progress_path_for(jsx_path)
    timed_out = _watch(wait, progress_path, timeout)
    if timed_out:
        proc.kill()
        kill_photoshop()
        raise _timeout_error(progress_path, *timed_out, timeout)
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RenderError(f"osascript 调用失败: {(stderr or '').strip()}")
    return startup


def _execute_windows(jsx_path: str, timeout: float) -> float:
    startup = ensure_photoshop_ready()
    thread, state = _start_com_call(lambda app: app.DoJavaScriptFile(jsx_path))

    def wait(seconds: float) -> bool:
        thread.join(seconds)
        return not thread.is_alive()

    progress_path = _progress_path_for(jsx_path)
    timed_out = _watch(wait, progress_path, timeout)
    if timed_out:
        kill_photoshop()
        raise _timeout_error(progress_path, *timed_out, timeout)
    if "error" in state:
        raise RenderError(f"COM 调用失败: {state['error']}")
    return startup


_execute = _execute_windows if IS_WINDOWS else _execute_macos


def _render_once(psd_path: str, replacements: dict, out_png_path: str, timeout: float,
                 visibility_actions: list | None = None,
                 path_replacements: list | None = None,
                 smart_object_text_replacements: list | None = None,
                 image_replacements: list | None = None,
                 font_name: str = "") -> dict:
    work_dir = tempfile.mkdtemp(prefix="psd-render-")
    try:
        params_path = os.path.join(work_dir, "params.json")
        result_path = os.path.join(work_dir, RESULT_FILENAME)
        # SaveForWeb 对非 ASCII 文件名不可靠: 先导出到临时 ASCII 路径, 成功后再改名落盘
        tmp_png_path = os.path.join(work_dir, "out.png")
        normalized_smart_tasks = []
        for item in smart_object_text_replacements or []:
            task = dict(item)
            task["fontName"] = str(font_name or "")
            normalized_smart_tasks.append(task)
        normalized_image_replacements = []
        for item in image_replacements or []:
            normalized = dict(item)
            source_path = str(item.get("sourcePath") or "")
            normalized["sourcePath"] = _to_jsx_path(source_path) if source_path else ""
            normalized_image_replacements.append(normalized)
        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "psdPath": _to_jsx_path(psd_path),
                    "outPngPath": _to_jsx_path(tmp_png_path),
                    "replacements": replacements,
                    "visibilityActions": visibility_actions or [],
                    "pathReplacements": path_replacements or [],
                    "smartObjectTextReplacements": normalized_smart_tasks,
                    "imageReplacements": normalized_image_replacements,
                    "fontName": str(font_name or ""),
                },
                f,
                ensure_ascii=False,
            )
        with open(RENDER_JSX_PATH, encoding="utf-8-sig") as f:
            jsx_source = f.read()
        jsx_source = jsx_source.replace("__PARAMS_JSON_PATH__", _to_jsx_path(params_path))
        jsx_source = jsx_source.replace("__RESULT_JSON_PATH__", _to_jsx_path(result_path))
        jsx_path = os.path.join(work_dir, "task.jsx")
        # 带 BOM: 无 BOM 时 ExtendScript 可能按系统编码(中文 Windows 为 GBK)读脚本,
        # 脚本里的中文提示和注入的中文路径会乱码
        with open(jsx_path, "w", encoding="utf-8-sig") as f:
            f.write(jsx_source)

        started = time.monotonic()
        startup = _execute(jsx_path, timeout)

        if not os.path.exists(result_path):
            raise RenderError("Photoshop 未返回执行结果")
        with open(result_path, encoding="utf-8") as f:
            result = json.load(f)
        if not result.get("ok"):
            raise RenderError(result.get("error") or "未知错误")
        if not os.path.exists(tmp_png_path):
            raise RenderError("导出的 PNG 文件不存在")
        os.makedirs(os.path.dirname(os.path.abspath(out_png_path)), exist_ok=True)
        shutil.move(tmp_png_path, out_png_path)
        # 只加在 Python 侧的结果里(不是 JSX 协议): 供日志显示用时
        result["elapsedSeconds"] = time.monotonic() - started
        if isinstance(startup, (int, float)):
            result["photoshopStartupSeconds"] = float(startup)
        return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def render(psd_path: str, replacements: dict, out_png_path: str,
           timeout: float = STALL_TIMEOUT, log=None,
           visibility_actions: list | None = None,
           path_replacements: list | None = None,
           smart_object_text_replacements: list | None = None,
           image_replacements: list | None = None,
           font_name: str = "") -> dict:
    """渲染一张 PNG; 超时/失败自动重启 Photoshop 重试 1 次, 仍失败抛 RenderError.

    ``timeout``: 某一步多少秒没有新进度判定卡死(单张总上限见 MAX_RENDER_SECONDS)。
    """
    last_error: RenderError | None = None
    for attempt in (1, 2):
        try:
            return _render_once(psd_path, replacements, out_png_path, timeout,
                                visibility_actions=visibility_actions,
                                path_replacements=path_replacements,
                                smart_object_text_replacements=smart_object_text_replacements,
                                image_replacements=image_replacements,
                                font_name=font_name)
        except RenderError as e:
            last_error = e
            if attempt == 1:
                if log:
                    log(f"渲染失败({e}), 重启 Photoshop 重试")
                if not isinstance(e, RenderTimeout):  # 超时路径已 kill 过
                    kill_photoshop()
    assert last_error is not None
    raise last_error
