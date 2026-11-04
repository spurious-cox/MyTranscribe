#!/usr/bin/env python3
# Copyright (c) 2026 Tim McCoy. MIT License — see LICENSE.
"""
mytranscribe.py
---------------
Small GUI that listens to whatever this Mac is playing (system audio, via
ScreenCaptureKit) and transcribes it with OpenAI Whisper.

Click Capture to start; text appears in chunks while it listens. Click it
again (Stop) to finish. Needs the Screen Recording permission the first time.

Floating panel: a small window that stays in front of other apps and never
takes focus from them. Its "Type at cursor" switch types each chunk of text as
keystrokes wherever the cursor is in the frontmost app (needs Accessibility).
The clipboard is not used. Saying "new line" starts a new line and "new
paragraph" leaves a blank line; they are typed as Shift-Return, which chat
boxes take as a line break rather than Send.

MyTranscribe.app is self-contained: py2app bundles Python 3.12, PyObjC, torch,
whisper and the models. Audio goes to Whisper as a float32 array, so ffmpeg
is not needed.

Version  : 2.8.0
Date     : November 2026

Usage:
    mytranscribe.py              open the window
    mytranscribe.py --selftest   capture 5 s of system audio, print level
"""

import faulthandler
import os
import queue
import re
import sys
import threading
import time

import numpy as np
import objc
import CoreMedia
import Quartz
import ScreenCaptureKit as SCK
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSColor, NSFontAttributeName,
    NSForegroundColorAttributeName, NSMenu, NSMenuItem, NSBezelBorder, NSBezelStyleRounded,
    NSButton, NSButtonTypeSwitch, NSFont, NSPasteboard, NSPasteboardTypeString, NSPopUpButton,
    NSProgressIndicator, NSSavePanel, NSView, NSLineBreakByWordWrapping, NSScrollView, NSTabView, NSTabViewItem, NSTextField, NSWorkspace, NSTextView,
    NSViewHeightSizable, NSViewMinXMargin, NSViewMinYMargin, NSViewWidthSizable,
    NSImage, NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled, NSPanel,
    NSWindowStyleMaskNonactivatingPanel, NSWindowStyleMaskUtilityWindow,
    NSFloatingWindowLevel, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary)
from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
from Foundation import NSURL, NSUserDefaults, NSAttributedString, NSBundle, NSMakeRange, NSMakeRect, NSMakeSize, NSObject, NSTimer
from libdispatch import dispatch_queue_create

VERSION = "2.8.0"
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
LOG = os.path.expanduser("~/Library/Logs/MyTranscribe.log")
RATE = 16000                 # Whisper's native rate
CHUNK_SECONDS = 10           # transcribe every N seconds of audio
SILENCE_RMS = 0.002          # below this a chunk is treated as silence
HALLU_COMPRESSION = 2.4      # segments more repetitive than this are dropped
HALLU_NO_SPEECH = 0.6        # with avg_logprob below HALLU_LOGPROB: silence
HALLU_LOGPROB = -1.0
SPK_MIN_SECONDS = 1.0        # shorter segments inherit the previous speaker
SPK_THRESHOLD = 0.28         # cosine similarity needed to join a speaker
SPK_MAX = 8
HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIRS = [os.path.join(HERE, "models"),
              os.path.join(os.path.dirname(HERE), "Resources", "models")]
FRAME_NAME = "MyTranscribeMainWindow"
PANEL_FRAME = "MyTranscribePanel"
MAC, MIC = "Mac", "Mic"                     # the two sources
SOURCES = [MAC, MIC]                       # order of the Mac/Mic switches
OLD_TABS = {"Mac Audio": MAC, "My Voice": MIC}   # names before 2.6.0
MODELS = ["tiny.en", "base.en", "small.en", "base", "small"]
DEFAULT_MODEL = "base.en"


def logline(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {msg}"
    if "--auto" in sys.argv:
        print(line, flush=True)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class AudioTap(NSObject):
    """SCStream output + delegate: collects mono float32 audio at RATE."""

    def init(self):
        self = objc.super(AudioTap, self).init()
        if self is None:
            return None
        self.lock = threading.Lock()
        self.chunks = []
        self.error = None
        self.stream = None
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
        if kind != 1:                       # 1 = SCStreamOutputTypeAudio
            return
        try:
            block = CoreMedia.CMSampleBufferGetDataBuffer(sbuf)
            n = CoreMedia.CMBlockBufferGetDataLength(block)
            status, data = CoreMedia.CMBlockBufferCopyDataBytes(
                block, 0, n, bytearray(n))
            if status != 0:
                return
            arr = np.frombuffer(bytes(data), dtype=np.float32).copy()
            with self.lock:
                self.chunks.append(arr)
        except Exception as exc:            # keep capturing; surface once
            self.error = f"audio decode: {exc}"

    def stream_didStopWithError_(self, stream, err):
        if err is not None:
            self.error = str(err)

    def take(self):
        with self.lock:
            if not self.chunks:
                return np.zeros(0, dtype=np.float32)
            out = np.concatenate(self.chunks)
            self.chunks = []
        return out


def start_capture(tap, timeout=10):
    """Start capturing system audio into `tap`. Returns an error string or None."""
    if not Quartz.CGPreflightScreenCaptureAccess():
        Quartz.CGRequestScreenCaptureAccess()      # registers us in the list
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security"
            "?Privacy_ScreenCapture"))
        return ("Turn on 'MyTranscribe' in Screen Recording (opened for you), "
                "quit MyTranscribe, then open it again.")
    done = threading.Event()
    result = {}

    def got_content(content, err):
        result["content"], result["err"] = content, err
        done.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
    if not done.wait(timeout):
        return "Timed out asking macOS for the display list."
    if result.get("err") is not None or not result.get("content"):
        return ("Screen Recording permission is needed. Allow it in System "
                "Settings > Privacy & Security > Screen & System Audio "
                f"Recording, then try again. ({result.get('err')})")
    displays = result["content"].displays()
    if not displays:
        return "No display found to attach the audio capture to."

    flt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
        displays[0], [])
    cfg = SCK.SCStreamConfiguration.alloc().init()
    cfg.setCapturesAudio_(True)
    cfg.setExcludesCurrentProcessAudio_(False)
    cfg.setSampleRate_(RATE)
    cfg.setChannelCount_(1)
    cfg.setWidth_(2)                        # video is not wanted; keep it tiny
    cfg.setHeight_(2)
    cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 1))

    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
        flt, cfg, tap)
    q = dispatch_queue_create(b"mytranscribe.audio", None)
    ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(
        tap, 1, q, None)
    if not ok:
        return f"Could not attach audio output: {err}"

    started = threading.Event()
    sres = {}

    def after_start(err):
        sres["err"] = err
        started.set()

    stream.startCaptureWithCompletionHandler_(after_start)
    if not started.wait(timeout):
        return "Timed out starting the capture."
    if sres.get("err") is not None:
        return ("Capture would not start. Check the Screen Recording "
                f"permission. ({sres['err']})")
    tap.stream = stream
    return None


class MicTap(AudioTap):
    """AVCaptureSession microphone input, delivered as 16 kHz mono float32."""

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, out, sbuf, conn):
        try:
            self.stream_didOutputSampleBuffer_ofType_(None, sbuf, 1)
        except Exception as exc:
            self.error = f"mic callback: {exc!r}"


def start_mic(tap, timeout=30):
    import AVFoundation as AV
    status = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)
    if status == 2 or status == 1:             # denied / restricted
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security"
            "?Privacy_Microphone"))
        return ("Turn on 'MyTranscribe' in Microphone (opened for you), "
                "quit MyTranscribe, then open it again.")
    if status == 0:                            # not determined: ask now
        ev, ok = threading.Event(), {}
        def answered(granted):             # a block must return None, not a tuple
            ok["g"] = bool(granted)
            ev.set()

        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio, answered)
        if not ev.wait(timeout) or not ok.get("g"):
            return "Microphone access was not allowed. Allow it and try again."
    dev = AV.AVCaptureDevice.defaultDeviceWithMediaType_(AV.AVMediaTypeAudio)
    if dev is None:
        return "No microphone found."
    sess = AV.AVCaptureSession.alloc().init()
    inp, err = AV.AVCaptureDeviceInput.deviceInputWithDevice_error_(dev, None)
    if inp is None:
        return f"Cannot open the microphone: {err}"
    sess.addInput_(inp)
    out = AV.AVCaptureAudioDataOutput.alloc().init()
    out.setAudioSettings_({
        AV.AVFormatIDKey: 1819304813,           # 'lpcm'
        AV.AVSampleRateKey: float(RATE),
        AV.AVNumberOfChannelsKey: 1,
        AV.AVLinearPCMBitDepthKey: 32,
        AV.AVLinearPCMIsFloatKey: True,
        AV.AVLinearPCMIsBigEndianKey: False,
        AV.AVLinearPCMIsNonInterleaved: False})
    q = dispatch_queue_create(b"mytranscribe.mic", None)
    out.setSampleBufferDelegate_queue_(tap, q)
    sess.addOutput_(out)
    sess.startRunning()
    tap.stream = sess
    tap.mic_name = str(dev.localizedName())
    return None


def stop_capture(tap):
    if isinstance(tap, MicTap):
        sess, tap.stream = tap.stream, None
        if sess is not None:
            sess.stopRunning()
        return
    stream, tap.stream = tap.stream, None
    if stream is not None:
        ev = threading.Event()
        stream.stopCaptureWithCompletionHandler_(lambda e: ev.set())
        ev.wait(5)


def can_type(prompt=False):
    """True when macOS lets this app send keystrokes (Accessibility)."""
    return bool(AXIsProcessTrustedWithOptions(
        {kAXTrustedCheckOptionPrompt: True} if prompt else None))


BREAK_RE = re.compile(r"[\s,.;:]*\bnew[\s-]?(line|paragraph)\b[\s,.;:!?]*", re.I)


def spoken_breaks(text):
    """Turn the spoken words "new line" / "new paragraph" into line breaks."""
    return BREAK_RE.sub(
        lambda m: "\n\n" if m.group(1).lower() == "paragraph" else "\n", text)


def type_text(text):
    """Type `text` as keystrokes into whatever has keyboard focus. Unicode
    strings are attached to the key events, so no layout or clipboard is
    involved; sent a few characters per event, which every app accepts.
    A line break is sent as Shift-Return, so a chat box does not send."""
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for n, line in enumerate(text.split("\n")):
        if n:
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(src, 36, down)   # Return
                Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskShift)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.01)
        for i in range(0, len(line), 16):
            piece = line[i:i + 16]
            units = len(piece.encode("utf-16-le")) // 2
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(src, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(ev, units, piece)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.01)


class Controller(NSObject):
    """Native window: Capture button, model menu, text area, status."""

    def init(self):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.msgs = queue.Queue()
        self.tap = None
        self.running = False
        self.model = None
        self.model_name = None
        self.speakers = None
        self.models_ready = threading.Event()
        self.run_source = "system"
        self.typeq = queue.Queue()
        self.build()
        self.build_panel()
        self.restore_prefs()
        threading.Thread(target=self.typist, daemon=True).start()
        threading.Thread(target=self.preload, daemon=True).start()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, "pump:", None, True)
        return self

    # -- window -------------------------------------------------------------
    @objc.python_method
    def build(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 640, 420), style, NSBackingStoreBuffered, False)
        self.win.setTitle_(f"MyTranscribe {VERSION}")
        self.win.setMinSize_(NSMakeSize(480, 300))
        self.win.setReleasedWhenClosed_(False)
        self.win.setDelegate_(self)
        if not self.win.setFrameUsingName_(FRAME_NAME):   # remembered position
            self.win.center()
        v = self.win.contentView()
        W, H = 640, 420

        def button(title, x, w, action, mask=NSViewMinYMargin):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, H - 40, w, 28))
            b.setTitle_(title)
            b.setBezelStyle_(NSBezelStyleRounded)
            b.setTarget_(self)
            b.setAction_(action)
            b.setAutoresizingMask_(mask)
            v.addSubview_(b)
            return b

        self.btn = button("Capture", 12, 80, "toggle:")
        self.btn.setKeyEquivalent_("\r")
        lab = NSTextField.labelWithString_("Model:")
        lab.setFrame_(NSMakeRect(104, H - 36, 50, 20))
        lab.setAutoresizingMask_(NSViewMinYMargin)
        v.addSubview_(lab)
        self.popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(156, H - 40, 110, 28), False)
        self.popup.addItemsWithTitles_(MODELS)
        self.popup.selectItemWithTitle_(DEFAULT_MODEL)
        self.popup.setAutoresizingMask_(NSViewMinYMargin)
        v.addSubview_(self.popup)
        self.spk_box = NSButton.alloc().initWithFrame_(
            NSMakeRect(276, H - 40, 96, 28))
        self.spk_box.setButtonType_(NSButtonTypeSwitch)
        self.spk_box.setTitle_("Speakers")
        self.spk_box.setState_(1)
        self.spk_box.setAutoresizingMask_(NSViewMinYMargin)
        v.addSubview_(self.spk_box)
        self.float_box = NSButton.alloc().initWithFrame_(
            NSMakeRect(W - 96, 38, 86, 24))
        self.float_box.setButtonType_(NSButtonTypeSwitch)
        self.float_box.setTitle_("Floating")
        self.float_box.setTarget_(self)
        self.float_box.setAction_("floatChanged:")
        self.float_box.setAutoresizingMask_(NSViewMinXMargin)
        v.addSubview_(self.float_box)
        right = NSViewMinXMargin | NSViewMinYMargin
        button("Copy", W - 76, 64, "copy:", right)
        button("Save…", W - 146, 64, "save:", right)
        button("Clear", W - 216, 64, "clear:", right)

        # Mac/Mic switch: the chosen one is filled with the accent color. The
        # tab view underneath has no tabs of its own.
        self.source = self.make_switch(NSMakeRect(12, H - 76, 150, 26))
        self.source.setAutoresizingMask_(NSViewMinYMargin)
        v.addSubview_(self.source)
        self.tabs = NSTabView.alloc().initWithFrame_(
            NSMakeRect(12, 62, W - 24, H - 142))
        self.tabs.setTabViewType_(4)            # NSNoTabsBezelBorder
        self.tabs.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.text = self.make_pane(MAC)
        self.mic_text = self.make_pane(MIC)
        v.addSubview_(self.tabs)

        self.status = NSTextField.labelWithString_(
            "Ready. Transcribes whatever the Mac is playing.")
        self.status.setFrame_(NSMakeRect(12, 30, W - 116, 36))
        self.status.setLineBreakMode_(NSLineBreakByWordWrapping)
        self.status.setMaximumNumberOfLines_(2)
        self.status.setAutoresizingMask_(NSViewWidthSizable)
        v.addSubview_(self.status)
        self.level = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(12, 14, W - 24, 12))
        self.level.setIndeterminate_(False)
        self.level.setMinValue_(0)
        self.level.setMaxValue_(0.3)
        self.level.setAutoresizingMask_(NSViewWidthSizable)
        v.addSubview_(self.level)

    def windowDidResize_(self, note):
        self.windowDidMove_(note)

    def windowWillClose_(self, note):
        if note.object() is self.panel:          # closing the panel = full window
            self.set_floating(False)
            return
        self.running = False
        if self.tap is not None:
            stop_capture(self.tap)
        NSApplication.sharedApplication().terminate_(None)

    # -- floating panel -----------------------------------------------------
    @objc.python_method
    def build_panel(self):
        """Small always-in-front panel. Non-activating: clicking it never
        takes focus from the app being typed in."""
        W, H = 360, 170
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskResizable | NSWindowStyleMaskUtilityWindow |
                 NSWindowStyleMaskNonactivatingPanel)
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        p.setTitle_("MyTranscribe")
        p.setFloatingPanel_(True)
        p.setLevel_(NSFloatingWindowLevel)
        p.setBecomesKeyOnlyIfNeeded_(True)
        p.setHidesOnDeactivate_(False)
        p.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
                                 NSWindowCollectionBehaviorFullScreenAuxiliary)
        p.setMinSize_(NSMakeSize(300, 140))
        p.setReleasedWhenClosed_(False)
        p.setDelegate_(self)
        if not p.setFrameUsingName_(PANEL_FRAME):
            p.center()
        v = p.contentView()
        top = NSViewMinYMargin

        self.pbtn = NSButton.alloc().initWithFrame_(NSMakeRect(8, H - 36, 76, 28))
        self.pbtn.setTitle_("Capture")
        self.pbtn.setBezelStyle_(NSBezelStyleRounded)
        self.pbtn.setTarget_(self)
        self.pbtn.setAction_("toggle:")
        self.pbtn.setAutoresizingMask_(top)
        v.addSubview_(self.pbtn)
        self.psource = self.make_switch(NSMakeRect(88, H - 35, 116, 26))
        self.psource.setAutoresizingMask_(top)
        v.addSubview_(self.psource)
        full = NSButton.alloc().initWithFrame_(NSMakeRect(W - 106, H - 36, 98, 28))
        full.setTitle_("Full Window")
        full.setBezelStyle_(NSBezelStyleRounded)
        full.setTarget_(self)
        full.setAction_("fullWindow:")
        full.setAutoresizingMask_(NSViewMinXMargin | top)
        v.addSubview_(full)
        self.type_box = NSButton.alloc().initWithFrame_(NSMakeRect(10, H - 62, 200, 24))
        self.type_box.setButtonType_(NSButtonTypeSwitch)
        self.type_box.setTitle_("Type at cursor")
        self.type_box.setTarget_(self)
        self.type_box.setAction_("typeChanged:")
        self.type_box.setAutoresizingMask_(top)
        v.addSubview_(self.type_box)

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(8, 26, W - 16, H - 92))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.ptext = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 16, H - 92))
        self.ptext.setFont_(NSFont.systemFontOfSize_(13))
        self.ptext.setEditable_(False)
        self.ptext.setVerticallyResizable_(True)
        self.ptext.setAutoresizingMask_(NSViewWidthSizable)
        self.ptext.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(self.ptext)
        v.addSubview_(scroll)
        self.pstatus = NSTextField.labelWithString_("")
        self.pstatus.setFont_(NSFont.systemFontOfSize_(11))
        self.pstatus.setFrame_(NSMakeRect(10, 4, W - 20, 18))
        self.pstatus.setAutoresizingMask_(NSViewWidthSizable)
        v.addSubview_(self.pstatus)
        self.panel = p

    @objc.python_method
    def set_floating(self, on):
        """Show the panel instead of the main window (or back again)."""
        self.float_box.setState_(1 if on else 0)
        d = NSUserDefaults.standardUserDefaults()
        d.setBool_forKey_(bool(on), "floating")
        d.synchronize()
        if on:
            self.win.orderOut_(None)
            self.panel.orderFrontRegardless()
        else:
            self.panel.orderOut_(None)
            self.win.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def floatChanged_(self, sender):
        self.set_floating(bool(self.float_box.state()))

    def fullWindow_(self, sender):
        self.set_floating(False)

    @objc.python_method
    def make_switch(self, frame):
        """Mac/Mic as two buttons painted by hand: the chosen one is filled
        with the accent color in every window, the panel included, which
        never becomes active and would otherwise draw it a dim gray."""
        box = NSView.alloc().initWithFrame_(frame)
        w, h = frame.size.width / len(SOURCES), frame.size.height
        for i, name in enumerate(SOURCES):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(i * w, 0, w - 3, h))
            b.setBordered_(False)
            b.setWantsLayer_(True)
            b.layer().setCornerRadius_(6)
            b.setTitle_(name)
            b.setTag_(i)
            b.setTarget_(self)
            b.setAction_("sourceChanged:")
            box.addSubview_(b)
        return box

    def sourceChanged_(self, sender):
        self.tabs.selectTabViewItemWithIdentifier_(SOURCES[sender.tag()])

    def typeChanged_(self, sender):
        on = bool(self.type_box.state())
        if on and not can_type(prompt=True):
            self.pstatus.setStringValue_(
                "Allow MyTranscribe in Accessibility, then turn this on again.")
            self.type_box.setState_(0)
            on = False
        d = NSUserDefaults.standardUserDefaults()
        d.setBool_forKey_(on, "typeAtCursor")
        d.synchronize()

    def windowDidMove_(self, note):
        if note.object() is getattr(self, "panel", None):   # None while building
            self.panel.saveFrameUsingName_(PANEL_FRAME)
        else:
            self.win.saveFrameUsingName_(FRAME_NAME)
        NSUserDefaults.standardUserDefaults().synchronize()

    @objc.python_method
    def typist(self):
        """Types queued text in order, off the main thread so the panel stays
        responsive. Never types into MyTranscribe itself."""
        me = os.getpid()
        while True:
            text = self.typeq.get()
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None and front.processIdentifier() in (me, os.getppid()):
                continue
            try:
                type_text(text)
            except Exception as exc:
                logline(f"type error: {exc!r}")

    # -- messages from the worker thread -----------------------------------
    @objc.python_method
    def restore_prefs(self):
        d = NSUserDefaults.standardUserDefaults()
        model = d.stringForKey_("model")
        if model in MODELS:
            self.popup.selectItemWithTitle_(model)
        if d.objectForKey_("speakerLabels") is not None:
            self.spk_box.setState_(1 if d.boolForKey_("speakerLabels") else 0)
        tab = OLD_TABS.get(d.stringForKey_("tab"), d.stringForKey_("tab"))
        if tab in SOURCES:
            self.tabs.selectTabViewItemWithIdentifier_(tab)
        self.show_source(str(self.tabs.selectedTabViewItem().label()))
        for ctl in (self.popup, self.spk_box):
            ctl.setTarget_(self)
            ctl.setAction_("prefChanged:")
        self.tabs.setDelegate_(self)
        # Typing stays on only while Accessibility still allows it.
        self.type_box.setState_(1 if d.boolForKey_("typeAtCursor") and can_type() else 0)
        self.float_box.setState_(1 if d.boolForKey_("floating") else 0)

    def prefChanged_(self, sender):
        d = NSUserDefaults.standardUserDefaults()
        d.setObject_forKey_(str(self.popup.titleOfSelectedItem()), "model")
        d.setBool_forKey_(bool(self.spk_box.state()), "speakerLabels")
        d.synchronize()

    def tabView_didSelectTabViewItem_(self, tabs, item):
        d = NSUserDefaults.standardUserDefaults()
        d.setObject_forKey_(str(item.label()), "tab")
        d.synchronize()
        self.show_source(str(item.label()))

    @objc.python_method
    def ready_text(self):
        if str(self.tabs.selectedTabViewItem().label()) == MIC:
            return "Ready. Captures what you say into the microphone."
        return "Ready. Captures whatever the Mac is playing."

    @objc.python_method
    def show_source(self, name):
        if not self.running and self.models_ready.is_set():
            self.msgs.put(("ready", None))
        on = SOURCES.index(name)
        for box in (self.source, self.psource):
            for b in box.subviews():
                chosen = b.tag() == on
                b.layer().setBackgroundColor_(
                    (NSColor.controlAccentColor() if chosen
                     else NSColor.quaternaryLabelColor()).CGColor())
                b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
                    str(b.title()), {
                        NSFontAttributeName: NSFont.boldSystemFontOfSize_(13),
                        NSForegroundColorAttributeName: (NSColor.whiteColor() if chosen
                                                         else NSColor.secondaryLabelColor())}))

    @objc.python_method
    def make_pane(self, title):
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 700, 380))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 700, 380))
        tv.setFont_(NSFont.systemFontOfSize_(16))
        tv.setEditable_(True)
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.setAutoresizingMask_(NSViewWidthSizable)
        tv.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(tv)
        item = NSTabViewItem.alloc().initWithIdentifier_(title)
        item.setLabel_(title)
        item.setView_(scroll)
        self.tabs.addTabViewItem_(item)
        return tv

    @objc.python_method
    def cur_text(self):
        return (self.mic_text if str(self.tabs.selectedTabViewItem().label())
                == MIC else self.text)

    @objc.python_method
    def say(self, msg):
        if msg.startswith(("Error", "Capture", "Screen", "Timed", "No display", "Turn on")):
            logline(msg)
        self.msgs.put(("status", msg))

    def pump_(self, timer):
        try:
            while True:
                kind, val = self.msgs.get_nowait()
                if kind == "ready":
                    kind, val = "status", self.ready_text()
                if kind == "status":
                    self.status.setStringValue_(val)
                    self.pstatus.setStringValue_(val)
                elif kind == "text":
                    val = spoken_breaks(val)
                    tail = "" if val.endswith("\n") else " "
                    tv = self.mic_text if self.run_source == "mic" else self.text
                    tv.textStorage().appendAttributedString_(
                        NSAttributedString.alloc().initWithString_attributes_(
                            val + tail, {
                                NSForegroundColorAttributeName:
                                    NSColor.labelColor(),
                                NSFontAttributeName:
                                    NSFont.systemFontOfSize_(16)}))
                    tv.scrollRangeToVisible_(
                        NSMakeRange(tv.string().length(), 0))
                    # The panel and the typed text drop speaker labels (and
                    # the breaks before them); spoken breaks are kept.
                    plain = re.sub(r"\s*Speaker \d+:\s*", " ", val)
                    plain = "\n".join(" ".join(ln.split()) for ln in plain.split("\n"))
                    plain = plain.strip(" ")
                    if plain:
                        plain += tail
                        self.ptext.textStorage().appendAttributedString_(
                            NSAttributedString.alloc().initWithString_attributes_(
                                plain, {
                                    NSForegroundColorAttributeName: NSColor.labelColor(),
                                    NSFontAttributeName: NSFont.systemFontOfSize_(13)}))
                        self.ptext.scrollRangeToVisible_(
                            NSMakeRange(self.ptext.string().length(), 0))
                        if self.type_box.state():
                            self.typeq.put(plain)
                elif kind == "level":
                    self.level.setDoubleValue_(val)
                elif kind == "stopped":
                    self.set_button("Capture", True)
                    self.running = False
        except queue.Empty:
            pass
        except Exception as exc:
            logline(f"pump error: {exc!r}")

    # -- buttons ------------------------------------------------------------
    @objc.python_method
    def set_button(self, title, enabled):
        """The main window's and the panel's Capture buttons, kept alike."""
        for b in (self.btn, self.pbtn):
            b.setTitle_(title)
            b.setEnabled_(enabled)

    def clear_(self, sender):
        self.cur_text().setString_("")
        self.ptext.setString_("")

    def copy_(self, sender):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(str(self.cur_text().string()).strip(), NSPasteboardTypeString)
        self.status.setStringValue_("Copied.")

    def save_(self, sender):
        panel = NSSavePanel.savePanel()
        panel.setNameFieldStringValue_("transcript.txt")
        if panel.runModal() == 1:
            path = panel.URL().path()
            with open(path, "w") as fh:
                fh.write(str(self.cur_text().string()).strip() + "\n")
            self.status.setStringValue_(f"Saved {path}")

    def toggle_(self, sender):
        if self.running:
            self.running = False
            self.set_button("Stopping…", False)
            return
        self.running = True
        self.set_button("Stop", True)
        self.model_choice = str(self.popup.titleOfSelectedItem())
        self.use_speakers = bool(self.spk_box.state())
        self.run_source = ("mic" if str(self.tabs.selectedTabViewItem().label())
                           == MIC else "system")
        threading.Thread(target=self.run, daemon=True).start()

    # -- worker -------------------------------------------------------------
    @objc.python_method
    def run(self):
        try:
            self.say("Loading models…")
            self.models_ready.wait()
            self.load_model()
            mic = self.run_source == "mic"
            self.speakers = (Speakers(self.encoder) if self.use_speakers
                             and self.encoder and not mic else None)
            self.tap = MicTap.alloc().init() if mic else AudioTap.alloc().init()
            err = start_mic(self.tap) if mic else start_capture(self.tap)
            if err:
                self.say(err)
                return
            self.say(("Speak now… " if mic else "Listening… ") + "(first text appears after "
                     f"~{CHUNK_SECONDS} s of audio)")
            buf = np.zeros(0, dtype=np.float32)
            while self.running:
                time.sleep(0.5)
                new = self.tap.take()
                if new.size:
                    buf = np.concatenate([buf, new])
                    self.msgs.put(("level", float(np.sqrt(np.mean(new ** 2)))))
                if self.tap.error:
                    self.say(f"Capture problem: {self.tap.error}")
                    self.tap.error = None
                if buf.size >= RATE * CHUNK_SECONDS:
                    self.transcribe(buf)
                    buf = np.zeros(0, dtype=np.float32)
            if buf.size >= RATE * 2:          # flush the tail
                self.transcribe(buf)
            self.say("Stopped.")
        except Exception as exc:
            self.say(f"Error: {exc}")
        finally:
            if self.tap is not None:
                stop_capture(self.tap)
                self.tap = None
            self.msgs.put(("level", 0))
            self.msgs.put(("stopped", None))

    @objc.python_method
    def preload(self):
        """Load Whisper and the voice model at launch so Capture is instant."""
        try:
            self.msgs.put(("status", "Loading models…"))
            self.load_model(DEFAULT_MODEL)
            from speechbrain.inference.speaker import EncoderClassifier
            enc = model_path("ecapa")
            self.encoder = EncoderClassifier.from_hparams(
                source=enc, savedir=enc, run_opts={"device": "cpu"}) if enc else None
        except Exception as exc:
            self.encoder = None
            logline(f"Error preload: {exc!r}")
        self.models_ready.set()
        self.msgs.put(("ready", None))

    @objc.python_method
    def load_model(self, name=None):
        name = name or self.model_choice
        if self.model is None or self.model_name != name:
            import whisper
            local = model_path(name + ".pt")
            self.model = whisper.load_model(local or name)
            self.model_name = name

    @objc.python_method
    def transcribe(self, audio):
        if float(np.sqrt(np.mean(audio ** 2))) < SILENCE_RMS:
            self.say("Listening… (silence)")
            return
        self.say("Transcribing…")
        out = self.model.transcribe(
            audio, fp16=False,
            language="en" if self.model_name.endswith(".en") else None,
            condition_on_previous_text=False)
        segs = [seg for seg in out["segments"] if not hallucinated(seg)]
        if self.speakers is None:
            text = collapse_repeats(" ".join(seg["text"].strip() for seg in segs))
        else:
            text = ""
            for seg in segs:
                t = collapse_repeats(seg["text"].strip())
                if not t:
                    continue
                a, b = int(seg["start"] * RATE), int(seg["end"] * RATE)
                prev = self.speakers.last
                who = self.speakers.label(audio[a:b])
                if who != prev:
                    text += f"\n\nSpeaker {who}: "
                text += t + " "
            text = text.rstrip()
        if text:
            self.msgs.put(("text", text))
        self.say("Listening…")


def hallucinated(seg):
    """Whisper invents text for near-silence or noise, often one phrase over
    and over. Its own per-segment measures give that away: highly repetitive
    text compresses well, and a likely-silent segment is decoded with low
    confidence."""
    if seg.get("compression_ratio", 0) > HALLU_COMPRESSION:
        return True
    return (seg.get("no_speech_prob", 0) > HALLU_NO_SPEECH
            and seg.get("avg_logprob", 0) < HALLU_LOGPROB)


REPEAT_RE = re.compile(r"\b(.{3,80}?)(?:[\s,.]+\1\b){2,}", re.I)


def collapse_repeats(text):
    """A phrase said three or more times in a row is kept once."""
    return REPEAT_RE.sub(r"\1", text).strip()


def model_path(name):
    for d in MODEL_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


class Speakers:
    """Online speaker clustering on ECAPA voice embeddings."""

    def __init__(self, encoder):
        self.encoder = encoder
        self.centroids = []          # unit vectors
        self.counts = []
        self.last = 0

    def label(self, audio):
        """Return a 1-based speaker number for a mono 16 kHz float32 clip."""
        import torch
        if audio.size < RATE * SPK_MIN_SECONDS:
            return self.last or 1
        with torch.no_grad():
            e = torch.nn.functional.normalize(
                self.encoder.encode_batch(
                    torch.from_numpy(audio)[None, :]).squeeze(), dim=0)
        best, score = -1, -1.0
        for i, c in enumerate(self.centroids):
            sc = float(e @ c)
            if sc > score:
                best, score = i, sc
        if best < 0 or (score < SPK_THRESHOLD and len(self.centroids) < SPK_MAX):
            self.centroids.append(e)
            self.counts.append(1)
            best = len(self.centroids) - 1
        else:
            n = self.counts[best]
            c = self.centroids[best] * n + e
            self.centroids[best] = c / c.norm()
            self.counts[best] = n + 1
        self.last = best + 1
        return self.last


def install_menu(app):
    bar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_(
        "Quit MyTranscribe", "terminate:", "q")
    app_item.setSubmenu_(app_menu)
    edit_item = NSMenuItem.alloc().init()
    bar.addItem_(edit_item)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                            ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    edit_item.setSubmenu_(edit)
    app.setMainMenu_(bar)


def selftest_mic():
    tap = MicTap.alloc().init()
    err = start_mic(tap)
    if err:
        print("FAIL:", err)
        return 1
    print("recording 4 s from", getattr(tap, "mic_name", "?"))
    time.sleep(4)
    audio = tap.take()
    stop_capture(tap)
    print(f"samples={audio.size} rms={float(np.sqrt(np.mean(audio**2))) if audio.size else 0:.5f} error={tap.error}")
    return 0 if audio.size else 2


PREF_KEYS = ["model", "speakerLabels", "tab", "floating", "typeAtCursor",
             "NSWindow Frame " + FRAME_NAME, "NSWindow Frame " + PANEL_FRAME]


def migrate_prefs():
    """Bring over settings saved by 2.7.0 and earlier, which ran under a
    plain Python and so kept them in the com.apple.python3 domain."""
    mine = NSUserDefaults.standardUserDefaults()
    if mine.objectForKey_("migratedPrefs"):
        return
    old = NSUserDefaults.alloc().initWithSuiteName_("com.apple.python3")
    for key in PREF_KEYS:
        value = old.objectForKey_(key) if old else None
        if value is not None and mine.objectForKey_(key) is None:
            mine.setObject_forKey_(value, key)
    mine.setBool_forKey_(True, "migratedPrefs")


def selftest():
    tap = AudioTap.alloc().init()
    err = start_capture(tap)
    if err:
        print("FAIL:", err)
        return 1
    print("capturing 5 s of system audio…")
    time.sleep(5)
    audio = tap.take()
    stop_capture(tap)
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    print(f"samples={audio.size} seconds={audio.size / RATE:.1f} rms={rms:.5f} "
          f"peak={float(np.abs(audio).max()) if audio.size else 0:.4f} "
          f"error={tap.error}")
    return 0 if audio.size else 2


if __name__ == "__main__":
    if "--selftest-mic" in sys.argv:
        sys.exit(selftest_mic())
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.stderr = open(os.path.expanduser("~/Library/Logs/MyTranscribe.err"), "a")
    faulthandler.enable(sys.stderr)
    logline(f"start v{VERSION} argv={sys.argv} py={sys.version.split()[0]}")
    migrate_prefs()
    info = NSBundle.mainBundle().infoDictionary()
    info["CFBundleName"] = "MyTranscribe"   # menu-bar name instead of "Python"
    app = NSApplication.sharedApplication()
    install_menu(app)
    icon = NSImage.alloc().initWithContentsOfFile_(ICON)
    if icon is not None:
        app.setApplicationIconImage_(icon)
    app.setActivationPolicy_(0)             # regular app: Dock icon, focus
    ctl = Controller.alloc().init()
    if ctl.float_box.state():                   # reopen as it was left
        ctl.panel.orderFrontRegardless()
    else:
        ctl.win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
    if "--auto" in sys.argv:                # test hook: start at launch
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, ctl, "toggle:", None, False)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            35, app, "terminate:", None, False)
    logline("window shown; entering run loop")
    app.run()
