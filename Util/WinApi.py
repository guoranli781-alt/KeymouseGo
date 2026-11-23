"""真实 Win32 查询与 PostMessage。

只读窗口信息，并向指定 HWND 投递窗口消息。
不改变前台，不把输入送进系统输入流，也不改剪贴板。
"""

import ctypes
import os
from ctypes import wintypes

from Util.WindowBind import WindowInfo

GA_ROOT = 2


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

    def post(self, hwnd, msg, wparam, lparam):
        try:
            return bool(self.win32gui.PostMessage(int(hwnd), int(msg), int(wparam), int(lparam)))
        except Exception:
            return False

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
