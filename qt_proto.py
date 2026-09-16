# -*- coding: utf-8 -*-
"""Qt 原型：验证窗口缩放流畅度（与 tkinter 版对比用）"""
import sys
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QProgressBar, QLabel,
                               QCheckBox, QPlainTextEdit, QFrame)
from PySide6.QtCore import Qt

QSS = """
QWidget {
    background: #17181c; color: #e8e9ed;
    font-family: "Microsoft YaHei UI"; font-size: 10.5pt;
}
QFrame#card {
    background: #202127; border: 1px solid #2e3038; border-radius: 8px;
}
QLabel { background: transparent; }
QLineEdit {
    background: #2a2c34; border: 1px solid #2e3038; border-radius: 6px;
    padding: 5px 8px; color: #e8e9ed;
}
QLineEdit:focus { border: 1px solid #3574f0; }
QPushButton {
    background: #33363e; border: 1px solid #2e3038; border-radius: 6px;
    padding: 6px 14px; color: #e8e9ed;
}
QPushButton:hover { background: #3d4149; }
QPushButton#accent {
    background: #3574f0; border: 1px solid #3574f0; color: #ffffff; font-weight: 600;
}
QPushButton#accent:hover { background: #2b5fd0; }
QProgressBar {
    background: #2a2c34; border: none; border-radius: 4px;
}
QProgressBar::chunk { background: #3574f0; border-radius: 4px; }
QPlainTextEdit {
    background: #121317; border: 1px solid #2e3038; border-radius: 6px;
    color: #c8cad2; padding: 6px;
}
"""


class Card(QFrame):
    def __init__(self, title=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(14, 10, 14, 12)
        self.vbox.setSpacing(6)
        if title:
            lbl = QLabel(title)
            lbl.setStyleSheet("color:#9a9ca8; font-weight:600;")
            self.vbox.addWidget(lbl)


class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Qt 原型 - 缩放流畅度对比测试（请拖动边缘/最大化）")
        self.resize(880, 780)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(8)

        top = QHBoxLayout()
        t = QLabel("BD3D 转换器")
        t.setStyleSheet("font-size:16pt; font-weight:700;")
        top.addWidget(t)
        v = QLabel("Qt 原型")
        v.setStyleSheet("color:#6b6d78;")
        top.addWidget(v)
        top.addStretch()
        top.addWidget(QLabel("3D 蓝光双流 → SBS / TAB · GPU 硬件加速"))
        root.addLayout(top)

        c1 = Card()
        for name in ("左眼文件", "右眼文件", "输出到"):
            row = QHBoxLayout()
            lb = QLabel(name)
            lb.setFixedWidth(64)
            row.addWidget(lb)
            row.addWidget(QLineEdit(), 1)
            row.addWidget(QPushButton("浏览"))
            c1.vbox.addLayout(row)
        root.addWidget(c1)

        for title, summ in (("输出格式", "全宽 SBS · MKV"),
                            ("编码设置", "GPU · 高画质 · 质量优先"),
                            ("音频", "自动 · 原声 + AAC 兼容轨"),
                            ("高级", "GOP 96 · 完成后打开")):
            c = Card()
            row = QHBoxLayout()
            row.addWidget(QLabel("›  " + title))
            row.addStretch()
            s = QLabel(summ)
            s.setStyleSheet("color:#6b6d78;")
            row.addWidget(s)
            c.vbox.addLayout(row)
            root.addWidget(c)

        brow = QHBoxLayout()
        b1 = QPushButton("开始转换")
        b1.setObjectName("accent")
        b1.setFixedWidth(130)
        brow.addWidget(b1)
        brow.addWidget(QPushButton("取消"))
        brow.addStretch()
        brow.addWidget(QPushButton("无损拼接（完整片）"))
        root.addLayout(brow)

        c2 = Card()
        row = QHBoxLayout()
        pct = QLabel("0.0%")
        pct.setStyleSheet("font-size:22pt; font-weight:700;")
        row.addWidget(pct)
        row.addWidget(QLabel("就绪"))
        row.addStretch()
        c2.vbox.addLayout(row)
        pb = QProgressBar()
        pb.setValue(35)
        pb.setFixedHeight(8)
        c2.vbox.addWidget(pb)
        c2.vbox.addWidget(QLabel(""))
        root.addWidget(c2)

        c3 = Card("日志")
        log = QPlainTextEdit()
        log.setPlainText(
            "[18:57:01] 就绪。这是 Qt 原型。\n\n"
            "请做以下对比测试：\n"
            "1. 拖动窗口边缘缩放 —— 内容应该即时跟随，无逐块重绘\n"
            "2. 最大化 / 还原 —— 应该瞬间完成\n"
            "3. 把窗口缩到最小再拉大 —— 控件应该全程稳定\n\n"
            "如果明显比之前的 tkinter 版跟手，就可以用 Qt 重写正式版。")
        log.setReadOnly(True)
        c3.vbox.addWidget(log, 1)
        root.addWidget(c3, 1)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    w = Main()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
