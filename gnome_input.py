#!/usr/bin/env python3
"""GNOME Mutter keyboard adapter, isolated so python3-dbus stays optional.

Mutter's interface is private: callers must handle incompatibility and retain
clipboard contents if injection is unavailable. No screen capture is requested.
"""
import sys
import time


def send_key(action):
    import dbus

    bus = dbus.SessionBus()
    destination = 'org.gnome.Mutter.RemoteDesktop'
    manager = dbus.Interface(bus.get_object(destination, '/org/gnome/Mutter/RemoteDesktop'), destination)
    path = manager.CreateSession(timeout=2)
    session = dbus.Interface(bus.get_object(destination, path), destination + '.Session')
    pressed = []
    try:
        session.Start(timeout=2)
        keys = {'paste': [29, 47], 'terminal': [29, 42, 47], 'enter': [28]}[action]
        for key in keys:
            session.NotifyKeyboardKeycode(dbus.UInt32(key), True, timeout=2)
            pressed.append(key)
        for key in reversed(keys):
            session.NotifyKeyboardKeycode(dbus.UInt32(key), False, timeout=2)
            pressed.remove(key)
        time.sleep(0.08)  # Allow the compositor to deliver queued key events.
    finally:
        for key in reversed(pressed):
            try:
                session.NotifyKeyboardKeycode(dbus.UInt32(key), False, timeout=2)
            except Exception:
                pass
        try:
            session.Stop(timeout=2)
        except Exception:
            pass
        bus.close()


if __name__ == '__main__':
    try:
        send_key(sys.argv[1])
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
