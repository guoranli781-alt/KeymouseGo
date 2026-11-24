import threading
import traceback
from dataclasses import dataclass
from typing import List

from PySide6.QtCore import QThread, Signal, QMutex, QWaitCondition, QDeadlineTimer, Qt, Slot
from PySide6.QtWidgets import QWidget
from loguru import logger

from Util.Global import State
from Event import ScriptEvent
from Plugin.Manager import PluginManager
from Util.Parser import LegacyParser, ScriptParser, JsonObject
from Util.WindowBind import BackgroundAbort, prepare_run

mutex = QMutex()
cond = QWaitCondition()


def _remember_abort(runner, exc):
    runner._abort_reason = exc.reason
    runner._abort_status = getattr(exc, 'status', None)


class RunScriptMeta:
    def pause(self):
        mutex.lock()
        cond.wait(mutex)
        mutex.unlock()

    def sleep(self, msecs: int):
        mutex.lock()
        cond.wait(mutex, QDeadlineTimer(int(msecs)))
        mutex.unlock()

    def resume(self):
        mutex.lock()
        cond.wakeAll()
        mutex.unlock()


class RunScriptClass(QThread, RunScriptMeta):
    logSignal: Signal = Signal(str)
    tnumrdSignal: Signal = Signal(str)
    btnSignal: Signal = Signal(bool)
    statusSignal: Signal = Signal(bool)
    playtuneSignal: Signal = Signal(str)

    def __init__(self, frame: QWidget):
        super().__init__()
        logger.debug('Thread created at thread' + str(threading.currentThread()))
        self.eventPause = False
        self.state = State.RUNNING
        self.script_path = frame.get_script_path()
        self.runtimes = frame.stimes.value()

        # 更新控件的槽函数
        self.logSignal.connect(frame.textlog.append)
        self.tnumrdSignal.connect(frame.tnumrd.setText)
        self.btnSignal.connect(frame.btrun.setEnabled)
        self.btnSignal.connect(frame.btrecord.setEnabled)
        frame.updateStateSignal.connect(self.update_state, Qt.DirectConnection)
        self.statusSignal.connect(frame.handle_runscript_status)
        self.playtuneSignal.connect(frame.playtune)
        self._abort_reason = None
        self._abort_status = None
        self.background_session = None
        self.background_requested = bool(getattr(frame, 'background_mode', False))
        self.background_spec = None
        if hasattr(frame, 'background_override_spec'):
            self.background_spec = frame.background_override_spec()

    def sleep(self, msecs: int):
        RunScriptMeta.sleep(self, msecs)

    def resume(self):
        self.eventPause = False
        super().resume()

    def set_pause(self):
        self.eventPause = True

    @Slot(State)
    def update_state(self, state):
        self.state = state

    def wait_if_pause(self):
        if self.eventPause:
            self.pause()
        else:
            self.resume()

    def run(self):
        logger.debug('Run script at thread' + str(QThread.currentThread()))

        if not self.script_path:
            self.tnumrdSignal.emit('script not found, please record first!')
            logger.warning('Script not found, please record first!')
            return

        self.btnSignal.emit(False)
        self.playtuneSignal.emit('start.wav')
        try:
            self.run_script_from_path(self.script_path)
        except BackgroundAbort as exc:
            self.logSignal.emit(exc.reason)
            self.tnumrdSignal.emit(exc.status or 'stopped')
            logger.info(exc.reason)
        except Exception as e:
            logger.error(e)
            self.logSignal.emit('An error occurred during execution! Please check logs!')
        self.statusSignal.emit(True)
        self.playtuneSignal.emit('end.wav')

    @logger.catch
    def run_script_from_path(self, script_path: str):
        try:
            running_text = '%s running..' % script_path.split('/')[-1].split('\\')[-1]
            self.tnumrdSignal.emit(running_text)
            logger.info('%s running..' % script_path.split('/')[-1].split('\\')[-1])

            # 解析脚本，返回事件集合与扩展类对象
            logger.debug('Parse script..')
            head_object = None
            try:
                head_object = ScriptParser.parse(script_path)
            except Exception as e:
                logger.warning('Failed to parse script, maybe it is using legacy grammar')
                logger.debug(e)
            if head_object is None:
                try:
                    head_object = LegacyParser.parse(script_path)
                except Exception as e:
                    logger.error(e)
                    self.logSignal.emit('==============\nAn error occurred while parsing script')
                    self.logSignal.emit(str(e))
                    self.logSignal.emit('==============')
            if head_object is None:
                self.logSignal.emit('无法解析脚本，已停止')
                self.tnumrdSignal.emit('stopped')
                return
            self._abort_reason = None
            self._abort_status = None
            try:
                prepare_run(self, head_object, self.background_requested,
                            self.background_spec, lambda: self.state == State.IDLE)
            except BackgroundAbort as exc:
                self.logSignal.emit(exc.reason)
                self.tnumrdSignal.emit(exc.status or 'stopped')
                logger.info(exc.reason)
                return

            j = 0
            nointerrupt = True
            logger.debug('Run script..')

            while (j < self.runtimes or self.runtimes == 0) and nointerrupt:
                logger.debug('===========%d==============' % j)
                if self.state == State.IDLE:
                    break
                self.tnumrdSignal.emit(f'{running_text}... Looptimes [{j + 1}/{self.runtimes}]')
                nointerrupt = nointerrupt and self.run_script_from_objects(head_object)
                if self._abort_reason:
                    self.logSignal.emit(self._abort_reason)
                    self.tnumrdSignal.emit(self._abort_status or 'stopped')
                    logger.info(self._abort_reason)
                    return
                j += 1
            if nointerrupt:
                self.tnumrdSignal.emit('finished')
                logger.info('Script run finish')
            else:
                logger.info('Script run interrupted')

        except Exception as e:
            logger.error('Run error: {0}'.format(e))
            traceback.print_exc()
            self.logSignal.emit('==============\nAn error occurred during runtime')
            self.logSignal.emit(str(e))
            self.logSignal.emit('==============')
            self.logSignal.emit('script run failed, please check your log file')
        finally:
            self.btnSignal.emit(True)

    # 执行集合中的ScriptEvent
    @logger.catch
    def run_script_from_objects(self, head_object: JsonObject, attach: List[str] = None):
        current_object = head_object
        while current_object is not None:
            self.wait_if_pause()
            if self.state == State.IDLE:
                return False
            if attach:
                try:
                    PluginManager.call_group(attach, current_object)
                except Exception as e:
                    logger.error(e)
                    self.logSignal.emit(f'An error occurred while calling {attach}, please check log file')
                    self.logSignal.emit(f'调用{attach}时发生错误，请检查程序日志')
            current_object = self.run_object(current_object)
            if self._abort_reason:
                return False
        return True

    # Only return next object when 'goto' is indicated
    @logger.catch
    def run_object(self, json_object: JsonObject):
        object_type: str = json_object.content.get('type', None)
        call_group: List[str] = json_object.content.get('call', None)
        if call_group:
            PluginManager.call_group(call_group, json_object)
        if object_type == 'event':
            event = ScriptEvent(json_object.content)
            self.logSignal.emit(str(event))
            logger.debug(str(event))
            try:
                event.execute(self)
            except BackgroundAbort as exc:
                _remember_abort(self, exc)
                return None
        elif object_type == 'sequence':
            self.run_script_from_objects(json_object.content['events'], json_object.content['attach'])
        elif object_type == 'if':
            result = PluginManager.call(json_object.content['judge'], json_object)
            if self._abort_reason:
                return None
            if result:
                return json_object.next_object
            else:
                return json_object.next_object_if_false
        elif object_type == 'goto' or object_type == 'custom':
            pass
        elif object_type == 'subroutine':
            for path in json_object.content['path']:
                self.run_script_from_path(path)
        else:
            # Not supposed to happen
            logger.error(f'Unexpected event type when running {json_object.content}')
        if self._abort_reason:
            return None
        return json_object.next_object


@dataclass
class StopFlag:
    value: bool


class RunScriptCMDClass(QThread, RunScriptMeta):
    def __init__(self, script_path: str, run_times: int, flag: StopFlag,
                 background=False, window_spec=None):
        super().__init__()
        self.script_path = script_path
        self.run_times = run_times
        self.flag = flag
        self.background_requested = bool(background)
        self.background_spec = window_spec
        self._abort_reason = None
        self._abort_status = None
        self.background_session = None

    def sleep(self, msecs: int):
        RunScriptMeta.sleep(self, msecs)

    def run(self) -> None:
        self.run_script_from_path(self.script_path)

    @logger.catch
    def run_script_from_path(self, script_path):
        for path in script_path:
            logger.info('Script path:%s' % path)
            logger.debug('Parse script..')
            head_object = None
            try:
                head_object = ScriptParser.parse(path)
            except Exception as e:
                logger.warning('Failed to parse script, maybe it is using legacy grammar')
                logger.debug(e)
            if head_object is None:
                try:
                    head_object = LegacyParser.parse(path)
                except Exception as e:
                    logger.error(e)
            if head_object is None:
                logger.error('无法解析脚本，已停止')
                self.flag.value = True
                break
            self._abort_reason = None
            self._abort_status = None
            try:
                prepare_run(self, head_object, self.background_requested,
                            self.background_spec, lambda: self.flag.value)
            except BackgroundAbort as exc:
                logger.info(exc.reason)
                self.flag.value = True
                break

            j = 0
            while j < self.run_times or self.run_times == 0:
                logger.info('===========%d==============' % j)
                self.run_script_from_objects(head_object)
                if self._abort_reason:
                    logger.info(self._abort_reason)
                    self.flag.value = True
                    break
                if self.flag.value:
                    logger.info('Stop Running thread')
                    break
                j += 1
            if self.flag.value or self._abort_reason:
                break
            logger.info('%s run finish' % path)

    @logger.catch
    def run_script_from_objects(self, head_object: JsonObject, attach: List[str] = None):
        current_object = head_object
        while current_object is not None:
            if self.flag.value:
                break
            if attach:
                PluginManager.call_group(attach, current_object)
            current_object = self.run_object(current_object)
            if self._abort_reason:
                return False

    @logger.catch
    def run_object(self, json_object: JsonObject):
        object_type: str = json_object.content.get('type', None)
        call_group: List[str] = json_object.content.get('call', None)
        if call_group:
            PluginManager.call_group(call_group, json_object)
        if object_type == 'event':
            event = ScriptEvent(json_object.content)
            logger.debug(str(event))
            try:
                event.execute(self)
            except BackgroundAbort as exc:
                _remember_abort(self, exc)
                return None
        elif object_type == 'sequence':
            self.run_script_from_objects(json_object.content['events'], json_object.content['attach'])
        elif object_type == 'if':
            result = PluginManager.call(json_object.content['judge'], json_object)
            if self._abort_reason:
                return None
            if result:
                return json_object.next_object
            else:
                return json_object.next_object_if_false
        elif object_type in ['goto', 'custom']:
            pass
        elif object_type == 'subroutine':
            self.run_script_from_path(json_object.content['path'])
        else:
            # Not supposed to happen
            logger.error(f'Unexpected event type when running {json_object.content}')
        if self._abort_reason:
            return None
        return json_object.next_object

