"""Windows 后台回放的纯逻辑。

这里不调用全局输入，也不引用 win32。Windows 调用集中在 Util.WinApi。
"""

from platform import system


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010
WHEEL_DELTA = 120

REASON_UNBOUND = '未绑定窗口'
REASON_NO_HIT = '没有命中窗口'
REASON_OUTSIDE = '不在目标窗口'
REASON_MINIMIZED = '目标窗口已最小化'
REASON_NON_CLIENT = '不在客户区'
REASON_UNSUPPORTED = '不支持的动作'
REASON_NOT_FOREGROUND = '前台不是目标窗口'
REASON_NO_FOCUS = '无法确定焦点控件'
REASON_FOCUS_OUTSIDE = '焦点不在目标窗口'
REASON_AMBIGUOUS_CHILD = '子窗口不唯一，已跳过'

ABORT_PLAIN_SCRIPT = '后台模式不会执行普通脚本，已停止'
ABORT_NEED_OPT_IN = '该脚本是后台脚本，需要显式开启后台回放；已停止，不会改用全局输入'
ABORT_NO_TARGET = '后台脚本没有可用的目标窗口，已停止'
ABORT_NOT_WINDOWS = '后台回放仅支持 Windows，已停止'
ABORT_MISSING = '找不到目标窗口，已停止'
ABORT_AMBIGUOUS = '找到多个同名窗口，已停止'
ABORT_STALE = '目标窗口已失效或已变化，已停止'
ABORT_MINIMIZED = '目标窗口已最小化，已停止'
ABORT_CHILD_MISSING = '找不到对应子窗口，已停止'
ABORT_CHILD_AMBIGUOUS = '子窗口不唯一，已停止'
ABORT_DPI = 'DPI 与录制时不一致，已停止'
ABORT_DPI_UNKNOWN = '无法确认 DPI，已停止'
ABORT_UNSUPPORTED = '后台回放遇到不支持的动作，已停止'
ABORT_BAD_COORDS = '后台鼠标事件缺少客户区坐标，已停止'
ABORT_PERCENT = '后台鼠标事件不能使用屏幕百分比坐标，已停止'
ABORT_PLAIN_EVENT = '后台回放遇到普通事件，已停止'
ABORT_NO_SESSION = '后台事件没有开启后台回放，已停止'
ABORT_POST = '窗口消息发送失败，已停止'
ABORT_CANCELLED = '已停止'
ABORT_NO_CHILD = '后台文字事件没有目标控件，已停止'

MOUSE_BUTTONS = {
    'mouse left down': (WM_LBUTTONDOWN, MK_LBUTTON),
    'mouse left up': (WM_LBUTTONUP, 0),
    'mouse right down': (WM_RBUTTONDOWN, MK_RBUTTON),
    'mouse right up': (WM_RBUTTONUP, 0),
    'mouse middle down': (WM_MBUTTONDOWN, MK_MBUTTON),
    'mouse middle up': (WM_MBUTTONUP, 0),
}
SUPPORTED_MOUSE = set(MOUSE_BUTTONS) | {'mouse move', 'mouse wheel up', 'mouse wheel down'}
SUPPORTED_KEYS = {'key down', 'key up'}


class BackgroundAbort(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class WindowInfo(object):
    def __init__(self, hwnd, title, class_name, pid):
        self.hwnd = int(hwnd)
        self.title = title or ''
        self.class_name = class_name or ''
        self.pid = int(pid or 0)

    def as_spec(self):
        return {
            'hwnd': self.hwnd,
            'title': self.title,
            'class_name': self.class_name,
            'pid': self.pid,
        }


class CaptureResult(object):
    def __init__(self, keep, reason='', fields=None):
        self.keep = keep
        self.reason = reason
        self.fields = fields or {}


def pack_xy(x, y):
    return (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)


def utf16_units(text):
    encoded = text.encode('utf-16-le')
    units = []
    for index in range(0, len(encoded), 2):
        units.append(encoded[index] | (encoded[index + 1] << 8))
    return units


def load_event_fields(content):
    fields = {}
    for key in ('delay', 'event_type', 'action_type', 'action'):
        fields[key] = content[key]
    for key in ('replay', 'coord', 'child', 'dpi', 'target', 'char'):
        if key in content and content[key] is not None:
            fields[key] = content[key]
    return fields


def event_is_background(event):
    return getattr(event, 'replay', None) == 'background'


def dispatch_route(event_background, session_active):
    """后台会话里绝不返回 global。没有会话的后台事件也不返回 global。"""
    if session_active and event_background:
        return 'background'
    if session_active and not event_background:
        return 'abort_plain_event'
    if event_background and not session_active:
        return 'abort_no_session'
    return 'global'


def abort_for_route(route):
    if route == 'abort_plain_event':
        return ABORT_PLAIN_EVENT
    if route == 'abort_no_session':
        return ABORT_NO_SESSION
    if route == 'background':
        return ABORT_NOT_WINDOWS
    return ABORT_UNSUPPORTED


def decide_run(requested_background, script_replay):
    script_background = script_replay == 'background'
    if requested_background and not script_background:
        return 'abort_plain_script'
    if script_background and not requested_background:
        return 'abort_need_opt_in'
    if script_background:
        return 'background'
    return 'global'


def decide_abort_reason(decision):
    if decision == 'abort_plain_script':
        return ABORT_PLAIN_SCRIPT
    if decision == 'abort_need_opt_in':
        return ABORT_NEED_OPT_IN
    return None


def cli_window_spec(title, class_name):
    title = title or ''
    class_name = class_name or ''
    if not title and not class_name:
        return None
    return {'title': title, 'class_name': class_name}


def _same_class(left, right):
    return (left or '').lower() == (right or '').lower()


def _match_window(info, title, class_name, pid):
    if title and info.title != title:
        return False
    if class_name and not _same_class(info.class_name, class_name):
        return False
    if pid and info.pid != int(pid):
        return False
    if not title and not class_name:
        return False
    return True


def resolve_target(api, spec):
    if not spec:
        raise BackgroundAbort(ABORT_NO_TARGET)
    title = (spec.get('title') or '').strip()
    class_name = (spec.get('class_name') or '').strip()
    pid = int(spec.get('pid') or 0)
    hwnd = int(spec.get('hwnd') or 0)
    if not title and not class_name and not hwnd:
        raise BackgroundAbort(ABORT_NO_TARGET)

    if hwnd and api.is_valid(hwnd):
        info = api.info(hwnd)
        if info is not None and _match_window(info, title, class_name, pid):
            return info
        if info is not None and not title and not class_name:
            return info

    def collect(use_pid):
        found = []
        for window in api.enum_top_level():
            if _match_window(window, title, class_name, use_pid):
                found.append(window)
        return found

    matches = collect(pid)
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0 and pid:
        matches = collect(0)
        if len(matches) == 1:
            return matches[0]
    if len(matches) == 0:
        raise BackgroundAbort(ABORT_MISSING)
    raise BackgroundAbort(ABORT_AMBIGUOUS)


def _point_in_client(rect, x, y):
    left, top, right, bottom = rect
    return left <= x < right and top <= y < bottom


def _child_fields(api, hwnd, root):
    level = 'root' if int(hwnd) == int(root) else 'child'
    return {
        'level': level,
        'class_name': api.class_name(hwnd),
        'ctrl_id': int(api.ctrl_id(hwnd)),
    }


def _target_fields(binding):
    return {'title': binding.title, 'class_name': binding.class_name}


def capture_mouse(api, binding, hwnd, screen_x, screen_y, action_type):
    if binding is None:
        return CaptureResult(False, REASON_UNBOUND)
    if action_type not in SUPPORTED_MOUSE:
        return CaptureResult(False, REASON_UNSUPPORTED)
    if not hwnd:
        return CaptureResult(False, REASON_NO_HIT)
    root = int(api.root(hwnd) or 0)
    if root != int(binding.hwnd):
        return CaptureResult(False, REASON_OUTSIDE)
    if api.is_minimized(binding.hwnd):
        return CaptureResult(False, REASON_MINIMIZED)
    client_x, client_y = api.screen_to_client(hwnd, screen_x, screen_y)
    client_x = int(client_x)
    client_y = int(client_y)
    if not _point_in_client(api.client_rect(hwnd), client_x, client_y):
        return CaptureResult(False, REASON_NON_CLIENT)
    child = _child_fields(api, hwnd, root)
    if child['level'] == 'child':
        matches = api.find_children(root, child['class_name'], child['ctrl_id'])
        if len(matches) != 1:
            return CaptureResult(False, REASON_AMBIGUOUS_CHILD)
    fields = {
        'replay': 'background',
        'coord': 'client',
        'action': [client_x, client_y],
        'dpi': int(api.dpi(hwnd) or 0),
        'child': child,
        'target': _target_fields(binding),
    }
    return CaptureResult(True, '', fields)


def capture_key(api, binding, foreground_hwnd, focus_hwnd, action_type, key_action, char):
    if binding is None:
        return CaptureResult(False, REASON_UNBOUND)
    if action_type not in SUPPORTED_KEYS:
        return CaptureResult(False, REASON_UNSUPPORTED)
    if not foreground_hwnd or int(api.root(foreground_hwnd) or 0) != int(binding.hwnd):
        return CaptureResult(False, REASON_NOT_FOREGROUND)
    if not focus_hwnd:
        return CaptureResult(False, REASON_NO_FOCUS)
    focus_root = int(api.root(focus_hwnd) or 0)
    if focus_root != int(binding.hwnd):
        return CaptureResult(False, REASON_FOCUS_OUTSIDE)
    child = _child_fields(api, focus_hwnd, focus_root)
    if child['level'] == 'child':
        matches = api.find_children(focus_root, child['class_name'], child['ctrl_id'])
        if len(matches) != 1:
            return CaptureResult(False, REASON_AMBIGUOUS_CHILD)
    vk, name, extended = key_action
    if vk is None:
        return CaptureResult(False, REASON_UNSUPPORTED)
    fields = {
        'replay': 'background',
        'action': [vk, name, int(extended or 0)],
        'dpi': int(api.dpi(focus_hwnd) or 0),
        'child': child,
        'target': _target_fields(binding),
    }
    if char:
        fields['char'] = char
    return CaptureResult(True, '', fields)


def background_script_document(records):
    payload = {'scripts': records}
    if records and records[0].get('replay') == 'background':
        payload['replay'] = 'background'
        payload['target'] = records[0].get('target')
    return payload


def iter_script_objects(head):
    seen = set()
    stack = [head]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        content = getattr(node, 'content', None) or {}
        if content.get('type') == 'sequence':
            stack.append(content.get('events'))
        stack.append(getattr(node, 'next_object', None))
        stack.append(getattr(node, 'next_object_if_false', None))


def chain_has_background(head):
    for node in iter_script_objects(head):
        content = getattr(node, 'content', None) or {}
        if content.get('type') == 'event' and content.get('replay') == 'background':
            return True
    return False


def script_replay_kind(head):
    meta = getattr(head, 'script_meta', None) or {}
    if meta.get('replay') == 'background' or chain_has_background(head):
        return 'background'
    return 'global'


def script_target(head):
    meta = getattr(head, 'script_meta', None) or {}
    if meta.get('target'):
        return meta.get('target')
    for node in iter_script_objects(head):
        content = getattr(node, 'content', None) or {}
        if content.get('target'):
            return content.get('target')
    return None


def check_stop(stop_check):
    if stop_check and stop_check():
        raise BackgroundAbort(ABORT_CANCELLED)


def _client_point(action):
    if not isinstance(action, (list, tuple)) or len(action) < 2:
        raise BackgroundAbort(ABORT_BAD_COORDS)
    x, y = action[0], action[1]
    if isinstance(x, str) or isinstance(y, str):
        raise BackgroundAbort(ABORT_PERCENT)
    return int(x), int(y)


def _key_lparam(extended, is_up):
    value = 1
    if extended:
        value |= 1 << 24
    if is_up:
        value |= (1 << 30) | (1 << 31)
    return value


class BackgroundSession(object):
    def __init__(self, api, binding, stop_check=None):
        self.api = api
        self.binding = binding
        self.stop_check = stop_check

    def play(self, event):
        check_stop(self.stop_check)
        event_type = getattr(event, 'event_type', None)
        action_type = getattr(event, 'action_type', None)
        if event_type == 'EM':
            self._play_mouse(event, action_type)
        elif event_type == 'EK':
            self._play_key(event, action_type)
        elif event_type == 'EX' and action_type == 'input':
            self._play_text(event)
        else:
            raise BackgroundAbort(ABORT_UNSUPPORTED)

    def _lock_root(self):
        check_stop(self.stop_check)
        hwnd = int(self.binding.hwnd)
        if not self.api.is_valid(hwnd):
            raise BackgroundAbort(ABORT_STALE)
        info = self.api.info(hwnd)
        if info is None:
            raise BackgroundAbort(ABORT_STALE)
        if info.title != self.binding.title or not _same_class(info.class_name, self.binding.class_name):
            raise BackgroundAbort(ABORT_STALE)
        if self.api.is_minimized(hwnd):
            raise BackgroundAbort(ABORT_MINIMIZED)
        return hwnd

    def _destination(self, child):
        root = self._lock_root()
        if not isinstance(child, dict):
            raise BackgroundAbort(ABORT_NO_CHILD)
        level = child.get('level')
        class_name = child.get('class_name') or ''
        ctrl_id = int(child.get('ctrl_id') or 0)
        if level == 'root':
            if not _same_class(self.api.class_name(root), class_name):
                raise BackgroundAbort(ABORT_STALE)
            return root
        if level != 'child':
            raise BackgroundAbort(ABORT_NO_CHILD)
        matches = self.api.find_children(root, class_name, ctrl_id)
        if len(matches) == 0:
            raise BackgroundAbort(ABORT_CHILD_MISSING)
        if len(matches) > 1:
            raise BackgroundAbort(ABORT_CHILD_AMBIGUOUS)
        return int(matches[0])

    def _check_dpi(self, hwnd, recorded):
        current = int(self.api.dpi(hwnd) or 0)
        recorded = int(recorded or 0)
        if recorded and current and recorded != current:
            raise BackgroundAbort(ABORT_DPI)
        if bool(recorded) != bool(current):
            raise BackgroundAbort(ABORT_DPI_UNKNOWN)

    def _post(self, hwnd, msg, wparam, lparam):
        check_stop(self.stop_check)
        if not self.api.post(int(hwnd), int(msg), int(wparam), int(lparam)):
            raise BackgroundAbort(ABORT_POST)

    def _play_mouse(self, event, action_type):
        if action_type not in SUPPORTED_MOUSE:
            raise BackgroundAbort(ABORT_UNSUPPORTED)
        dest = self._destination(getattr(event, 'child', None))
        self._check_dpi(dest, getattr(event, 'dpi', 0))
        x, y = _client_point(getattr(event, 'action', None))
        if action_type == 'mouse move':
            self._post(dest, WM_MOUSEMOVE, 0, pack_xy(x, y))
            return
        if action_type in ('mouse wheel up', 'mouse wheel down'):
            delta = WHEEL_DELTA if action_type == 'mouse wheel up' else -WHEEL_DELTA
            screen_x, screen_y = self.api.client_to_screen(dest, x, y)
            wparam = (delta & 0xFFFF) << 16
            self._post(dest, WM_MOUSEWHEEL, wparam, pack_xy(screen_x, screen_y))
            return
        message, wparam = MOUSE_BUTTONS[action_type]
        self._post(dest, message, wparam, pack_xy(x, y))

    def _play_key(self, event, action_type):
        if action_type not in SUPPORTED_KEYS:
            raise BackgroundAbort(ABORT_UNSUPPORTED)
        dest = self._destination(getattr(event, 'child', None))
        self._check_dpi(dest, getattr(event, 'dpi', 0))
        action = getattr(event, 'action', None)
        if not isinstance(action, (list, tuple)) or len(action) < 1 or action[0] is None:
            raise BackgroundAbort(ABORT_UNSUPPORTED)
        vk = int(action[0])
        extended = int(action[2]) if len(action) > 2 and action[2] else 0
        is_up = action_type == 'key up'
        self._post(dest, WM_KEYUP if is_up else WM_KEYDOWN, vk, _key_lparam(extended, is_up))
        char = getattr(event, 'char', '') or ''
        if action_type == 'key down' and char:
            for unit in utf16_units(char):
                self._post(dest, WM_CHAR, unit, 1)

    def _play_text(self, event):
        dest = self._destination(getattr(event, 'child', None))
        self._check_dpi(dest, getattr(event, 'dpi', 0))
        text = getattr(event, 'action', None)
        if not isinstance(text, str):
            raise BackgroundAbort(ABORT_UNSUPPORTED)
        for unit in utf16_units(text):
            self._post(dest, WM_CHAR, unit, 1)


def prepare_run(runner, head, requested_background, spec, stop_check, api_factory=None):
    kind = script_replay_kind(head)
    decision = decide_run(bool(requested_background), kind)
    reason = decide_abort_reason(decision)
    if reason:
        raise BackgroundAbort(reason)
    if decision == 'global':
        runner.background_session = None
        return 'global'
    target = spec or script_target(head)
    factory = api_factory or get_api
    api = factory()
    info = resolve_target(api, target)
    if api.is_minimized(info.hwnd):
        raise BackgroundAbort(ABORT_MINIMIZED)
    runner.background_session = BackgroundSession(api, info, stop_check)
    return 'background'


def get_api():
    if system() != 'Windows':
        raise BackgroundAbort(ABORT_NOT_WINDOWS)
    from Util.WinApi import WindowsWinApi
    return WindowsWinApi()
