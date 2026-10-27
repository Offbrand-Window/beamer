"""Offline setup UI; no Bluetooth or input work runs on the GUI thread."""
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core import bluetooth_setup
from core.i18n import tr


def show(parent, book, platform="windows"):
    code = bluetooth_setup.own_code(book, platform)
    dialog = QDialog(parent)
    dialog.setWindowTitle(tr("Beamer Bluetooth · Setup"))
    dialog.resize(600, 540)
    layout = QVBoxLayout(dialog)
    intro = QLabel(tr("Exchange the two private setup codes once. On each computer, paste the other computer's code. "
                   "Choose Advertise on one and Connect on the other. For Windows + Mac, advertise on the Mac. "
                   "Either role can supply the keyboard and mouse."))
    intro.setWordWrap(True)
    layout.addWidget(intro)
    layout.addWidget(QLabel(tr("This computer's private code")))
    own = QPlainTextEdit(code)
    own.setReadOnly(True)
    own.setAccessibleName(tr("This computer's private Bluetooth setup code"))
    own.setMaximumHeight(100)
    layout.addWidget(own)
    copy = QPushButton(tr("Copy my code"))
    copy.clicked.connect(lambda: QApplication.clipboard().setText(code))
    layout.addWidget(copy)
    layout.addWidget(QLabel(tr("Other computer's private code")))
    other = QPlainTextEdit()
    other.setAccessibleName(tr("Other computer's private Bluetooth setup code"))
    other.setPlaceholderText(tr("BB1.…"))
    other.setMaximumHeight(100)
    layout.addWidget(other)
    role = QComboBox()
    role.addItem(tr("Connect to the other computer"), "connect")
    role.addItem(tr("Advertise this computer"), "advertise")
    role.setAccessibleName(tr("Bluetooth role"))
    layout.addWidget(role)
    note = QLabel(tr("Keep these codes private: they authorize input and clipboard access. "
                  "After saving on both computers, arrange the screens in Beamer's screen map. "
                  "Bluetooth carries text clipboard entries up to 16 KiB; images and files are excluded."))
    note.setWordWrap(True)
    layout.addWidget(note)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)
    buttons.rejected.connect(dialog.reject)
    def save():
        try:
            bluetooth_setup.pair(book, code, other.toPlainText(), role.currentData())
        except Exception as exc:
            QMessageBox.warning(dialog, "Bluetooth setup", str(exc))
            return
        dialog.accept()
    buttons.accepted.connect(save)
    return dialog.exec() == QDialog.DialogCode.Accepted
