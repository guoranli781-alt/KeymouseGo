"""真实 Win32 查询与 PostMessage。

只读窗口信息，并向指定 HWND 投递窗口消息。
不改变前台，不把输入送进系统输入流，也不改剪贴板。
"""

import ctypes
import os
from ctypes import wintypes

from Util.WindowBind import WindowInfo

GA_ROOT = 2
MAPVK_VK_TO_VSC = 0


class DeliveryError(object):
    """操作系统拒绝把消息放入队列。winerror 来自 API 包装当场记下的错误，不是事后再读的 GetLastError。"""

    def __init__(self, winerror, message, funcname='PostMessage'):
        self.winerror = winerror
        self.message = message or ''
        self.funcname = funcname or 'PostMessage'


def to_wparam(value):
    """WPARAM 按无符号 32 位保留。滚轮高字为负数时高位必须还在。"""
    return int(value) & 0xFFFFFFFF


def to_lparam(value):
    """压成 Win32 MAKELPARAM / 键 lParam 的有符号 32 位。

    高位为 1 时用负数交给 pywin32，64 位 LPARAM 才会按符号扩展，
    低 32 位与 GET_X_LPARAM / GET_Y_LPARAM 一致。
    """
    value = int(value) & 0xFFFFFFFF
    if value >= 0x80000000:
        return value - 0x100000000
    return value


def _is_pywin32_api_error(exc):
    return (
        type(exc).__name__ == 'error'
        and hasattr(exc, 'winerror')
        and hasattr(exc, 'strerror')
    )


def classify_post_outcome(result, api_error=None, captured_last_error=None):
    """区分「系统已接受消息」和「系统拒绝投递」。

    pywin32 的 win32gui.PostMessage / win32api.PostMessage 是 BOOLAPI：
    成功返回 None，失败抛 pywintypes.error，错误码在异常对象里。
    成功时不能再读 GetLastError，那个值是过期的。

    ctypes 的 PostMessageW 在设置了指针宽度参数和 use_last_error 之后：
    非 0 为成功；0 为失败，错误码只能用调用当时记下的 captured_last_error。
    """
    if api_error is not None:
        winerror = getattr(api_error, 'winerror', None)
        if isinstance(winerror, bool) or not isinstance(winerror, int):
            winerror = None
        message = getattr(api_error, 'strerror', None) or str(api_error)
        funcname = getattr(api_error, 'funcname', None) or 'PostMessage'
        return DeliveryError(winerror, ' '.join(str(message).split()), funcname)
    if result is None or result is True:
        return None
    if isinstance(result, int) and not isinstance(result, bool) and result != 0:
        return None
    code = None
    if captured_last_error is not None and not isinstance(captured_last_error, bool):
        try:
            code = int(captured_last_error)
        except (TypeError, ValueError):
            code = None
    text = '' if code is not None else 'PostMessage 返回 0'
    return DeliveryError(code, text, 'PostMessage')


def apply_postmessage_prototype(post_message):
    """HWND / WPARAM / LPARAM 都是指针宽度。Windows x64 上 c_long 只有 32 位，不能用来传句柄。"""
    post_message.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    post_message.restype = ctypes.c_int
    return post_message


class _RECT(ctypes.Structure):
    _fields_ = [
        ('left', ctypes.c_long),
        ('top', ctypes.c_long),
        ('right', ctypes.c_long),
        ('bottom', ctypes.c_long),
    ]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', ctypes.c_ulong),
        ('flags', ctypes.c_ulong),
        ('hwndActive', ctypes.c_void_p),
        ('hwndFocus', ctypes.c_void_p),
        ('hwndCapture', ctypes.c_void_p),
        ('hwndMenuOwner', ctypes.c_void_p),
        ('hwndMoveSize', ctypes.c_void_p),
        ('hwndCaret', ctypes.c_void_p),
        ('rcCaret', _RECT),
    ]


class WindowsWinApi(object):
    def __init__(self):
        import win32gui
        import win32process
        self.win32gui = win32gui
        self.win32process = win32process
        self.user32 = ctypes.windll.user32
        self._my_pid = os.getpid()
        self.last_post_failure = None

    def is_valid(self, hwnd):
        try:
            return bool(hwnd) and bool(self.win32gui.IsWindow(int(hwnd)))
        except Exception:
            return False

    def info(self, hwnd):
        if not self.is_valid(hwnd):
            return None
        try:
            title = self.win32gui.GetWindowText(int(hwnd)) or ''
            class_name = self.win32gui.GetClassName(int(hwnd)) or ''
            _tid, pid = self.win32process.GetWindowThreadProcessId(int(hwnd))
            return WindowInfo(int(hwnd), title, class_name, int(pid))
        except Exception:
            return None

    def enum_top_level(self):
        found = []

        def _cb(hwnd, _extra):
            try:
                if not self.win32gui.IsWindowVisible(hwnd):
                    return True
                title = self.win32gui.GetWindowText(hwnd) or ''
                if not title.strip():
                    return True
                class_name = self.win32gui.GetClassName(hwnd) or ''
                _tid, pid = self.win32process.GetWindowThreadProcessId(hwnd)
                if int(pid) == self._my_pid:
                    return True
                found.append(WindowInfo(int(hwnd), title, class_name, int(pid)))
            except Exception:
                return True
            return True

        self.win32gui.EnumWindows(_cb, None)
        return found

    def is_minimized(self, hwnd):
        try:
            return bool(self.win32gui.IsIconic(int(hwnd)))
        except Exception:
            return False

    def root(self, hwnd):
        if not hwnd:
            return 0
        try:
            ancestor = self.win32gui.GetAncestor(int(hwnd), GA_ROOT)
            return int(ancestor or hwnd)
        except Exception:
            return int(hwnd)

    def class_name(self, hwnd):
        try:
            return self.win32gui.GetClassName(int(hwnd)) or ''
        except Exception:
            return ''

    def ctrl_id(self, hwnd):
        try:
            return int(self.win32gui.GetDlgCtrlID(int(hwnd)))
        except Exception:
            return 0

    def client_rect(self, hwnd):
        try:
            left, top, right, bottom = self.win32gui.GetClientRect(int(hwnd))
            return (int(left), int(top), int(right), int(bottom))
        except Exception:
            return (0, 0, 0, 0)

    def screen_to_client(self, hwnd, x, y):
        try:
            cx, cy = self.win32gui.ScreenToClient(int(hwnd), (int(x), int(y)))
            return int(cx), int(cy)
        except Exception:
            return 0, 0

    def client_to_screen(self, hwnd, x, y):
        try:
            sx, sy = self.win32gui.ClientToScreen(int(hwnd), (int(x), int(y)))
            return int(sx), int(sy)
        except Exception:
            return int(x), int(y)

    def dpi(self, hwnd):
        try:
            fn = self.user32.GetDpiForWindow
        except AttributeError:
            return 0
        try:
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_uint
            return int(fn(int(hwnd)) or 0)
        except Exception:
            return 0

    def find_children(self, root, class_name, ctrl_id):
        wanted = (class_name or '').lower()
        ctrl_id = int(ctrl_id)
        found = []

        def _cb(hwnd, _extra):
            try:
                name = (self.win32gui.GetClassName(hwnd) or '').lower()
                if name == wanted and int(self.win32gui.GetDlgCtrlID(hwnd)) == ctrl_id:
                    found.append(int(hwnd))
            except Exception:
                return True
            return True

        try:
            self.win32gui.EnumChildWindows(int(root), _cb, None)
        except Exception:
            return []
        return found

    def scan_code(self, vk):
        """MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) 的低 8 位。取不到时返回 0，不中断回放。"""
        try:
            fn = self.user32.MapVirtualKeyW
            fn.argtypes = [ctypes.c_uint, ctypes.c_uint]
            fn.restype = ctypes.c_uint
            return int(fn(int(vk) & 0xFFFFFFFF, MAPVK_VK_TO_VSC) or 0) & 0xFF
        except Exception:
            return 0

    def post(self, hwnd, msg, wparam, lparam):
        """把一条消息放入目标队列。成功返回 True。

        pywin32 成功时返回 None，不是 True。None 表示系统已经接受，
        不能当成失败，也不能再用 GetLastError 去解释它。
        游戏忽略已经入队的消息不会走到这里。
        """
        self.last_post_failure = None
        try:
            result = self.win32gui.PostMessage(
                int(hwnd), int(msg), to_wparam(wparam), to_lparam(lparam))
        except Exception as exc:
            if _is_pywin32_api_error(exc):
                failure = classify_post_outcome(None, api_error=exc)
            else:
                failure = DeliveryError(
                    None,
                    '{0}: {1}'.format(type(exc).__name__, exc),
                    'PostMessage')
            self.last_post_failure = failure
            return False
        failure = classify_post_outcome(result)
        self.last_post_failure = failure
        return failure is None

    def foreground(self):
        try:
            return int(self.win32gui.GetForegroundWindow() or 0)
        except Exception:
            return 0

    def focus_hwnd(self):
        try:
            fg = self.foreground()
            if not fg:
                return 0
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
            self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            tid = self.user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            self.user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
            self.user32.GetGUIThreadInfo.restype = wintypes.BOOL
            if not self.user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                return 0
            return int(info.hwndFocus or 0)
        except Exception:
            return 0

    def window_from_point(self, x, y):
        try:
            return int(self.win32gui.WindowFromPoint((int(x), int(y))) or 0)
        except Exception:
            return 0
