import os
import tempfile
import unittest

from Util.Parser import LegacyParser, ScriptParser
from Util.WinApi import (
    WindowsWinApi,
    apply_postmessage_prototype,
    classify_post_outcome,
    to_lparam,
    to_wparam,
)
from Util.WindowBind import (
    ABORT_AMBIGUOUS,
    ABORT_BAD_COORDS,
    ABORT_CANCELLED,
    ABORT_DPI,
    ABORT_DPI_UNKNOWN,
    ABORT_MINIMIZED,
    ABORT_MISSING,
    ABORT_NEED_OPT_IN,
    ABORT_NO_SESSION,
    ABORT_PERCENT,
    ABORT_PLAIN_EVENT,
    ABORT_PLAIN_SCRIPT,
    ABORT_POST,
    ABORT_STALE,
    ABORT_UNSUPPORTED,
    CLICK_MOVE_DELAY_MS,
    WM_CHAR,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_RBUTTONDOWN,
    _key_lparam,
    WHEEL_DELTA,
    BackgroundAbort,
    BackgroundSession,
    WindowInfo,
    background_script_document,
    capture_key,
    capture_mouse,
    cli_window_spec,
    decide_run,
    dispatch_route,
    load_event_fields,
    pack_xy,
    prepare_run,
    resolve_target,
    REASON_NO_HIT,
    REASON_NON_CLIENT,
    REASON_NOT_FOREGROUND,
    REASON_OUTSIDE,
    REASON_UNSUPPORTED,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class FakeApi(object):
    def __init__(self):
        self.windows = {}
        self.parent = {}
        self.rects = {}
        self.dpi_map = {}
        self.minimized = set()
        self.children = {}
        self.origin = {}
        self.class_names = {}
        self.ctrl_ids = {}
        self.posts = []
        self.fail_post = False
        self.forced_failure = None
        self.last_post_failure = None
        self.clipboard = []

    def is_valid(self, hwnd):
        return int(hwnd) in self.windows or int(hwnd) in self.class_names

    def info(self, hwnd):
        return self.windows.get(int(hwnd))

    def enum_top_level(self):
        return list(self.windows.values())

    def is_minimized(self, hwnd):
        return int(hwnd) in self.minimized

    def root(self, hwnd):
        return int(self.parent.get(int(hwnd), hwnd))

    def class_name(self, hwnd):
        if int(hwnd) in self.class_names:
            return self.class_names[int(hwnd)]
        info = self.windows.get(int(hwnd))
        return info.class_name if info else ''

    def ctrl_id(self, hwnd):
        return int(self.ctrl_ids.get(int(hwnd), 0))

    def client_rect(self, hwnd):
        return self.rects.get(int(hwnd), (0, 0, 200, 100))

    def screen_to_client(self, hwnd, x, y):
        ox, oy = self.origin.get(int(hwnd), (0, 0))
        return int(x) - ox, int(y) - oy

    def client_to_screen(self, hwnd, x, y):
        ox, oy = self.origin.get(int(hwnd), (0, 0))
        return int(x) + ox, int(y) + oy

    def dpi(self, hwnd):
        return int(self.dpi_map.get(int(hwnd), 96))

    def find_children(self, root, class_name, ctrl_id):
        found = []
        for hwnd in self.children.get(int(root), []):
            if self.class_name(hwnd).lower() == (class_name or '').lower() and self.ctrl_id(hwnd) == int(ctrl_id):
                found.append(int(hwnd))
        return found

    def post(self, hwnd, msg, wparam, lparam):
        if self.fail_post:
            self.last_post_failure = self.forced_failure
            return False
        self.last_post_failure = None
        self.posts.append((int(hwnd), int(msg), int(wparam), int(lparam)))
        return True


def notepad_api():
    api = FakeApi()
    api.windows[1] = WindowInfo(1, '无标题 - 记事本', 'Notepad', 10)
    api.class_names[1] = 'Notepad'
    api.class_names[5] = 'Edit'
    api.ctrl_ids[5] = 15
    api.parent[1] = 1
    api.parent[5] = 1
    api.children[1] = [5]
    api.rects[5] = (0, 0, 400, 300)
    api.origin[5] = (100, 200)
    api.dpi_map[1] = 96
    api.dpi_map[5] = 96
    api.windows[2] = WindowInfo(2, '计算器', 'CalcFrame', 11)
    api.parent[2] = 2
    return api


class RouteTests(unittest.TestCase):
    def test_global_route_is_only_for_normal_events_outside_a_session(self):
        self.assertEqual(dispatch_route(False, False), 'global')
        self.assertEqual(dispatch_route(True, True), 'background')
        self.assertEqual(dispatch_route(False, True), 'abort_plain_event')
        self.assertEqual(dispatch_route(True, False), 'abort_no_session')

    def test_opt_in_does_not_cross_modes(self):
        self.assertEqual(decide_run(False, 'global'), 'global')
        self.assertEqual(decide_run(True, 'background'), 'background')
        self.assertEqual(decide_run(True, 'global'), 'abort_plain_script')
        self.assertEqual(decide_run(False, 'background'), 'abort_need_opt_in')

    def test_old_event_fields_stay_unchanged(self):
        fields = load_event_fields({
            'delay': 10,
            'event_type': 'EM',
            'action_type': 'mouse left down',
            'action': ['0.05208%', '0.1852%'],
        })
        self.assertNotIn('replay', fields)
        self.assertEqual(fields['action'], ['0.05208%', '0.1852%'])

    def test_background_fields_keep_zero_dpi(self):
        fields = load_event_fields({
            'delay': 0,
            'event_type': 'EM',
            'action_type': 'mouse left down',
            'action': [1, 2],
            'replay': 'background',
            'dpi': 0,
            'child': {'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15},
        })
        self.assertEqual(fields['dpi'], 0)
        self.assertEqual(fields['replay'], 'background')


class ResolveTests(unittest.TestCase):
    def test_unique_title_and_live_hwnd(self):
        api = notepad_api()
        info = resolve_target(api, {'hwnd': 1, 'title': '无标题 - 记事本', 'class_name': 'Notepad', 'pid': 10})
        self.assertEqual(info.hwnd, 1)

    def test_stale_pid_falls_back_to_unique_title(self):
        api = notepad_api()
        info = resolve_target(api, {'title': '无标题 - 记事本', 'class_name': 'Notepad', 'pid': 999})
        self.assertEqual(info.hwnd, 1)

    def test_ambiguous_title_aborts(self):
        api = notepad_api()
        api.windows[3] = WindowInfo(3, '无标题 - 记事本', 'Notepad', 12)
        with self.assertRaises(BackgroundAbort) as raised:
            resolve_target(api, {'title': '无标题 - 记事本', 'class_name': 'Notepad'})
        self.assertEqual(raised.exception.reason, ABORT_AMBIGUOUS)

    def test_missing_window_aborts(self):
        api = notepad_api()
        with self.assertRaises(BackgroundAbort) as raised:
            resolve_target(api, {'title': '不存在', 'class_name': 'Nope'})
        self.assertEqual(str(raised.exception.reason), ABORT_MISSING)

    def test_reused_hwnd_does_not_win_when_title_changed(self):
        api = notepad_api()
        api.windows[1] = WindowInfo(1, '别的窗口', 'Notepad', 10)
        with self.assertRaises(BackgroundAbort):
            resolve_target(api, {'hwnd': 1, 'title': '无标题 - 记事本', 'class_name': 'Notepad', 'pid': 10})


class CaptureTests(unittest.TestCase):
    def test_mouse_inside_child_uses_client_coordinates(self):
        api = notepad_api()
        binding = api.windows[1]
        result = capture_mouse(api, binding, 5, 130, 250, 'mouse left down')
        self.assertTrue(result.keep)
        self.assertEqual(result.fields['action'], [30, 50])
        self.assertEqual(result.fields['coord'], 'client')
        self.assertEqual(result.fields['child'], {
            'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15,
        })
        self.assertEqual(result.fields['dpi'], 96)
        self.assertNotIn('hwnd', result.fields['target'])

    def test_outside_window_and_non_client_are_not_recorded(self):
        api = notepad_api()
        binding = api.windows[1]
        outside = capture_mouse(api, binding, 2, 1, 1, 'mouse left down')
        self.assertFalse(outside.keep)
        self.assertEqual(outside.reason, REASON_OUTSIDE)
        non_client = capture_mouse(api, binding, 5, 130, 100, 'mouse left down')
        self.assertFalse(non_client.keep)
        self.assertEqual(non_client.reason, REASON_NON_CLIENT)

    def test_ambiguous_child_falls_back_to_the_bound_window(self):
        api = notepad_api()
        api.class_names[6] = 'Edit'
        api.ctrl_ids[6] = 15
        api.children[1] = [5, 6]
        api.origin[1] = (0, 0)
        result = capture_mouse(api, api.windows[1], 5, 130, 250, 'mouse left down')
        self.assertTrue(result.keep)
        self.assertEqual(result.fields['child']['level'], 'root')
        self.assertEqual(result.fields['child']['class_name'], 'Notepad')
        self.assertEqual(result.fields['action'], [130, 250])

    def test_missing_hit_window_uses_the_bound_client_point(self):
        api = notepad_api()
        api.origin[1] = (10, 20)
        api.rects[1] = (0, 0, 500, 400)
        inside = capture_mouse(api, api.windows[1], 0, 40, 60, 'mouse left down')
        self.assertTrue(inside.keep)
        self.assertEqual(inside.fields['child']['level'], 'root')
        self.assertEqual(inside.fields['action'], [30, 40])
        outside = capture_mouse(api, api.windows[1], 0, -10, -10, 'mouse left down')
        self.assertFalse(outside.keep)
        self.assertEqual(outside.reason, REASON_NO_HIT)

    def test_key_without_a_focus_control_uses_the_bound_window(self):
        api = notepad_api()
        kept = capture_key(api, api.windows[1], 1, 0, 'key down', (65, 'A', 0), 'A')
        self.assertTrue(kept.keep)
        self.assertEqual(kept.fields['child'], {
            'level': 'root', 'class_name': 'Notepad', 'ctrl_id': 0,
        })
        api.class_names[6] = 'Edit'
        api.ctrl_ids[6] = 15
        api.children[1] = [5, 6]
        ambiguous = capture_key(api, api.windows[1], 1, 5, 'key down', (65, 'A', 0), 'A')
        self.assertTrue(ambiguous.keep)
        self.assertEqual(ambiguous.fields['child']['level'], 'root')

    def test_unsupported_mouse_is_not_recorded(self):
        api = notepad_api()
        result = capture_mouse(api, api.windows[1], 5, 130, 250, 'mouse x1 down')
        self.assertFalse(result.keep)
        self.assertEqual(result.reason, REASON_UNSUPPORTED)

    def test_key_records_focus_child_only_when_target_is_foreground(self):
        api = notepad_api()
        kept = capture_key(api, api.windows[1], 1, 5, 'key down', (65, 'A', 0), 'A')
        self.assertTrue(kept.keep)
        self.assertEqual(kept.fields['char'], 'A')
        self.assertEqual(kept.fields['child']['class_name'], 'Edit')
        skipped = capture_key(api, api.windows[1], 2, 5, 'key down', (65, 'A', 0), 'A')
        self.assertFalse(skipped.keep)
        self.assertEqual(skipped.reason, REASON_NOT_FOREGROUND)


class PlaybackTests(unittest.TestCase):
    def session(self, api=None):
        api = api or notepad_api()
        delays = []
        session = BackgroundSession(api, api.windows[1], lambda: False, sleeper=delays.append)
        session.delays = delays
        return api, session

    def mouse(self, action_type, point=(30, 50), dpi=96):
        event = type('E', (), {})()
        event.event_type = 'EM'
        event.action_type = action_type
        event.action = list(point)
        event.child = {'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15}
        event.dpi = dpi
        return event

    def test_click_posts_client_messages_and_does_not_touch_clipboard(self):
        api, session = self.session()
        session.play(self.mouse('mouse left down'))
        session.play(self.mouse('mouse left up'))
        self.assertEqual(api.posts, [
            (5, WM_MOUSEMOVE, 0, pack_xy(30, 50)),
            (5, WM_LBUTTONDOWN, 0x0001, pack_xy(30, 50)),
            (5, WM_LBUTTONUP, 0, pack_xy(30, 50)),
        ])
        self.assertEqual(session.delays, [CLICK_MOVE_DELAY_MS])
        self.assertEqual(api.clipboard, [])

    def test_wheel_uses_screen_coordinates(self):
        api, session = self.session()
        session.play(self.mouse('mouse wheel down'))
        hwnd, msg, wparam, lparam = api.posts[0]
        self.assertEqual(hwnd, 5)
        self.assertEqual(msg, WM_MOUSEWHEEL)
        self.assertEqual(wparam, ((-WHEEL_DELTA) & 0xFFFF) << 16)
        self.assertEqual(lparam, pack_xy(130, 250))

    def test_key_and_text_are_window_messages(self):
        api, session = self.session()
        key = type('E', (), {})()
        key.event_type = 'EK'
        key.action_type = 'key down'
        key.action = [65, 'A', 1]
        key.char = 'A'
        key.child = {'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15}
        key.dpi = 96
        session.play(key)
        text = type('E', (), {})()
        text.event_type = 'EX'
        text.action_type = 'input'
        text.action = '你好'
        text.child = key.child
        text.dpi = 96
        session.play(text)
        self.assertEqual(api.posts[0][1], WM_KEYDOWN)
        self.assertEqual(api.posts[1], (5, WM_CHAR, ord('A'), 1))
        self.assertEqual(api.posts[2][1], WM_CHAR)
        self.assertEqual(api.posts[2][2], ord('你'))
        self.assertEqual(api.posts[3][2], ord('好'))
        self.assertEqual(api.clipboard, [])

    def test_key_up_has_no_char_message(self):
        api, session = self.session()
        key = type('E', (), {})()
        key.event_type = 'EK'
        key.action_type = 'key up'
        key.action = [65, 'A', 0]
        key.child = {'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15}
        key.dpi = 96
        session.play(key)
        self.assertEqual(api.posts, [(5, WM_KEYUP, 65, 1 | (1 << 30) | (1 << 31))])

    def test_cancel_stops_before_later_characters(self):
        api = notepad_api()

        def stop():
            return len(api.posts) >= 1

        session = BackgroundSession(api, api.windows[1], stop)
        text = type('E', (), {})()
        text.event_type = 'EX'
        text.action_type = 'input'
        text.action = 'ab'
        text.child = {'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15}
        text.dpi = 96
        with self.assertRaises(BackgroundAbort) as raised:
            session.play(text)
        self.assertEqual(raised.exception.reason, ABORT_CANCELLED)
        self.assertEqual(len(api.posts), 1)

    def test_safety_aborts_post_nothing(self):
        cases = []

        api = notepad_api()
        api.minimized.add(1)
        cases.append((api, api.windows[1], self.mouse('mouse left down'), ABORT_MINIMIZED))

        api = notepad_api()
        stale_binding = WindowInfo(1, '无标题 - 记事本', 'Notepad', 10)
        api.windows[1] = WindowInfo(1, '已关闭', 'Notepad', 10)
        cases.append((api, stale_binding, self.mouse('mouse left down'), ABORT_STALE))

        api = notepad_api()
        api.dpi_map[5] = 144
        cases.append((api, api.windows[1], self.mouse('mouse left down', dpi=96), ABORT_DPI))

        api = notepad_api()
        api.dpi_map[5] = 96
        cases.append((api, api.windows[1], self.mouse('mouse left down', dpi=0), ABORT_DPI_UNKNOWN))

        api = notepad_api()
        cases.append((api, api.windows[1], self.mouse('mouse x1 down'), ABORT_UNSUPPORTED))

        api = notepad_api()
        bad = self.mouse('mouse left down')
        bad.action = None
        cases.append((api, api.windows[1], bad, ABORT_BAD_COORDS))

        api = notepad_api()
        cases.append((api, api.windows[1], self.mouse('mouse left down', point=('0.1%', '0.2%')), ABORT_PERCENT))

        for api, binding, event, reason in cases:
            session = BackgroundSession(api, binding, lambda: False)
            with self.assertRaises(BackgroundAbort) as raised:
                session.play(event)
            self.assertEqual(raised.exception.reason, reason)
            self.assertEqual(api.posts, [])

    def test_post_failure_aborts(self):
        api, session = self.session()
        api.fail_post = True
        with self.assertRaises(BackgroundAbort) as raised:
            session.play(self.mouse('mouse left down'))
        self.assertTrue(raised.exception.reason.startswith(ABORT_POST))
        self.assertIn('mouse left down', raised.exception.reason)
        self.assertIn('HWND=0x5', raised.exception.reason)
        self.assertIn('class=Edit', raised.exception.reason)
        self.assertIn('Windows 错误 无', raised.exception.reason)
        self.assertIn('不是目标程序忽略了已经投递成功的消息', raised.exception.reason)
        self.assertEqual(api.posts, [])

    def test_both_unknown_dpi_is_allowed(self):
        api, session = self.session()
        api.dpi_map[5] = 0
        session.play(self.mouse('mouse left down', dpi=0))
        self.assertEqual(len(api.posts), 2)

    def test_negative_client_coordinate_is_packed(self):
        self.assertEqual(pack_xy(10, -1), 10 | (0xFFFF << 16))


class PrepareTests(unittest.TestCase):
    def test_global_script_does_not_touch_windows_api(self):
        head = type('N', (), {})()
        head.content = {'type': 'event', 'replay': None}
        head.next_object = None
        head.next_object_if_false = None
        head.script_meta = {'replay': 'global', 'target': None}
        runner = type('R', (), {})()

        def factory():
            raise AssertionError('global mode must not open a window api')

        self.assertEqual(prepare_run(runner, head, False, None, lambda: False, factory), 'global')
        self.assertIsNone(runner.background_session)

    def test_background_script_without_opt_in_aborts_before_api(self):
        head = type('N', (), {})()
        head.content = {'type': 'event', 'replay': 'background'}
        head.next_object = None
        head.next_object_if_false = None
        head.script_meta = {'replay': 'background', 'target': {'title': '无标题 - 记事本', 'class_name': 'Notepad'}}
        runner = type('R', (), {})()

        def factory():
            raise AssertionError('opt-in failure must happen before the api')

        with self.assertRaises(BackgroundAbort) as raised:
            prepare_run(runner, head, False, None, lambda: False, factory)
        self.assertEqual(raised.exception.reason, ABORT_NEED_OPT_IN)

    def test_plain_script_in_background_mode_aborts(self):
        head = type('N', (), {})()
        head.content = {'type': 'event'}
        head.next_object = None
        head.next_object_if_false = None
        head.script_meta = {'replay': 'global', 'target': None}
        runner = type('R', (), {})()
        with self.assertRaises(BackgroundAbort) as raised:
            prepare_run(runner, head, True, None, lambda: False, lambda: None)
        self.assertEqual(raised.exception.reason, ABORT_PLAIN_SCRIPT)

    def test_opt_in_binds_session(self):
        api = notepad_api()
        head = type('N', (), {})()
        head.content = {'type': 'event', 'replay': 'background'}
        head.next_object = None
        head.next_object_if_false = None
        head.script_meta = {'replay': 'background', 'target': {'title': '无标题 - 记事本', 'class_name': 'Notepad'}}
        runner = type('R', (), {})()
        mode = prepare_run(runner, head, True, None, lambda: False, lambda: api)
        self.assertEqual(mode, 'background')
        self.assertEqual(runner.background_session.binding.hwnd, 1)

    def test_minimized_target_aborts(self):
        api = notepad_api()
        api.minimized.add(1)
        head = type('N', (), {})()
        head.content = {'type': 'event', 'replay': 'background'}
        head.next_object = None
        head.next_object_if_false = None
        head.script_meta = {'replay': 'background', 'target': {'title': '无标题 - 记事本', 'class_name': 'Notepad'}}
        runner = type('R', (), {})()
        with self.assertRaises(BackgroundAbort) as raised:
            prepare_run(runner, head, True, None, lambda: False, lambda: api)
        self.assertEqual(raised.exception.reason, ABORT_MINIMIZED)


class ScriptTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json5', delete=False, encoding='utf-8')
        handle.write(text)
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_legacy_script_stays_global(self):
        path = self.write('[[3000, "EM", "mouse left down", ["0.05208%", "0.1852%"]],'
                          '[50, "EK", "key down", [70, "F", 0]]]')
        head = LegacyParser.parse(path)
        self.assertEqual(head.script_meta['replay'], 'global')
        self.assertEqual(head.content['action_type'], 'mouse left down')
        self.assertEqual(head.content['action'], ['0.05208%', '0.1852%'])
        self.assertNotIn('replay', head.content)
        self.assertEqual(head.next_object.content['event_type'], 'EK')
        self.assertEqual(decide_run(False, head.script_meta['replay']), 'global')

    def test_v52_script_keeps_actions_and_global_route(self):
        path = self.write('''{
          scripts: [
            {type: "event", event_type: "EM", delay: 100, action_type: "mouse left down", action: ["0.2604%", "0.4630%"]},
            {type: "event", event_type: "EK", delay: 50, action_type: "key up", action: [70, "F", 0]}
          ]
        }''')
        head = ScriptParser.parse(path)
        self.assertEqual(head.script_meta['replay'], 'global')
        self.assertEqual(head.content['action'], ['0.2604%', '0.4630%'])
        self.assertEqual(head.next_object.content['action'], [70, 'F', 0])
        self.assertEqual(dispatch_route(False, False), 'global')

    def test_background_header_round_trip(self):
        import json
        records = [{
            'type': 'event',
            'delay': 0,
            'event_type': 'EM',
            'action_type': 'mouse left down',
            'action': [30, 50],
            'replay': 'background',
            'coord': 'client',
            'dpi': 96,
            'child': {'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15},
            'target': {'title': '无标题 - 记事本', 'class_name': 'Notepad'},
        }]
        document = background_script_document(records)
        self.assertEqual(document['replay'], 'background')
        handle = tempfile.NamedTemporaryFile('w', suffix='.json5', delete=False, encoding='utf-8')
        json.dump(document, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.remove, handle.name)
        head = ScriptParser.parse(handle.name)
        self.assertEqual(head.script_meta['replay'], 'background')
        self.assertEqual(head.script_meta['target']['class_name'], 'Notepad')
        self.assertEqual(head.content['action'], [30, 50])
        self.assertEqual(head.content['child']['ctrl_id'], 15)

    def test_normal_save_does_not_add_background_header(self):
        records = [{
            'type': 'event',
            'event_type': 'EM',
            'action_type': 'mouse left down',
            'action': ['0.1%', '0.2%'],
        }]
        self.assertEqual(background_script_document(records), {'scripts': records})

    def test_cli_spec(self):
        self.assertIsNone(cli_window_spec('', ''))
        self.assertEqual(cli_window_spec('记事本', 'Notepad'),
                         {'title': '记事本', 'class_name': 'Notepad'})


class SourceGuardTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(ROOT, name), 'r', encoding='utf-8') as handle:
            return handle.read()

    def test_background_modules_do_not_use_global_input(self):
        forbidden = [
            'SetForegroundWindow',
            'AttachThreadInput',
            'SetCursorPos',
            'mouse_event',
            'keybd_event',
            'SendInput',
            'WM_ACTIVATE',
            'WM_MOUSEACTIVATE',
            'WM_SETFOCUS',
            'pyperclip',
            'OpenClipboard',
            'SetClipboard',
        ]
        for name in ('Util/WindowBind.py', 'Util/WinApi.py'):
            text = self.read(name)
            for token in forbidden:
                self.assertNotIn(token, text, '{0} contains {1}'.format(name, token))

    def test_windows_executor_checks_route_before_global_input(self):
        text = self.read('Event/WindowsEvents.py')
        route = text.index('dispatch_route')
        mouse = text.index('mouse_event')
        paste = text.index('pyperclip.copy')
        self.assertLess(route, mouse)
        self.assertLess(route, paste)
        self.assertIn('return', text[route:mouse])
        self.assertIn('load_event_fields', self.read('Event/Event.py'))

    def test_universal_executor_refuses_background_before_pyautogui(self):
        text = self.read('Event/UniversalEvents.py')
        route = text.index('dispatch_route')
        move = text.index('pyautogui.moveTo')
        self.assertLess(route, move)
        self.assertIn('BackgroundAbort', text[route:move])

    def test_runner_falls_back_to_legacy_parser(self):
        text = self.read('Util/RunScriptClass.py')
        self.assertIn('LegacyParser.parse', text)
        self.assertEqual(text.count('prepare_run('), 2)

    def test_abort_messages_are_explicit(self):
        self.assertEqual(ABORT_PLAIN_EVENT, '后台回放遇到普通事件，已停止')
        self.assertEqual(ABORT_NO_SESSION, '后台事件没有开启后台回放，已停止')


class MessageSequenceTests(unittest.TestCase):
    session = PlaybackTests.session
    mouse = PlaybackTests.mouse
    def test_button_up_never_keeps_the_down_flag(self):
        api, session = self.session()
        session.play(self.mouse('mouse right down', point=(3, 4)))
        session.play(self.mouse('mouse right up', point=(3, 4)))
        session.play(self.mouse('mouse middle up', point=(3, 4)))
        self.assertEqual(api.posts[0][1:], (WM_MOUSEMOVE, 0, pack_xy(3, 4)))
        self.assertEqual(api.posts[1][1:], (WM_RBUTTONDOWN, 0x0002, pack_xy(3, 4)))
        self.assertEqual(api.posts[2][2], 0)
        self.assertEqual(api.posts[3][2], 0)
        self.assertEqual(session.delays, [CLICK_MOVE_DELAY_MS])

    def test_missing_child_is_delivered_to_the_bound_window(self):
        api = notepad_api()
        api.children[1] = []
        api, session = self.session(api)
        session.play(self.mouse('mouse left down'))
        self.assertEqual([item[0] for item in api.posts], [1, 1])
        self.assertEqual(api.posts[1][1:], (WM_LBUTTONDOWN, 0x0001, pack_xy(30, 50)))

    def test_wheel_stays_on_screen_coordinates(self):
        api, session = self.session()
        session.play(self.mouse('mouse wheel up'))
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0][1], WM_MOUSEWHEEL)
        self.assertEqual(api.posts[0][3], pack_xy(130, 250))

    def test_arrow_key_lparam_has_scan_code_and_extended_bit(self):
        api, session = self.session()
        api.scan_code = lambda vk: 0x4B if int(vk) == 0x25 else 0
        key = type('E', (), {})()
        key.event_type = 'EK'
        key.action_type = 'key down'
        key.action = [0x25, 'Left', 0]
        key.child = {'level': 'child', 'class_name': 'Edit', 'ctrl_id': 15}
        key.dpi = 96
        key.char = ''
        session.play(key)
        down = api.posts[0][3]
        self.assertEqual(down & 0xFFFF, 1)
        self.assertEqual((down >> 16) & 0xFF, 0x4B)
        self.assertNotEqual(down & (1 << 24), 0)
        self.assertEqual(down & (1 << 31), 0)
        key.action_type = 'key up'
        session.play(key)
        up = api.posts[1][3]
        self.assertNotEqual(up & (1 << 30), 0)
        self.assertNotEqual(up & (1 << 31), 0)
        self.assertEqual((up >> 16) & 0xFF, 0x4B)
        enter_down = _key_lparam(0x0D, 0, False, 0x1C)
        enter_numpad = _key_lparam(0x0D, 1, False, 0x1C)
        self.assertEqual(enter_down & (1 << 24), 0)
        self.assertNotEqual(enter_numpad & (1 << 24), 0)


class WinApiContractTests(unittest.TestCase):
    def gui(self, mode='ok', winerror=5, message='Access is denied.'):
        parent = self

        class error(Exception):
            def __init__(self, code, funcname, strerror):
                super().__init__(code, funcname, strerror)
                self.winerror = code
                self.funcname = funcname
                self.strerror = strerror

        class Gui(object):
            def __init__(self):
                self.calls = []
                self.mode = mode
                self.winerror = winerror
                self.message = message

            def PostMessage(self, hwnd, msg, wparam, lparam):
                self.calls.append((hwnd, msg, wparam, lparam))
                if self.mode == 'ok':
                    return None
                if self.mode == 'boom':
                    raise OverflowError('int too big to convert')
                raise error(self.winerror, 'PostMessage', self.message)

            def GetLastError(self):
                parent.fail('PostMessage 之后不能再读 GetLastError')

        holder = WindowsWinApi.__new__(WindowsWinApi)
        holder.win32gui = Gui()
        holder.last_post_failure = None
        return holder

    def test_pywin32_none_is_success_even_if_last_error_would_be_stale(self):
        holder = self.gui()
        hwnd = 0x100000001
        self.assertTrue(holder.post(hwnd, WM_LBUTTONDOWN, 1, pack_xy(10, -1)))
        self.assertIsNone(holder.last_post_failure)
        self.assertEqual(holder.win32gui.calls[0][0], hwnd)
        self.assertEqual(holder.win32gui.calls[0][3] & 0xFFFFFFFF, pack_xy(10, -1) & 0xFFFFFFFF)

    def test_pywin32_error_keeps_the_code_from_the_exception(self):
        holder = self.gui(mode='raise', winerror=5, message='Access is denied.')
        self.assertFalse(holder.post(16, WM_LBUTTONDOWN, 1, 0))
        self.assertEqual(holder.last_post_failure.winerror, 5)
        self.assertEqual(holder.last_post_failure.message, 'Access is denied.')
        holder = self.gui(mode='raise', winerror=1400, message='Invalid window handle.')
        self.assertFalse(holder.post(16, WM_KEYDOWN, 65, 1))
        self.assertEqual(holder.last_post_failure.winerror, 1400)
        self.assertIn('Invalid window handle', holder.last_post_failure.message)

    def test_non_api_exception_is_not_turned_into_a_fake_winerror(self):
        holder = self.gui(mode='boom')
        self.assertFalse(holder.post(16, WM_LBUTTONDOWN, 1, 0))
        self.assertIsNone(holder.last_post_failure.winerror)
        self.assertIn('OverflowError', holder.last_post_failure.message)

    def test_classify_ignores_a_stale_last_error_after_success(self):
        self.assertIsNone(classify_post_outcome(None, captured_last_error=5))
        self.assertIsNone(classify_post_outcome(1, captured_last_error=1400))
        denied = classify_post_outcome(0, captured_last_error=5)
        self.assertEqual(denied.winerror, 5)
        wrapped = classify_post_outcome(
            None,
            api_error=type('error', (Exception,), {
                'winerror': 5,
                'funcname': 'PostMessage',
                'strerror': 'Access is denied.',
            })(),
            captured_last_error=0,
        )
        self.assertEqual(wrapped.winerror, 5)
        self.assertNotEqual(wrapped.winerror, 0)

    def test_ctypes_prototype_keeps_pointer_sized_handles(self):
        import ctypes
        seen = {}

        def _cb(hwnd, msg, wparam, lparam):
            seen['hwnd'] = hwnd
            seen['wparam'] = wparam
            seen['lparam'] = lparam
            return 1

        prototype = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        function = prototype(_cb)
        apply_postmessage_prototype(function)
        self.assertIs(function.argtypes[0], ctypes.c_void_p)
        self.assertIs(function.argtypes[2], ctypes.c_size_t)
        self.assertIs(function.argtypes[3], ctypes.c_ssize_t)
        self.assertGreaterEqual(ctypes.sizeof(function.argtypes[0]), 8)
        self.assertNotEqual(function.argtypes[0], ctypes.c_int)
        hwnd = 0x100000001
        lparam = to_lparam(pack_xy(10, -1))
        wparam = to_wparam(((-WHEEL_DELTA) & 0xFFFF) << 16)
        self.assertEqual(function(hwnd, WM_MOUSEWHEEL, wparam, lparam), 1)
        self.assertEqual(seen['hwnd'], hwnd)
        self.assertEqual(seen['lparam'] & 0xFFFFFFFF, pack_xy(10, -1) & 0xFFFFFFFF)
        self.assertEqual(seen['wparam'] & 0xFFFFFFFF, ((-WHEEL_DELTA) & 0xFFFF) << 16)
        truncated = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)(lambda value: value or 0)
        try:
            narrowed = truncated(hwnd)
        except (OverflowError, ctypes.ArgumentError):
            narrowed = None
        self.assertNotEqual(narrowed, hwnd)


class BackgroundChainTests(unittest.TestCase):
    def test_checked_background_record_parses_and_accepts_pywin32_none(self):
        import json
        records = [{
            'type': 'event',
            'delay': 1000,
            'event_type': 'EM',
            'action_type': 'mouse left down',
            'action': [120, 40],
            'replay': 'background',
            'coord': 'client',
            'dpi': 96,
            'child': {'level': 'root', 'class_name': 'UnrealWindow', 'ctrl_id': 0},
            'target': {'title': 'MIR4', 'class_name': 'UnrealWindow'},
        }]
        document = background_script_document(records)
        self.assertEqual(document['replay'], 'background')
        handle = tempfile.NamedTemporaryFile('w', suffix='.json5', delete=False, encoding='utf-8')
        json.dump(document, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.remove, handle.name)
        head = ScriptParser.parse(handle.name)
        self.assertEqual(head.script_meta['replay'], 'background')
        self.assertEqual(head.script_meta['target']['title'], 'MIR4')
        self.assertEqual(head.content['action'], [120, 40])
        self.assertEqual(head.content['child']['level'], 'root')

        hwnd = 0x100000001
        api = FakeApi()
        api.windows[hwnd] = WindowInfo(hwnd, 'MIR4', 'UnrealWindow', 4242)
        api.class_names[hwnd] = 'UnrealWindow'
        api.parent[hwnd] = hwnd
        api.rects[hwnd] = (0, 0, 1920, 1080)
        api.dpi_map[hwnd] = 96
        gui = WinApiContractTests.gui(self)
        real_post = api.post

        def post(target, msg, wparam, lparam):
            ok = WindowsWinApi.post(gui, target, msg, wparam, lparam)
            api.last_post_failure = gui.last_post_failure
            if ok:
                real_post(target, msg, wparam, lparam)
            return ok

        api.post = post
        delays = []
        runner = type('R', (), {'sleep': delays.append})()
        mode = prepare_run(runner, head, True, None, lambda: False, lambda: api)
        self.assertEqual(mode, 'background')
        event = type('E', (), {})()
        for key in ('event_type', 'action_type', 'action', 'child', 'dpi'):
            setattr(event, key, head.content[key])
        runner.background_session.play(event)
        self.assertEqual(delays, [CLICK_MOVE_DELAY_MS])
        self.assertEqual(gui.win32gui.calls[0][0], hwnd)
        self.assertEqual(gui.win32gui.calls[0][1], WM_MOUSEMOVE)
        self.assertEqual(gui.win32gui.calls[1][1], WM_LBUTTONDOWN)
        self.assertEqual(gui.win32gui.calls[1][2], 0x0001)
        self.assertEqual(gui.win32gui.calls[1][3], pack_xy(120, 40))
        self.assertIsNone(gui.last_post_failure)

        gui.win32gui.mode = 'raise'
        gui.win32gui.winerror = 5
        gui.win32gui.message = 'Access is denied.'
        with self.assertRaises(BackgroundAbort) as raised:
            runner.background_session.play(event)
        self.assertIn('Windows 错误 5', raised.exception.reason)
        self.assertIn('Access is denied', raised.exception.reason)
        self.assertIn('HWND=0x100000001', raised.exception.reason)
        self.assertIn('class=UnrealWindow', raised.exception.reason)
        self.assertIn('不是目标程序忽略了已经投递成功的消息', raised.exception.reason)
        self.assertEqual(raised.exception.status, 'stopped: WM_MOUSEMOVE 5')


if __name__ == '__main__':
    unittest.main()
