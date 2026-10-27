"""Native AppKit setup. Exchanging private codes works with Wi-Fi switched off."""
import AppKit

from core import bluetooth_setup


def show(book):
    code = bluetooth_setup.own_code(book, "macos")
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Beamer Bluetooth · Setup")
    alert.setInformativeText_(
        "Exchange the two private setup codes once. On each computer, paste the other's code. "
        "Choose Advertise on one and Connect on the other. For Windows + Mac, advertise on the Mac. "
        "Either role can supply the mouse and keyboard. Keep codes private: they authorize input and clipboard access.")
    alert.addButtonWithTitle_("Save")
    alert.addButtonWithTitle_("Cancel")
    alert.addButtonWithTitle_("Copy my code")
    view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (520, 245)))
    def label(text, y):
        control = AppKit.NSTextField.labelWithString_(text)
        control.setFrame_(((0, y), (520, 20)))
        view.addSubview_(control)
    label("This computer's private code (use Copy my code)", 220)
    own = AppKit.NSTextField.alloc().initWithFrame_(((0, 185), (520, 28)))
    own.setStringValue_(code)
    own.setEditable_(False)
    own.setSelectable_(True)
    own.setAccessibilityLabel_("This computer's private Bluetooth setup code")
    view.addSubview_(own)
    label("Other computer's private code", 150)
    other = AppKit.NSTextField.alloc().initWithFrame_(((0, 115), (520, 28)))
    other.setPlaceholderString_("BB1.…")
    other.setAccessibilityLabel_("Other computer's private Bluetooth setup code")
    view.addSubview_(other)
    role = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(((0, 70), (520, 28)), False)
    role.addItemsWithTitles_(["Advertise this computer", "Connect to the other computer"])
    role.setAccessibilityLabel_("Bluetooth role")
    view.addSubview_(role)
    label("Text clipboard: up to 16 KiB. Images and files are excluded.", 35)
    label("After saving on both computers, arrange screens in the screen map.", 10)
    alert.setAccessoryView_(view)
    while True:
        result = alert.runModal()
        if result == AppKit.NSAlertSecondButtonReturn:
            return False
        if result == AppKit.NSAlertThirdButtonReturn:
            board = AppKit.NSPasteboard.generalPasteboard()
            board.clearContents()
            board.setString_forType_(code, AppKit.NSPasteboardTypeString)
            continue
        try:
            bluetooth_setup.pair(book, code, str(other.stringValue()),
                                 "advertise" if role.indexOfSelectedItem() == 0 else "connect")
            return True
        except Exception as exc:
            error = AppKit.NSAlert.alloc().init()
            error.setMessageText_("Bluetooth setup")
            error.setInformativeText_(str(exc))
            error.runModal()
