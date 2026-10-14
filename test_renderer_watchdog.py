import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import excel_data
import renderer


def _fake_com(app):
    """pythoncom / win32com stand-ins that record which thread initialised COM."""
    calls = {"init": set(), "dispatch": []}

    def co_initialize():
        calls["init"].add(threading.get_ident())

    def dispatch(name):
        # Real COM raises "尚未调用 CoInitialize" on a thread that skipped it.
        if threading.get_ident() not in calls["init"]:
            raise RuntimeError("尚未调用 CoInitialize")
        calls["dispatch"].append(name)
        return app

    pythoncom = types.SimpleNamespace(CoInitialize=co_initialize, CoUninitialize=lambda: None)
    client = types.SimpleNamespace(Dispatch=dispatch)
    win32com = types.SimpleNamespace(client=client)
    modules = {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client}
    return calls, mock.patch.dict(sys.modules, modules)


class ComThreadTests(unittest.TestCase):
    def test_quit_initialises_com_on_its_own_thread(self):
        app = mock.Mock()
        calls, patch_modules = _fake_com(app)
        running = iter([True, False])
        with patch_modules, mock.patch.object(renderer, "IS_WINDOWS", True), \
                mock.patch.object(renderer, "photoshop_running", lambda: next(running)), \
                mock.patch.object(renderer.time, "sleep", lambda _: None):
            self.assertTrue(renderer.quit_photoshop(timeout=5))
        app.Quit.assert_called_once()
        self.assertEqual(calls["dispatch"], ["Photoshop.Application"])

    def test_ready_check_reports_a_photoshop_that_never_answers(self):
        release = threading.Event()
        app = mock.Mock()
        type(app).Version = mock.PropertyMock(side_effect=lambda: release.wait(5) or "24.0")
        _, patch_modules = _fake_com(app)
        with patch_modules, mock.patch.object(renderer, "IS_WINDOWS", True):
            with self.assertRaisesRegex(renderer.RenderError, "仍未响应"):
                renderer.ensure_photoshop_ready(timeout=0.2)
        release.set()


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.progress = os.path.join(self._tmp.name, "result.json.progress")

    def tearDown(self):
        self._tmp.cleanup()

    def _script(self, steps, gap, finish=True):
        """Simulated JSX: append one step every ``gap`` seconds; wait() reports when done."""
        done = threading.Event()

        def run():
            for index, step in enumerate(steps):
                with open(self.progress, "a", encoding="utf-8") as handle:
                    handle.write(f"{index * int(gap * 1000)}\t{step}\n")
                time.sleep(gap)
            if finish:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        return lambda seconds: done.wait(seconds)

    def test_slow_script_that_keeps_progressing_is_not_killed(self):
        wait = self._script(["开始执行脚本", "打开 PSD", "打开智能对象 人物", "导出 PNG"], gap=0.6)
        self.assertIsNone(renderer._watch(wait, self.progress, stall_timeout=1.0))

    def test_stalled_step_is_reported_with_its_name(self):
        wait = self._script(["开始执行脚本", "打开 PSD", "打开智能对象 Characters' Look"], gap=0.1, finish=False)
        timed_out = renderer._watch(wait, self.progress, stall_timeout=0.8)
        self.assertIsNotNone(timed_out)
        error = renderer._timeout_error(self.progress, *timed_out, 0.8)
        self.assertIn("「打开智能对象 Characters' Look」之后", str(error))
        self.assertIn("打开 PSD 0s → 打开智能对象 Characters' Look（进行中）", str(error))

    def test_total_cap_stops_a_script_that_never_ends(self):
        wait = self._script([f"切换显隐 {i}" for i in range(40)], gap=0.05, finish=False)
        with mock.patch.object(renderer, "MAX_RENDER_SECONDS", 0.6):
            stalled, total = renderer._watch(wait, self.progress, stall_timeout=60)
        self.assertLess(stalled, 1)
        self.assertGreaterEqual(total, 0.6)


class OutputNameKeyTests(unittest.TestCase):
    def test_names_differing_only_in_case_collide(self):
        self.assertEqual(excel_data.output_name_key("aB3kF9x2-1.png"), excel_data.output_name_key("Ab3kf9X2-1.PNG"))
        self.assertNotEqual(excel_data.output_name_key("aB3kF9x2-1.png"), excel_data.output_name_key("aB3kF9x2-2.png"))

    def test_control_characters_never_reach_the_file_name(self):
        row = types.SimpleNamespace(sku="HJ-A", hash="", order_no="", row_no=2, values={"图片命名": "normal\x01name\t2"})
        self.assertEqual(excel_data.output_basename(row, False, "", 1, name_column="图片命名"), "normal_name_2.png")


class PhotoshopFontsTests(unittest.TestCase):
    """2026-10-14: ask an already running Photoshop for its fonts; never launch or kill it."""

    def test_not_running_returns_none_without_calling_photoshop(self):
        with mock.patch.object(renderer, "photoshop_running", return_value=False), \
             mock.patch.object(renderer.subprocess, "run") as run:
            self.assertIsNone(renderer.photoshop_fonts())
        run.assert_not_called()

    @unittest.skipIf(renderer.IS_WINDOWS, "macOS 分支")
    def test_reads_font_list_written_by_the_script(self):
        def fake_run(args, **kwargs):
            script = args[-1]
            jsx = script.split("new File('", 1)[1].split("')", 1)[0]
            with open(jsx, encoding="utf-8-sig") as handle:
                out = handle.read().split('var OUT = "', 1)[1].split('"', 1)[0]
            with open(out, "w", encoding="utf-8") as handle:
                handle.write("ArnoPro-Bold\nMyriadPro-Regular\n")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(renderer, "photoshop_running", return_value=True), \
             mock.patch.object(renderer.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(renderer, "kill_photoshop") as kill:
            self.assertEqual(renderer.photoshop_fonts(), {"ArnoPro-Bold", "MyriadPro-Regular"})
        kill.assert_not_called()

    @unittest.skipIf(renderer.IS_WINDOWS, "macOS 分支")
    def test_no_answer_falls_back_without_killing_photoshop(self):
        timeout = renderer.subprocess.TimeoutExpired("osascript", 1)
        with mock.patch.object(renderer, "photoshop_running", return_value=True), \
             mock.patch.object(renderer.subprocess, "run", side_effect=timeout), \
             mock.patch.object(renderer, "kill_photoshop") as kill:
            self.assertIsNone(renderer.photoshop_fonts(timeout=1))
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
