# Copyright (c) 2019 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.
import functools
import json
import logging
import os
import threading

import sgtk
from sgtk.platform.qt import QtCore
from tank_vendor import six

from .rpc import Communicator

logger = logging.getLogger(__name__)


###############################################################################
# functions


def timeout(seconds=5.0, error_message="Timed out."):
    """
    A timeout decorator. When the given amount of time has passed
    after the decorated callable is called, if it has not completed
    an RPCTimeoutError is raised.

    :param float seconds: The timeout duration, in seconds.
    :param str error_message: The error message to raise once timed out.
    """

    def decorator(func):
        def _handle_timeout():
            raise RPCTimeoutError(error_message)

        def wrapper(*args, **kwargs):
            timer = threading.Timer(float(seconds), _handle_timeout)
            try:
                timer.start()
                result = func(*args, **kwargs)
            finally:
                timer.cancel()
            return result

        return functools.wraps(func)(wrapper)

    return decorator


##########################################################################################
# classes


class MessageEmitter(QtCore.QObject):
    """
    Container QObject for Qt signals fired when messages requesting certain
    actions take place in Python arrive from the remote process.

    :signal logging_received(str, str): Fires when a logging call has been
        received. The first string is the logging level (debug, info, warning,
        or error) and the second string is the message.
    :signal command_received(int): Fires when an engine command has been
        received. The integer value is the unique id of the engine command
        that was requested to be executed.
    :signal run_tests_request_received: Fires when a request for unit tests to
        be run has been received.
    :signal state_requested: Fires when the remote process requests the current
        state.
    :signal active_document_changed(str): Fires when alerted to a change in active
        document by the RPC server. The string value is the path to the new
        active document, or an empty string if the active document is unsaved.
    """

    logging_received = QtCore.Signal(str, str)
    command_received = QtCore.Signal(int)
    run_tests_request_received = QtCore.Signal()
    state_requested = QtCore.Signal()
    active_document_changed = QtCore.Signal(str)
    gdn_hwnd = QtCore.Signal(int)


class GDNBridge(Communicator):
    """
    Bridge layer between the Adobe product and Shotgun Toolkit.
    """

    # Backwards compatibility added to support tk-photoshop environment vars.
    # https://community.shotgridsoftware.com/t/adobe-engine-crashing-on-long-operations/8329
    SHOTGUN_GDN_RESPONSE_TIMEOUT = os.environ.get(
        "SHOTGUN_GDN_RESPONSE_TIMEOUT", 300.0)
    SHOTGUN_GDN_HEARTBEAT_TIMEOUT = os.environ.get(
        "SHOTGUN_GDN_HEARTBEAT_TIMEOUT", 0.5)

    def __init__(self, *args, **kwargs):
        super(GDNBridge, self).__init__(*args, **kwargs)

        self.logger.debug(
            "SHOTGUN_GDN_RESPONSE_TIMEOUT "
            "is %s" % self.SHOTGUN_GDN_RESPONSE_TIMEOUT
        )

        self.logger.debug(
            "SHOTGUN_GDN_HEARTBEAT_TIMEOUT "
            "is %s" % self.SHOTGUN_GDN_HEARTBEAT_TIMEOUT
        )

        self._emitter = MessageEmitter()
        self.logger.debug("Setting up event connections")
        self._io.on("logging", self._forward_logging)
        self._io.on("command", self._forward_command)
        self._io.on("run_tests", self._forward_run_tests)
        self._io.on("state_requested", self._forward_state_request)
        self._io.on("active_document_changed", self._forward_active_document_changed)
        self._io.on("gdn_hwnd", self._forward_gdn_hwnd)

    ##########################################################################################
    # properties

    @property
    def active_document_changed(self):
        """
        The signal that is emitted when notification of an active document
        change arrives via RPC.
        """
        return self._emitter.active_document_changed

    @property
    def logging_received(self):
        """
        The signal that is emitted when a logging message has arrived
        via RPC.
        """
        return self._emitter.logging_received

    @property
    def command_received(self):
        """
        The signal that is emitted when a command message has arrived
        via RPC.
        """
        return self._emitter.command_received

    @property
    def hwnd_changed(self):
        """
        The signal that is emitted when a command message has arrived
        via RPC.
        """
        return self._emitter.gdn_hwnd

    @property
    def run_tests_request_received(self):
        """
        The signal that is emitted when a run_tests message has arrived
        via RPC.
        """
        return self._emitter.run_tests_request_received

    @property
    def state_requested(self):
        """
        The QSignal that is emitted when the state is requested via RPC.
        """

        return self._emitter.state_requested

    ##########################################################################################
    # public methods

    @timeout(SHOTGUN_GDN_HEARTBEAT_TIMEOUT, "Ping timed out.")
    def ping(self):
        """
        Pings the socket.io server to test whether the connection is still
        active.
        """
        super(GDNBridge, self).ping()

    def get_active_document(self):
        """
        Gets the active document in the current session.

        :returns: The active document, or None.
        """
        with self.response_logging_silenced():
            try:
                doc = self.app.activeDocument
            except Exception:
                logger.warning("Failed to get Active Document from GDN")
                doc = None

        return doc

    def get_active_document_path(self):
        """
        Gets the path to the currently-active document. This will do so
        without raising a RuntimeError if the active document is a "new"
        document that has not been saved. In that case, a None will be
        returned instead.

        :returns: The active document's file path on disk as a str, or
                  None if the document has never been saved.
        """
        doc = self.get_active_document()

        if not doc:
            return None

        with self.response_logging_silenced():
            try:
                path = doc.fullName.fsName
            except Exception:
                path = None

            if path is not None:
                path = six.ensure_str(path)

        return path

    def log_message(self, level, msg):
        """
        Log a message from python so that it is visible on js side.

        :param level: The js log level name.
        :param msg: The message to log.
        """

        log_data = {"level": level, "msg": msg}

        # NOTE: do not log in this method
        json_log_data = json.dumps(log_data)
        self._io.emit("log_message", json_log_data)

    def send_commands(self, commands):
        """
        Responsible for forwarding the current engine commands to js.

        This method knows about the structure of the json that the js side
        expects. We provide display info and we also
        """
        # encode the python dict as json
        json_commands = json.dumps(commands)
        self.logger.debug("Sending commands: %s" % json_commands)
        self._io.emit("set_commands", json_commands)

    def send_context_display(self, context_display):
        """
        Responsible for forwarding the current engine context display to js.

        This method knows about the structure of the json that the js side
        expects. We provide display info and we also
        """
        # encode the python dict as json
        json_context_display = json.dumps(context_display)
        self.logger.debug("Sending context display.")
        self._io.emit("set_context_display", json_context_display)

    def send_context_thumbnail(self, context_thumbnail):
        """
        Responsible for forwarding the current engine context thumb path to js.

        This method knows about the structure of the json that the js side
        expects. We provide display info and we also
        """
        # encode the python dict as json
        json_context_thumbnail = json.dumps(context_thumbnail)
        self.logger.debug("Sending context thumb path: %s" % json_context_thumbnail)
        self._io.emit("set_context_thumbnail", json_context_thumbnail)

    def send_log_file_path(self, log_file):
        """
        Responsible for forwarding the current log file path to js.

        The path is displayed in errors to help facilitate getting the log to
        support teams when problems occur.
        """
        json_file_path = json.dumps(log_file)
        self.logger.debug("Sending log file path: %s" % json_file_path)
        self._io.emit("set_log_file_path", json_file_path)

    def send_unknown_context(self):
        """
        Sent when a context can not be determined for the current file.
        """
        self.logger.debug("Alerting js that there is no context")
        self._io.emit("set_unknown_context")

    def context_about_to_change(self):
        """
        Sent just before the context is about to change.
        """
        self.logger.debug("Sending context about to change message.")
        self._io.emit("context_about_to_change")

    def save_as(self, doc, file_path):
        """
        Performs a save-as operation on the given document, saving to the
        given file path. The purpose of this method is to abstract away the
        additional processing required to save a .psb file, as compared to
        a more-typical .psd file save-as.

        :param doc: The document to be saved.
        :param str file_path: The destination file path.
        """
        # TODO doc.saveAs(self.File(file_path))

    ##########################################################################################
    # internal methods

    def _forward_active_document_changed(self, response):
        """
        Forwards the notification that the host application's active document
        has changed.

        :param response: The data received with the message. This
                         is disregarded.
        """
        self.logger.debug("Emitting active_document_changed signal.")
        response = sgtk.util.json.loads(response)
        self.active_document_changed.emit(response.get("active_document_path"))

    def _forward_gdn_hwnd(self, response):
        """
        Forwards the notification that the host application's the main gdn frame hwnd.

        :param response: The data received with the message. This
                         is disregarded.
        """
        self.logger.debug("Emitting gdn_hwnd_changed signal.")
        response = sgtk.util.json.loads(response)
        self.hwnd_changed.emit(int(response))

    def _forward_command(self, response):
        """
        Forwards the received command on as a Qt Signal.

        :param response: The data received with the message. This
                         will take the form of a JSON encoded integeter
                         that is the unique id of the command to be called.
        """
        self.logger.debug("Emitting command_received signal.")
        self.command_received.emit(int(sgtk.util.json.loads(response)))

    def _forward_logging(self, response):
        """
        Forwards the logging request received as a Qt Signal.

        :param response: The data received with the message. This will
                         take the form of a JSON encoded dictionary with
                         "level" and "message" keys containing the severity
                         level of the logging message, and the message itself,
                         respectively.
        """
        response = sgtk.util.json.loads(response)
        self.logger.debug("Got logging command - forwarding to Qt %s" % response)
        self.logging_received.emit(
            response.get("level"),
            response.get("message"),
        )

    def _forward_run_tests(self, response):
        """
        Forwards the request for tests to be run as a Qt Signal.

        :param response: The data received with the message. This
                         is disregarded.
        """
        self.logger.debug("Emitting run_tests_request_received signal.")
        self.run_tests_request_received.emit()

    def _forward_state_request(self, response):
        """
        Forwards the request for state as a QtSignal.

        :param response: The data received with the message. This
                         is disregarded.
        """
        self.logger.debug("Emitting state_requested signal.")
        self.state_requested.emit()

    @timeout(SHOTGUN_GDN_RESPONSE_TIMEOUT, "Timed out waiting for response.")
    def _wait_for_response(self, uid):
        """
        Waits for the results of an RPC call. A timeout is attached to this
        operation equal to the number of seconds defined in the
        SHOTGUN_GDN_RESPONSE_TIMEOUT environment variable, or 300 seconds
        if that is not defined.

        :param int uid: The unique id of the RPC call to wait for.

        :returns: The raw returned results data.
        """
        return super(GDNBridge, self)._wait_for_response(uid)


##########################################################################################
# exceptions


class RPCTimeoutError(Exception):
    """
    Raised when an RPC event times out.
    """

    pass
